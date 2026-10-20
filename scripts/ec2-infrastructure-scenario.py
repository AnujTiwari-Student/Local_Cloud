"""Isolated EC2 infrastructure reference probe (Boto3). Cleans created resources."""
import json
import os
import boto3
from botocore.config import Config


def scenario():
    client = boto3.client('ec2', endpoint_url=os.environ.get('EC2_PROBE_ENDPOINT', 'http://127.0.0.1:4566'),
                          region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                          aws_session_token=os.environ.get('EC2_PROBE_TOKEN'),
                          config=Config(retries={'max_attempts': 0}, read_timeout=30))
    rows, cleanup = [], []
    def call(label, operation, **args):
        try:
            result = getattr(client, operation)(**args)
            status = result.pop('ResponseMetadata')['HTTPStatusCode']
            rows.append({'case': label, 'operation': operation, 'parameters': args, 'status': status, 'result': result})
            return result
        except Exception as error:
            response = getattr(error, 'response', None)
            rows.append({'case': label, 'operation': operation, 'parameters': args,
                         'status': response.get('ResponseMetadata', {}).get('HTTPStatusCode') if response else None,
                         'error': response.get('Error') if response else {'type': type(error).__name__, 'message': str(error)}})
            return {}
    try:
        vpc = call('vpc-create', 'create_vpc', CidrBlock='10.88.0.0/16').get('Vpc', {}).get('VpcId')
        if not vpc:
            return rows
        cleanup.append(('delete_vpc', {'VpcId': vpc}))
        call('vpc-describe', 'describe_vpcs', VpcIds=[vpc])
        call('vpc-dns', 'describe_vpc_attribute', VpcId=vpc, Attribute='enableDnsSupport')
        subnet = call('subnet-create', 'create_subnet', VpcId=vpc, CidrBlock='10.88.1.0/24', AvailabilityZone='us-east-1a').get('Subnet', {}).get('SubnetId')
        if subnet:
            cleanup.append(('delete_subnet', {'SubnetId': subnet}))
            call('subnet-describe', 'describe_subnets', SubnetIds=[subnet])
            call('subnet-overlap', 'create_subnet', VpcId=vpc, CidrBlock='10.88.1.0/24')
        group = call('sg-create', 'create_security_group', VpcId=vpc, GroupName='rea-web', Description='REA probe').get('GroupId')
        if group:
            cleanup.append(('delete_security_group', {'GroupId': group}))
            permission = {'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080, 'IpRanges': [{'CidrIp': '127.0.0.1/32'}]}
            call('sg-authorize', 'authorize_security_group_ingress', GroupId=group, IpPermissions=[permission])
            call('sg-duplicate', 'authorize_security_group_ingress', GroupId=group, IpPermissions=[permission])
            call('sg-describe', 'describe_security_groups', GroupIds=[group])
            call('sg-revoke', 'revoke_security_group_ingress', GroupId=group, IpPermissions=[permission])
        gateway = call('igw-create', 'create_internet_gateway').get('InternetGateway', {}).get('InternetGatewayId')
        if gateway:
            cleanup.append(('delete_internet_gateway', {'InternetGatewayId': gateway}))
            call('igw-attach', 'attach_internet_gateway', InternetGatewayId=gateway, VpcId=vpc)
            cleanup.append(('detach_internet_gateway', {'InternetGatewayId': gateway, 'VpcId': vpc}))
            call('igw-describe', 'describe_internet_gateways', InternetGatewayIds=[gateway])
        table = call('route-table-create', 'create_route_table', VpcId=vpc).get('RouteTable', {}).get('RouteTableId')
        if table:
            cleanup.append(('delete_route_table', {'RouteTableId': table}))
            if gateway:
                call('route-create', 'create_route', RouteTableId=table, DestinationCidrBlock='0.0.0.0/0', GatewayId=gateway)
            if subnet:
                association = call('route-associate', 'associate_route_table', RouteTableId=table, SubnetId=subnet).get('AssociationId')
                if association:
                    cleanup.append(('disassociate_route_table', {'AssociationId': association}))
            call('route-table-describe', 'describe_route_tables', RouteTableIds=[table])
        volume = call('volume-create', 'create_volume', AvailabilityZone='us-east-1a', Size=1, VolumeType='gp3').get('VolumeId')
        if volume:
            cleanup.append(('delete_volume', {'VolumeId': volume}))
            call('volume-describe', 'describe_volumes', VolumeIds=[volume])
            call('volume-modify', 'modify_volume', VolumeId=volume, Size=2)
            snap = call('snapshot-create', 'create_snapshot', VolumeId=volume, Description='REA probe').get('SnapshotId')
            if snap:
                cleanup.append(('delete_snapshot', {'SnapshotId': snap}))
                call('snapshot-describe', 'describe_snapshots', SnapshotIds=[snap])
        key = call('key-create', 'create_key_pair', KeyName='rea-probe', KeyType='ed25519')
        if key.get('KeyPairId'):
            cleanup.append(('delete_key_pair', {'KeyName': 'rea-probe'}))
            call('key-describe', 'describe_key_pairs', KeyNames=['rea-probe'])
        call('availability-zones', 'describe_availability_zones')
        call('instance-types', 'describe_instance_types', InstanceTypes=['t3.micro'])
        instance = call('instance-create', 'run_instances', ImageId=os.environ.get('EC2_PROBE_IMAGE', 'ami-03cf127a'), MinCount=1, MaxCount=1, InstanceType='t3.micro', **({'SubnetId': subnet, 'SecurityGroupIds': [group]} if subnet and group else {})).get('Instances', [])
        if instance:
            identifier = instance[0]['InstanceId']
            cleanup.append(('terminate_instances', {'InstanceIds': [identifier]}))
            call('instance-status', 'describe_instance_status', InstanceIds=[identifier], IncludeAllInstances=True)
            call('instance-reboot', 'reboot_instances', InstanceIds=[identifier])
            call('instance-console', 'get_console_output', InstanceId=identifier)
            if volume:
                call('volume-attach', 'attach_volume', VolumeId=volume, InstanceId=identifier, Device='/dev/sdf')
                call('volume-in-use-delete', 'delete_volume', VolumeId=volume)
                call('volume-detach', 'detach_volume', VolumeId=volume)
            call('instance-describe-network', 'describe_instances', InstanceIds=[identifier])
        call('missing-vpc', 'describe_vpcs', VpcIds=['vpc-' + '0' * 17])
    finally:
        for operation, args in reversed(cleanup):
            call('cleanup-' + operation, operation, **args)
    # Private key material is not needed as evidence. Record only its presence.
    for row in rows:
        if 'KeyMaterial' in row.get('result', {}):
            row['result']['KeyMaterial'] = '<generated private key omitted>'
    return rows


if __name__ == '__main__':
    print(json.dumps(scenario(), default=str))
