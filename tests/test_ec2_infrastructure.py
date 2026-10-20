import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from localcloud.engine import Engine, Error
from localcloud.ec2_metadata import Metadata
from localcloud.ec2 import query_request, query_response
from localcloud.cli import parser, execute


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'state.db'))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return self.engine.ec2.call(operation, args, 'test')

    def network(self):
        vpc = self.call('CreateVpc', CidrBlock='10.90.0.0/16')['Vpc']['VpcId']
        subnet = self.call('CreateSubnet', VpcId=vpc, CidrBlock='10.90.1.0/24')['Subnet']['SubnetId']
        return vpc, subnet

    def instance(self, **args):
        return self.call('RunInstances', ImageId='ami-local', **args)['Instances'][0]

    def test_network_overlap_dependencies_and_address_allocation(self):
        vpc, subnet = self.network()
        with self.assertRaises(Error) as caught:
            self.call('CreateSubnet', VpcId=vpc, CidrBlock='10.90.1.128/25')
        self.assertEqual(caught.exception.code, 'InvalidSubnet.Conflict')
        with self.assertRaises(Error):
            self.call('CreateSubnet', VpcId=vpc, CidrBlock='10.91.0.0/24')
        first, second = self.instance(SubnetId=subnet), self.instance(SubnetId=subnet)
        self.assertEqual(first['PrivateIpAddress'], '10.90.1.4')
        self.assertEqual(second['PrivateIpAddress'], '10.90.1.5')
        for operation, args in [('DeleteVpc', {'VpcId': vpc}), ('DeleteSubnet', {'SubnetId': subnet})]:
            with self.assertRaises(Error) as caught:
                self.call(operation, **args)
            self.assertEqual(caught.exception.code, 'DependencyViolation')
        self.call('TerminateInstances', InstanceIds=[first['InstanceId'], second['InstanceId']])
        self.call('DeleteSubnet', SubnetId=subnet)
        self.call('DeleteVpc', VpcId=vpc)

    def test_security_group_rules_and_wrong_vpc(self):
        vpc, subnet = self.network()
        group = self.call('CreateSecurityGroup', VpcId=vpc, GroupName='web', Description='test')['GroupId']
        permission = {'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080, 'IpRanges': [{'CidrIp': '127.0.0.1/32'}]}
        self.call('AuthorizeSecurityGroupIngress', GroupId=group, IpPermissions=[permission])
        with self.assertRaises(Error) as caught:
            self.call('AuthorizeSecurityGroupIngress', GroupId=group, IpPermissions=[permission])
        self.assertEqual(caught.exception.code, 'InvalidPermission.Duplicate')
        other = self.call('CreateVpc', CidrBlock='10.92.0.0/16')['Vpc']['VpcId']
        foreign = self.call('CreateSecurityGroup', VpcId=other, GroupName='foreign', Description='test')['GroupId']
        with self.assertRaises(Error):
            self.instance(SubnetId=subnet, SecurityGroupIds=[foreign])
        self.call('RevokeSecurityGroupIngress', GroupId=group, IpPermissions=[permission])
        self.assertEqual(self.call('DescribeSecurityGroups', GroupIds=[group])['SecurityGroups'][0]['IpPermissions'], [])

    def test_rule_batch_is_atomic_and_description_is_not_identity(self):
        group = self.call('CreateSecurityGroup', GroupName='batch', Description='test')['GroupId']
        rule = {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'IpRanges': [{'CidrIp': '127.0.0.1/32'}]}
        with self.assertRaises(Error):
            self.call('AuthorizeSecurityGroupIngress', GroupId=group, IpPermissions=[rule, rule])
        self.assertEqual(self.call('DescribeSecurityGroups', GroupIds=[group])['SecurityGroups'][0]['IpPermissions'], [])
        rule['IpRanges'][0]['Description'] = 'first description'
        self.call('AuthorizeSecurityGroupIngress', GroupId=group, IpPermissions=[rule])
        rule['IpRanges'][0]['Description'] = 'different description'
        with self.assertRaises(Error):
            self.call('AuthorizeSecurityGroupIngress', GroupId=group, IpPermissions=[rule])
        self.call('RevokeSecurityGroupIngress', GroupId=group, IpPermissions=[rule])

    def test_instance_pagination_limits_instances_in_one_reservation(self):
        created = self.call('RunInstances', ImageId='ami-local', MinCount=7, MaxCount=7)['Instances']
        first = self.call('DescribeInstances', MaxResults=5)
        second = self.call('DescribeInstances', MaxResults=5, NextToken=first['NextToken'])
        first_ids = {i['InstanceId'] for r in first['Reservations'] for i in r['Instances']}
        second_ids = {i['InstanceId'] for r in second['Reservations'] for i in r['Instances']}
        self.assertEqual(len(first_ids), 5)
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, {i['InstanceId'] for i in created})

    def test_route_associations_and_gateway_dependencies(self):
        vpc, subnet = self.network()
        gateway = self.call('CreateInternetGateway')['InternetGateway']['InternetGatewayId']
        table = self.call('CreateRouteTable', VpcId=vpc, ClientToken='routes')['RouteTable']['RouteTableId']
        self.assertEqual(self.call('CreateRouteTable', VpcId=vpc, ClientToken='routes')['RouteTable']['RouteTableId'], table)
        with self.assertRaises(Error):
            self.call('CreateRoute', RouteTableId=table, DestinationCidrBlock='0.0.0.0/0', GatewayId=gateway)
        self.call('AttachInternetGateway', InternetGatewayId=gateway, VpcId=vpc)
        self.call('CreateRoute', RouteTableId=table, DestinationCidrBlock='0.0.0.0/0', GatewayId=gateway)
        association = self.call('AssociateRouteTable', RouteTableId=table, SubnetId=subnet)['AssociationId']
        with self.assertRaises(Error):
            self.call('DeleteRouteTable', RouteTableId=table)
        self.call('DisassociateRouteTable', AssociationId=association)
        self.call('DeleteRoute', RouteTableId=table, DestinationCidrBlock='0.0.0.0/0')
        self.call('DeleteRouteTable', RouteTableId=table)

    def test_volume_snapshot_restore_attach_and_delete_on_termination(self):
        item = self.instance(BlockDeviceMappings=[{'DeviceName': '/data', 'Ebs': {'VolumeSize': 2}}])
        key = item['BlockDeviceMappings'][0]['Ebs']['VolumeId']
        with self.assertRaises(Error) as caught:
            self.call('DeleteVolume', VolumeId=key)
        self.assertEqual(caught.exception.code, 'VolumeInUse')
        snapshot = self.call('CreateSnapshot', VolumeId=key)['SnapshotId']
        self.assertEqual(self.call('DescribeSnapshots', SnapshotIds=[snapshot])['Snapshots'][0]['State'], 'completed')
        with self.assertRaises(Error):
            self.call('CreateVolume', AvailabilityZone='us-east-1a', SnapshotId=snapshot, Size=1)
        restored = self.call('CreateVolume', AvailabilityZone='us-east-1a', SnapshotId=snapshot)['VolumeId']
        self.call('TerminateInstances', InstanceIds=[item['InstanceId']])
        with self.assertRaises(Error):
            self.call('DescribeVolumes', VolumeIds=[key])
        self.assertEqual(self.call('DescribeVolumes', VolumeIds=[restored])['Volumes'][0]['Size'], 2)

    def test_volume_zone_and_shrink_validation(self):
        item = self.instance()
        volume = self.call('CreateVolume', AvailabilityZone='us-east-1b', Size=2)['VolumeId']
        with self.assertRaises(Error):
            self.call('AttachVolume', VolumeId=volume, InstanceId=item['InstanceId'], Device='/data')
        with self.assertRaises(Error):
            self.call('ModifyVolume', VolumeId=volume, Size=1)
        self.call('ModifyVolume', VolumeId=volume, Size=3)
        self.assertEqual(self.call('DescribeVolumesModifications', VolumeIds=[volume])['VolumesModifications'][0]['TargetSize'], 3)

    def test_resource_pagination_and_portable_validation(self):
        for i in range(7):
            self.call('CreateVpc', CidrBlock=f'10.{i}.0.0/16')
        first = self.call('DescribeVpcs', MaxResults=5)
        second = self.call('DescribeVpcs', MaxResults=5, NextToken=first['NextToken'])
        self.assertEqual(len(first['Vpcs']) + len(second['Vpcs']), 8)
        with self.assertRaises(Error):
            self.call('DescribeVpcs', NextToken=first['NextToken'], Filters=[{'Name': 'is-default', 'Values': ['true']}])
        bundle = self.engine.tools.export_state()
        self.engine.tools.import_state(bundle)
        self.assertEqual(len(self.call('DescribeVpcs')['Vpcs']), 8)
        corrupt = json.loads(json.dumps(bundle))
        corrupt['state']['ec2']['vpcs'][first['Vpcs'][0]['VpcId']]['DockerPath'] = '/etc'
        corrupt['sha256'] = hashlib.sha256(json.dumps(corrupt['state'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.assertRaises(Error):
            self.engine.tools.import_state(corrupt)

    def test_mock_network_interface_and_elastic_address(self):
        vpc, subnet = self.network()
        item = self.instance(SubnetId=subnet)
        interface = self.call('CreateNetworkInterface', SubnetId=subnet)['NetworkInterface']['NetworkInterfaceId']
        attachment = self.call('AttachNetworkInterface', NetworkInterfaceId=interface, InstanceId=item['InstanceId'], DeviceIndex=1)['AttachmentId']
        with self.assertRaises(Error):
            self.call('DeleteNetworkInterface', NetworkInterfaceId=interface)
        self.call('DetachNetworkInterface', AttachmentId=attachment)
        self.call('DeleteNetworkInterface', NetworkInterfaceId=interface)
        address = self.call('AllocateAddress')['AllocationId']
        association = self.call('AssociateAddress', AllocationId=address, InstanceId=item['InstanceId'])['AssociationId']
        with self.assertRaises(Error):
            self.call('ReleaseAddress', AllocationId=address)
        self.call('DisassociateAddress', AssociationId=association)
        self.call('ReleaseAddress', AllocationId=address)

    def test_iam_faults_and_trace_for_infrastructure(self):
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error) as caught:
            self.call('CreateVpc', CidrBlock='10.0.0.0/16')
        self.assertEqual(caught.exception.code, 'AccessDenied')
        self.engine.iam_mode = 'off'
        self.engine.tools.set_rule({'name': 'vpc-down', 'service': 'ec2', 'operation': 'CreateVpc', 'error': {'code': 'Unavailable', 'status': 503}})
        with self.assertRaises(Error) as caught:
            self.call('CreateVpc', CidrBlock='10.0.0.0/16')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.engine.tools.traces()[-1]['operation'], 'CreateVpc')

    def test_public_key_import_and_private_key_not_persisted(self):
        with patch('localcloud.ec2_infrastructure.subprocess.run') as generate:
            def create(*args, **kwargs):
                path = Path(args[0][args[0].index('-f') + 1])
                path.write_text('PRIVATE KEY TEST')
                path.with_suffix('.pub').write_text('ssh-ed25519 ' + base64.b64encode(b'x' * 32).decode())
            generate.side_effect = create
            result = self.call('CreateKeyPair', KeyName='key', KeyType='ed25519')
        self.assertEqual(result['KeyMaterial'], 'PRIVATE KEY TEST')
        self.assertNotIn('PRIVATE KEY TEST', json.dumps(self.engine.tools.export_state()))
        self.assertEqual(self.call('DescribeKeyPairs', KeyNames=['key'])['KeyPairs'][0]['KeyName'], 'key')

    def test_model_codec_and_infrastructure_cli(self):
        operation, args = query_request(b'Action=DescribeInternetGateways&InternetGatewayId.1=igw-abc')
        self.assertEqual(args['InternetGatewayIds'], ['igw-abc'])
        self.assertIn('vpcSet', query_response('DescribeVpcs', {'Vpcs': []}))
        client = Mock()
        execute(parser().parse_args(['ec2', 'create-vpc', '--cidr-block', '10.0.0.0/16']), client)
        self.assertEqual(client.request.call_args.args[2], {'Action': 'CreateVpc', 'CidrBlock': '10.0.0.0/16'})
        execute(parser().parse_args(['ec2', 'create-volume', '--availability-zone', 'us-east-1a', '--size', '2']), client)
        self.assertEqual(client.request.call_args.args[2]['Size'], 2)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.instance = {'InstanceId': 'i-' + '1' * 17, 'ImageId': 'ami-local', 'InstanceType': 't3.micro', 'PrivateIpAddress': '10.0.0.4', 'MetadataOptions': {'HttpTokens': 'required'}, 'UserData': base64.b64encode(b'echo hello').decode()}
        self.metadata = Metadata(self.instance)

    def test_token_requirement_expiry_and_instance_isolation(self):
        path = '/latest/meta-data/instance-id'
        self.assertEqual(self.metadata.request('GET', path, {})[0], 401)
        status, token = self.metadata.request('PUT', '/latest/api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '60'})
        self.assertEqual(status, 200)
        header = {'X-aws-ec2-metadata-token': token}
        self.assertEqual(self.metadata.request('GET', path, header), (200, self.instance['InstanceId']))
        self.assertEqual(Metadata(self.instance).request('GET', path, header)[0], 401)
        self.metadata.tokens[token] = time.monotonic() - 1
        self.assertEqual(self.metadata.request('GET', path, header)[0], 401)

    def test_token_validation_v1_userdata_and_disabled_endpoint(self):
        self.assertEqual(self.metadata.request('PUT', '/latest/api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '21601'})[0], 400)
        self.instance['MetadataOptions']['HttpTokens'] = 'optional'
        self.assertEqual(self.metadata.request('GET', '/latest/user-data', {}), (200, b'echo hello'))
        self.assertEqual(self.metadata.request('GET', '/latest/meta-data/iam/security-credentials/', {})[0], 404)
        self.instance['MetadataOptions']['HttpEndpoint'] = 'disabled'
        self.assertEqual(self.metadata.request('GET', '/latest/meta-data/instance-id', {})[0], 404)
