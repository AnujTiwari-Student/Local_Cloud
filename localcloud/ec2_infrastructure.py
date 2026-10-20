"""Persistent EC2 infrastructure contracts reconstructed from reference captures.

Network resources model AWS configuration; Docker bridges provide local VPC
isolation. This module never installs host routes or host firewall rules.
"""
import base64
import copy
import hashlib
import ipaddress
import json
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .engine import Error, integer, wildcard
from .ec2_networking import Networking
from .ec2_storage import PARAMETERS as STORAGE_PARAMETERS, StorageExtension
from .ec2_images import PARAMETERS as IMAGE_PARAMETERS, ImageLifecycle

ACCOUNT = '000000000000'
REGION = 'us-east-1'
ZONE_IDS = {'a': 6, 'b': 1, 'c': 2, 'd': 4, 'e': 3, 'f': 5}


def identifier(prefix):
    return prefix + '-' + uuid.uuid4().hex[:17]


def now():
    return datetime.now(timezone.utc).isoformat()


def cidr(value):
    try:
        network = ipaddress.IPv4Network(value, strict=False)
    except (ValueError, TypeError):
        raise Error('InvalidParameterValue', 'Expected an IPv4 CIDR')
    return network


def text(value, limit=255):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or '\x00' in value:
        raise Error('InvalidParameterValue', 'Expected nonempty text of bounded length')
    return value


def boolean(value):
    if type(value) is not bool:
        raise Error('InvalidParameterValue', 'Expected boolean')
    return value


def arn(kind, key):
    return f'arn:aws:ec2:{REGION}:{ACCOUNT}:{kind}/{key}'


COLLECTIONS = {
    'Vpc': ('vpcs', 'VpcId', 'vpc', 'InvalidVpcID.NotFound'),
    'Subnet': ('subnets', 'SubnetId', 'subnet', 'InvalidSubnetID.NotFound'),
    'SecurityGroup': ('groups', 'GroupId', 'sg', 'InvalidGroup.NotFound'),
    'InternetGateway': ('gateways', 'InternetGatewayId', 'igw', 'InvalidInternetGatewayID.NotFound'),
    'RouteTable': ('route_tables', 'RouteTableId', 'rtb', 'InvalidRouteTableID.NotFound'),
    'Volume': ('volumes', 'VolumeId', 'vol', 'InvalidVolume.NotFound'),
    'Snapshot': ('snapshots', 'SnapshotId', 'snap', 'InvalidSnapshot.NotFound'),
    'KeyPair': ('keys', 'KeyPairId', 'key', 'InvalidKeyPair.NotFound'),
    'NetworkInterface': ('interfaces', 'NetworkInterfaceId', 'eni', 'InvalidNetworkInterfaceID.NotFound'),
    'Address': ('addresses', 'AllocationId', 'eipalloc', 'InvalidAllocationID.NotFound'),
}

PARAMETERS = {
    **STORAGE_PARAMETERS,
    **IMAGE_PARAMETERS,
    'CreateVpc': {'CidrBlock', 'InstanceTenancy', 'TagSpecifications'},
    'DeleteVpc': {'VpcId'}, 'DescribeVpcs': {'VpcIds', 'Filters', 'MaxResults', 'NextToken'},
    'DescribeVpcAttribute': {'VpcId', 'Attribute'}, 'ModifyVpcAttribute': {'VpcId', 'EnableDnsSupport', 'EnableDnsHostnames'},
    'CreateSubnet': {'VpcId', 'CidrBlock', 'AvailabilityZone', 'TagSpecifications'},
    'DescribeSubnets': {'SubnetIds', 'Filters', 'MaxResults', 'NextToken'}, 'DeleteSubnet': {'SubnetId'},
    'ModifySubnetAttribute': {'SubnetId', 'MapPublicIpOnLaunch'},
    'CreateSecurityGroup': {'VpcId', 'GroupName', 'Description', 'TagSpecifications'},
    'DescribeSecurityGroups': {'GroupIds', 'GroupNames', 'Filters', 'MaxResults', 'NextToken'},
    'DeleteSecurityGroup': {'GroupId', 'GroupName'},
    **{action + 'SecurityGroup' + direction: {'GroupId', 'GroupName', 'IpPermissions', 'IpProtocol', 'FromPort', 'ToPort', 'CidrIp'}
       for action in ('Authorize', 'Revoke') for direction in ('Ingress', 'Egress')},
    'CreateInternetGateway': {'TagSpecifications'}, 'DescribeInternetGateways': {'InternetGatewayIds', 'Filters', 'MaxResults', 'NextToken'},
    'DeleteInternetGateway': {'InternetGatewayId'}, 'AttachInternetGateway': {'InternetGatewayId', 'VpcId'}, 'DetachInternetGateway': {'InternetGatewayId', 'VpcId'},
    'CreateRouteTable': {'VpcId', 'TagSpecifications', 'ClientToken'}, 'DescribeRouteTables': {'RouteTableIds', 'Filters', 'MaxResults', 'NextToken'},
    'DeleteRouteTable': {'RouteTableId'}, 'AssociateRouteTable': {'RouteTableId', 'SubnetId'}, 'DisassociateRouteTable': {'AssociationId'},
    **{action + 'Route': {'RouteTableId', 'DestinationCidrBlock', 'GatewayId', 'NetworkInterfaceId'} for action in ('Create', 'Replace', 'Delete')},
    'CreateVolume': {'AvailabilityZone', 'Size', 'VolumeType', 'Iops', 'Throughput', 'Encrypted', 'SnapshotId', 'TagSpecifications', 'ClientToken'},
    'DescribeVolumes': {'VolumeIds', 'Filters', 'MaxResults', 'NextToken'}, 'DeleteVolume': {'VolumeId'},
    'ModifyVolume': {'VolumeId', 'Size', 'VolumeType', 'Iops', 'Throughput'}, 'DescribeVolumesModifications': {'VolumeIds', 'Filters', 'MaxResults', 'NextToken'},
    'AttachVolume': {'VolumeId', 'InstanceId', 'Device'}, 'DetachVolume': {'VolumeId', 'InstanceId', 'Device', 'Force'},
    'CreateSnapshot': {'VolumeId', 'Description', 'TagSpecifications'}, 'DescribeSnapshots': {'SnapshotIds', 'OwnerIds', 'Filters', 'MaxResults', 'NextToken'}, 'DeleteSnapshot': {'SnapshotId'},
    'CreateKeyPair': {'KeyName', 'KeyType', 'KeyFormat', 'TagSpecifications'}, 'ImportKeyPair': {'KeyName', 'PublicKeyMaterial', 'TagSpecifications'},
    'DescribeKeyPairs': {'KeyNames', 'KeyPairIds', 'IncludePublicKey', 'Filters'}, 'DeleteKeyPair': {'KeyName', 'KeyPairId'},
    'DescribeAvailabilityZones': {'ZoneNames', 'ZoneIds', 'Filters', 'AllAvailabilityZones'},
    'DescribeInstanceTypes': {'InstanceTypes', 'Filters', 'MaxResults', 'NextToken'},
    'CreateNetworkInterface': {'SubnetId', 'Description', 'PrivateIpAddress', 'Groups', 'TagSpecifications', 'ClientToken'},
    'DescribeNetworkInterfaces': {'NetworkInterfaceIds', 'Filters', 'MaxResults', 'NextToken'}, 'DeleteNetworkInterface': {'NetworkInterfaceId'},
    'AttachNetworkInterface': {'NetworkInterfaceId', 'InstanceId', 'DeviceIndex'}, 'DetachNetworkInterface': {'AttachmentId', 'Force'},
    'ModifyNetworkInterfaceAttribute': {'NetworkInterfaceId', 'Description', 'SourceDestCheck', 'Groups', 'Attachment'},
    'DescribeNetworkInterfaceAttribute': {'NetworkInterfaceId', 'Attribute'},
    'AssignPrivateIpAddresses': {'NetworkInterfaceId', 'PrivateIpAddresses', 'SecondaryPrivateIpAddressCount', 'AllowReassignment'},
    'UnassignPrivateIpAddresses': {'NetworkInterfaceId', 'PrivateIpAddresses'},
    'AllocateAddress': {'Domain', 'TagSpecifications'}, 'DescribeAddresses': {'AllocationIds', 'PublicIps', 'Filters'},
    'AssociateAddress': {'AllocationId', 'InstanceId', 'NetworkInterfaceId', 'PrivateIpAddress', 'AllowReassociation'},
    'DisassociateAddress': {'AssociationId', 'PublicIp'}, 'ReleaseAddress': {'AllocationId', 'PublicIp'},
}


def tag_specs(specs, kind):
    from .ec2 import validated_tags
    if specs is None:
        return []
    if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], dict) or set(specs[0]) != {'ResourceType', 'Tags'} or specs[0]['ResourceType'] != kind:
        raise Error('InvalidParameterValue', 'Expected one tag specification for ' + kind)
    return validated_tags(specs[0]['Tags'])


def paginated(result, key, args):
    items = result[key]
    if not args.get('NextToken') and 'MaxResults' not in args:
        return result
    size = integer(args.get('MaxResults', 100), 5, 1000)
    query = {k: v for k, v in args.items() if k not in ('NextToken', 'MaxResults')}
    digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:24]
    offset = 0
    if args.get('NextToken'):
        try:
            token = args['NextToken']
            if not isinstance(token, str) or len(token) > 256:
                raise ValueError()
            raw = json.loads(base64.urlsafe_b64decode(token).decode())
            offset = raw['offset']
            if type(offset) is not int or offset < 0 or offset > len(items) or raw['query'] != digest:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise Error('InvalidNextToken', 'Invalid or mismatched pagination token')
    output = {**result, key: items[offset:offset + size]}
    if offset + size < len(items):
        output['NextToken'] = base64.urlsafe_b64encode(json.dumps({'query': digest, 'offset': offset + size}).encode()).decode()
    return output


class Infrastructure(Networking):
    def __init__(self, ec2):
        self.ec2, self.engine = ec2, ec2.engine

    def store(self, data):
        resources = data.setdefault('ec2', {name: {} for name, _, _, _ in COLLECTIONS.values()})
        if not resources['vpcs'] and not resources.get('initialized'):
            resources['initialized'] = True
            vpc = self.vpc('172.31.0.0/16', default=True)
            resources['vpcs'][vpc['VpcId']] = vpc
            self.defaults(resources, vpc)
            for index, zone in enumerate(('a', 'b', 'c')):
                subnet = self.subnet(vpc['VpcId'], f'172.31.{index * 16}.0/20', REGION + zone, default=True)
                resources['subnets'][subnet['SubnetId']] = subnet
        return resources

    def vpc(self, block, default=False):
        network = cidr(block)
        if not 16 <= network.prefixlen <= 28:
            raise Error('InvalidVpc.Range', 'VPC IPv4 prefix must be /16 through /28')
        return {'VpcId': identifier('vpc'), 'CidrBlock': str(network), 'State': 'available', 'IsDefault': default,
                'OwnerId': ACCOUNT, 'InstanceTenancy': 'default', 'DhcpOptionsId': 'default', 'Tags': [],
                'CidrBlockAssociationSet': [{'AssociationId': identifier('vpc-cidr-assoc'), 'CidrBlock': str(network), 'CidrBlockState': {'State': 'associated'}}],
                'EnableDnsSupport': True, 'EnableDnsHostnames': default}

    def defaults(self, resources, vpc):
        group = self.group(vpc['VpcId'], 'default', 'default VPC security group')
        resources['groups'][group['GroupId']] = group
        table = self.route_table(vpc)
        table['Associations'] = [{'Main': True, 'RouteTableAssociationId': identifier('rtbassoc'), 'RouteTableId': table['RouteTableId'], 'AssociationState': {'State': 'associated'}}]
        resources['route_tables'][table['RouteTableId']] = table

    def subnet(self, vpc, block, zone, default=False):
        network = cidr(block)
        if zone not in [REGION + s for s in 'abcdef'] or not 16 <= network.prefixlen <= 28:
            raise Error('InvalidParameterValue', 'Unsupported availability zone or subnet prefix')
        key = identifier('subnet')
        return {'SubnetId': key, 'VpcId': vpc, 'CidrBlock': str(network), 'State': 'available', 'OwnerId': ACCOUNT,
                'AvailabilityZone': zone, 'AvailabilityZoneId': 'use1-az' + str(ZONE_IDS[zone[-1]]),
                'AvailableIpAddressCount': network.num_addresses - 5, 'DefaultForAz': default,
                'MapPublicIpOnLaunch': default, 'AssignIpv6AddressOnCreation': False,
                'Ipv6CidrBlockAssociationSet': [], 'Ipv6Native': False, 'Tags': [], 'SubnetArn': arn('subnet', key)}

    def group(self, vpc, name, description):
        key = identifier('sg')
        return {'GroupId': key, 'VpcId': vpc, 'GroupName': name, 'Description': description,
                'OwnerId': ACCOUNT, 'Tags': [], 'SecurityGroupArn': arn('security-group', key), 'IpPermissions': [],
                'IpPermissionsEgress': [{'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}], 'Ipv6Ranges': [], 'UserIdGroupPairs': [], 'PrefixListIds': []}]}

    def route_table(self, vpc):
        return {'RouteTableId': identifier('rtb'), 'VpcId': vpc['VpcId'], 'OwnerId': ACCOUNT, 'Tags': [], 'Associations': [],
                'Routes': [{'DestinationCidrBlock': vpc['CidrBlock'], 'GatewayId': 'local', 'Origin': 'CreateRouteTable', 'State': 'active'}]}

    def get(self, resources, kind, key):
        collection, field, prefix, error = COLLECTIONS[kind]
        if not isinstance(key, str) or key not in resources[collection]:
            raise Error(error, f'{kind} {key!r} does not exist')
        return resources[collection][key]

    def active(self, data):
        return [i for i in data.get('instances', {}).values() if i['State']['Name'] != 'terminated']

    def instance(self, data, key):
        if not isinstance(key, str) or key not in data.get('instances', {}):
            raise Error('InvalidInstanceID.NotFound', f'The instance ID {key!r} does not exist')
        return data['instances'][key]

    def select(self, resources, kind, args):
        collection, field, _, _ = COLLECTIONS[kind]
        ids = args.get(field + 's', [])
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise Error('InvalidParameterValue', 'Expected an identifier list')
        return [self.get(resources, kind, key) for key in ids] if ids else list(resources[collection].values())

    def filter(self, values, filters):
        # AWS ANDs filters and ORs values. No silent unsupported-filter matches.
        if not isinstance(filters, list) or len(filters) > 30:
            raise Error('InvalidParameterValue', 'Expected a bounded filter list')
        aliases = {'cidr-block': 'CidrBlock', 'cidr': 'CidrBlock', 'vpc-id': 'VpcId', 'subnet-id': 'SubnetId',
                   'group-id': 'GroupId', 'group-name': 'GroupName', 'state': 'State', 'status': 'State',
                   'volume-id': 'VolumeId', 'snapshot-id': 'SnapshotId', 'availability-zone': 'AvailabilityZone',
                   'is-default': 'IsDefault', 'route-table-id': 'RouteTableId', 'internet-gateway-id': 'InternetGatewayId',
                   'attachment.vpc-id': 'Attachments.VpcId', 'attachment.instance-id': 'Attachment.InstanceId',
                   'attachment.attachment-id': 'Attachment.AttachmentId', 'attachment.device-index': 'Attachment.DeviceIndex',
                   'addresses.private-ip-address': 'PrivateIpAddresses.PrivateIpAddress', 'private-ip-address': 'PrivateIpAddress',
                   'association.public-ip': 'Association.PublicIp', 'allocation-id': 'AllocationId', 'association-id': 'AssociationId', 'public-ip': 'PublicIp',
                   'key-name': 'KeyName', 'network-interface-id': 'NetworkInterfaceId', 'owner-id': 'OwnerId', 'instance-type': 'InstanceType',
                   'zone-name': 'ZoneName', 'zone-id': 'ZoneId', 'region-name': 'RegionName'}
        result = list(values)
        def walk(item, path):
            if isinstance(item, list):
                return [v for child in item for v in walk(child, path)]
            if not path:
                return [str(item).lower() if isinstance(item, bool) else str(item)]
            return walk(item.get(path[0], ''), path[1:]) if isinstance(item, dict) else []
        for entry in filters:
            if (not isinstance(entry, dict) or set(entry) != {'Name', 'Values'} or not isinstance(entry['Name'], str)
                    or not isinstance(entry['Values'], list) or not 1 <= len(entry['Values']) <= 100
                    or any(not isinstance(v, str) or len(v) > 256 for v in entry['Values'])):
                raise Error('InvalidParameterValue', 'Invalid filter')
            name = entry['Name']
            if name not in aliases and not name.startswith('tag:'):
                raise Error('InternalFailure', 'Unsupported filter: ' + name, 501)
            def matches(item):
                candidates = [t['Value'] for t in item.get('Tags', item.get('TagSet', [])) if t['Key'] == name[4:]] if name.startswith('tag:') else walk(item, ('Status' if name == 'status' and 'Status' in item else 'Attachments.InstanceId' if name == 'attachment.instance-id' and 'Attachments' in item else aliases[name]).split('.'))
                return any(wildcard(v, p) for v in candidates for p in entry['Values'])
            result = [item for item in result if matches(item)]
        return result

    def call(self, operation, args, principal):
        if not isinstance(args, dict) or set(args) - PARAMETERS[operation]:
            raise Error('UnsupportedOperation', 'Unsupported parameters for ' + operation, 501)
        validate_input(operation, args)
        def public(value):
            if isinstance(value, dict):
                return {k: public(v) for k, v in value.items() if not k.startswith('_')}
            return [public(v) for v in value] if isinstance(value, list) else value
        return self.engine.tools.call('ec2', operation, principal, '*', lambda: public(self.perform(operation, args, principal)))

    def replay(self, collection, args):
        token = args.get('ClientToken')
        if token is None:
            return None
        if not isinstance(token, str) or not 1 <= len(token) <= 64:
            raise Error('InvalidParameterValue', 'Invalid ClientToken')
        digest = hashlib.sha256(json.dumps({k: v for k, v in args.items() if k != 'ClientToken'}, sort_keys=True).encode()).hexdigest()
        for item in collection.values():
            if item.get('_client_token') == token:
                if item.get('_request_hash') != digest:
                    raise Error('IdempotentParameterMismatch', 'ClientToken was used with different parameters')
                return item
        return None

    def token_fields(self, args):
        if args.get('ClientToken'):
            return {'_client_token': args['ClientToken'], '_request_hash': hashlib.sha256(json.dumps({k: v for k, v in args.items() if k != 'ClientToken'}, sort_keys=True).encode()).hexdigest()}
        return {}

    def resource_tags(self, operation, args, principal):
        from .ec2 import validated_tags
        if set(args) != {'Resources', 'Tags'} or not isinstance(args['Resources'], list) or not 1 <= len(args['Resources']) <= 100:
            raise Error('InvalidParameterValue', 'Expected bounded Resources and Tags')
        tags = validated_tags(args['Tags'], delete=operation == 'DeleteTags')
        def change():
            with self.engine.transaction() as data:
                r = self.store(data)
                targets = []
                for key in args['Resources']:
                    if not isinstance(key, str):
                        raise Error('InvalidParameterValue', 'Invalid resource identifier')
                    if key.startswith('ami-'):
                        target, kind, field = ImageLifecycle(self).tagtarget(data, key), 'image', 'Tags'
                    elif key.startswith('lt-'):
                        target = r.get('launch_templates', {}).get(key)
                        if target is None:
                            raise Error('InvalidLaunchTemplateId.NotFound', 'Launch template does not exist')
                        kind, field = 'launch-template', 'Tags'
                    elif key.startswith('i-'):
                        target, kind, field = self.instance(data, key), 'instance', 'Tags'
                    else:
                        entry = next(((k, values) for k, values in COLLECTIONS.items() if key in r[values[0]]), None)
                        if not entry:
                            raise Error('InvalidID', 'Resource not found: ' + key)
                        name, (collection, _, _, _) = entry
                        target = r[collection][key]
                        kind = {'SecurityGroup': 'security-group', 'InternetGateway': 'internet-gateway', 'RouteTable': 'route-table', 'KeyPair': 'key-pair', 'NetworkInterface': 'network-interface', 'Address': 'elastic-ip'}.get(name, name.lower())
                        field = 'TagSet' if name == 'NetworkInterface' else 'Tags'
                    self.engine.authorize(data, principal, 'ec2:' + operation, arn(kind, key))
                    targets.append((target, field))
                for target, field in targets:
                    current = {t['Key']: t['Value'] for t in target.get(field, [])}
                    for tag in tags:
                        if operation == 'CreateTags':
                            current[tag['Key']] = tag['Value']
                        elif 'Value' not in tag or current.get(tag['Key']) == tag['Value']:
                            current.pop(tag['Key'], None)
                    if len(current) > 50:
                        raise Error('TagLimitExceeded', 'At most 50 tags are supported')
                    target[field] = [{'Key': key, 'Value': value} for key, value in current.items()]
                return {}
        return self.engine.tools.call('ec2', operation, principal, '*', change)

    def perform(self, operation, args, principal):
        with self.engine.transaction() as data:
            self.engine.authorize(data, principal, 'ec2:' + operation, '*')
            r = self.store(data)
            if operation in STORAGE_PARAMETERS:
                return StorageExtension(self).perform(data, r, operation, args)
            if operation in IMAGE_PARAMETERS:
                return ImageLifecycle(self).perform(operation, args, data, principal)
            if operation == 'CreateVpc':
                if args.get('InstanceTenancy', 'default') != 'default':
                    raise Error('InvalidParameterValue', 'Only default tenancy is supported')
                item = self.vpc(args.get('CidrBlock'))
                item['Tags'] = tag_specs(args.get('TagSpecifications'), 'vpc')
                r['vpcs'][item['VpcId']] = item
                self.defaults(r, item)
                return {'Vpc': copy.deepcopy(item)}
            if operation == 'DeleteVpc':
                item = self.get(r, 'Vpc', args.get('VpcId'))
                if any(s['VpcId'] == item['VpcId'] for s in r['subnets'].values()) or any(a.get('VpcId') == item['VpcId'] for g in r['gateways'].values() for a in g['Attachments']):
                    raise Error('DependencyViolation', 'Delete subnets and detach gateways first')
                if any(i.get('VpcId') == item['VpcId'] for i in self.active(data)):
                    raise Error('DependencyViolation', 'VPC has active instances')
                if any(n.get('VpcId') == item['VpcId'] for n in r['interfaces'].values()) or any(g['VpcId'] == item['VpcId'] and g['GroupName'] != 'default' for g in r['groups'].values()) or any(t['VpcId'] == item['VpcId'] and not any(a.get('Main') for a in t['Associations']) for t in r['route_tables'].values()):
                    raise Error('DependencyViolation', 'Delete network interfaces, custom security groups and custom route tables first')
                self.ec2.runtime.delete_network(item['VpcId'])
                del r['vpcs'][item['VpcId']]
                for collection in ('groups', 'route_tables'):
                    r[collection] = {k: v for k, v in r[collection].items() if v['VpcId'] != item['VpcId']}
                return {}
            if operation in ('DescribeVpcAttribute', 'ModifyVpcAttribute'):
                item = self.get(r, 'Vpc', args.get('VpcId'))
                if operation == 'DescribeVpcAttribute':
                    key = {'enableDnsSupport': 'EnableDnsSupport', 'enableDnsHostnames': 'EnableDnsHostnames'}.get(args.get('Attribute'))
                    if not key:
                        raise Error('InvalidParameterValue', 'Unsupported VPC attribute')
                    return {'VpcId': item['VpcId'], key: {'Value': item[key]}}
                for key in ('EnableDnsSupport', 'EnableDnsHostnames'):
                    if key in args:
                        if not isinstance(args[key], dict) or set(args[key]) != {'Value'}:
                            raise Error('InvalidParameterValue', 'Expected attribute Value')
                        item[key] = boolean(args[key]['Value'])
                return {}
            if operation == 'CreateSubnet':
                vpc = self.get(r, 'Vpc', args.get('VpcId'))
                network = cidr(args.get('CidrBlock'))
                if not network.subnet_of(cidr(vpc['CidrBlock'])):
                    raise Error('InvalidSubnet.Range', 'Subnet must be inside the VPC CIDR')
                if any(s['VpcId'] == vpc['VpcId'] and cidr(s['CidrBlock']).overlaps(network) for s in r['subnets'].values()):
                    raise Error('InvalidSubnet.Conflict', f"The CIDR '{network}' conflicts with another subnet")
                item = self.subnet(vpc['VpcId'], str(network), args.get('AvailabilityZone', REGION + 'a'))
                item['Tags'] = tag_specs(args.get('TagSpecifications'), 'subnet')
                r['subnets'][item['SubnetId']] = item
                return {'Subnet': copy.deepcopy(item)}
            if operation in ('DeleteSubnet', 'ModifySubnetAttribute'):
                item = self.get(r, 'Subnet', args.get('SubnetId'))
                if operation == 'ModifySubnetAttribute':
                    value = args.get('MapPublicIpOnLaunch', {})
                    if not isinstance(value, dict) or set(value) != {'Value'}:
                        raise Error('InvalidParameterValue', 'Expected MapPublicIpOnLaunch.Value')
                    item['MapPublicIpOnLaunch'] = boolean(value['Value'])
                    return {}
                if any(i.get('SubnetId') == item['SubnetId'] for i in self.active(data)) or any(i['SubnetId'] == item['SubnetId'] for i in r['interfaces'].values()):
                    raise Error('DependencyViolation', 'Subnet is in use')
                for table in r['route_tables'].values():
                    table['Associations'] = [a for a in table['Associations'] if a.get('SubnetId') != item['SubnetId']]
                del r['subnets'][item['SubnetId']]
                return {}
            if operation == 'CreateSecurityGroup':
                vpc = self.get(r, 'Vpc', args.get('VpcId') or next(k for k, v in r['vpcs'].items() if v['IsDefault']))
                name, description = text(args.get('GroupName')), text(args.get('Description'))
                if any(g['VpcId'] == vpc['VpcId'] and g['GroupName'] == name for g in r['groups'].values()):
                    raise Error('InvalidGroup.Duplicate', 'Security group already exists')
                item = self.group(vpc['VpcId'], name, description)
                item['Tags'] = tag_specs(args.get('TagSpecifications'), 'security-group')
                r['groups'][item['GroupId']] = item
                return {'GroupId': item['GroupId'], 'SecurityGroupArn': item['SecurityGroupArn']}
            if operation == 'DeleteSecurityGroup' or 'SecurityGroupIngress' in operation or 'SecurityGroupEgress' in operation:
                groups = [g for g in r['groups'].values() if g['GroupName'] == args.get('GroupName')]
                group = self.get(r, 'SecurityGroup', args.get('GroupId') or (groups[0]['GroupId'] if len(groups) == 1 else None))
                if operation == 'DeleteSecurityGroup':
                    if group['GroupName'] == 'default' or any(group['GroupId'] in [g['GroupId'] for g in i.get('SecurityGroups', [])] for i in self.active(data)) or any(group['GroupId'] in [g['GroupId'] for g in i.get('Groups', [])] for i in r['interfaces'].values()):
                        raise Error('DependencyViolation', 'Security group is in use or is the default')
                    del r['groups'][group['GroupId']]
                    return {}
                return self.permissions(group, operation, args)
            if operation == 'CreateInternetGateway':
                item = {'InternetGatewayId': identifier('igw'), 'OwnerId': ACCOUNT, 'Attachments': [], 'Tags': tag_specs(args.get('TagSpecifications'), 'internet-gateway')}
                r['gateways'][item['InternetGatewayId']] = item
                return {'InternetGateway': copy.deepcopy(item)}
            if operation in ('AttachInternetGateway', 'DetachInternetGateway', 'DeleteInternetGateway'):
                item = self.get(r, 'InternetGateway', args.get('InternetGatewayId'))
                if operation == 'DeleteInternetGateway':
                    if item['Attachments']:
                        raise Error('DependencyViolation', 'Detach the gateway first')
                    del r['gateways'][item['InternetGatewayId']]
                elif operation == 'AttachInternetGateway':
                    vpc = self.get(r, 'Vpc', args.get('VpcId'))
                    if item['Attachments'] or any(a['VpcId'] == vpc['VpcId'] for g in r['gateways'].values() for a in g['Attachments']):
                        raise Error('Resource.AlreadyAssociated', 'VPC or gateway already has an attachment')
                    item['Attachments'] = [{'VpcId': vpc['VpcId'], 'State': 'available'}]
                else:
                    if not any(a['VpcId'] == args.get('VpcId') for a in item['Attachments']):
                        raise Error('Gateway.NotAttached', 'Gateway is not attached to that VPC')
                    item['Attachments'] = []
                return {}
            if operation == 'CreateRouteTable':
                replay = self.replay(r['route_tables'], args)
                if replay:
                    return {'RouteTable': copy.deepcopy(replay)}
                item = self.route_table(self.get(r, 'Vpc', args.get('VpcId')))
                item.update(self.token_fields(args))
                item['Tags'] = tag_specs(args.get('TagSpecifications'), 'route-table')
                r['route_tables'][item['RouteTableId']] = item
                return {'RouteTable': copy.deepcopy(item)}
            if operation in ('CreateRoute', 'ReplaceRoute', 'DeleteRoute', 'DeleteRouteTable', 'AssociateRouteTable', 'DisassociateRouteTable'):
                return self.routes(r, operation, args)
            if operation in ('CreateVolume', 'ModifyVolume', 'DeleteVolume', 'AttachVolume', 'DetachVolume', 'CreateSnapshot', 'DeleteSnapshot', 'DescribeVolumesModifications'):
                return self.storage(data, r, operation, args)
            if operation in ('CreateKeyPair', 'ImportKeyPair', 'DeleteKeyPair', 'DescribeKeyPairs'):
                return self.keys(r, operation, args)
            if operation == 'DescribeAvailabilityZones':
                zones = [{'ZoneName': REGION + c, 'ZoneId': 'use1-az' + str(ZONE_IDS[c]), 'RegionName': REGION, 'State': 'available', 'ZoneType': 'availability-zone', 'Messages': []} for c in 'abcdef' if (not args.get('ZoneNames') or REGION + c in args['ZoneNames']) and (not args.get('ZoneIds') or 'use1-az' + str(ZONE_IDS[c]) in args['ZoneIds'])]
                return {'AvailabilityZones': self.filter(zones, args.get('Filters', []))}
            if operation == 'DescribeInstanceTypes':
                types = args.get('InstanceTypes', ['t3.micro', 't3.small', 't3.medium'])
                if not isinstance(types, list) or any(t not in ('t3.micro', 't3.small', 't3.medium') for t in types):
                    raise Error('InvalidInstanceType', 'Only configured local presets are supported')
                values = [{'InstanceType': t, 'VCpuInfo': {'DefaultVCpus': 2}, 'MemoryInfo': {'SizeInMiB': {'t3.micro': 1024, 't3.small': 2048, 't3.medium': 4096}[t]}, 'ProcessorInfo': {'SupportedArchitectures': ['x86_64']}, 'SupportedUsageClasses': ['on-demand'], 'SupportedRootDeviceTypes': ['ebs'], 'SupportedVirtualizationTypes': ['hvm']} for t in types]
                return paginated({'InstanceTypes': self.filter(values, args.get('Filters', []))}, 'InstanceTypes', args)
            if operation in ('ModifyNetworkInterfaceAttribute', 'DescribeNetworkInterfaceAttribute'):
                return self.interface_attributes(data, r, operation, args)
            if operation in ('AssignPrivateIpAddresses', 'UnassignPrivateIpAddresses'):
                return self.private_addresses(data, r, operation, args)
            if operation in ('CreateNetworkInterface', 'AttachNetworkInterface', 'DetachNetworkInterface', 'DeleteNetworkInterface', 'AllocateAddress', 'AssociateAddress', 'DisassociateAddress', 'ReleaseAddress'):
                return self.interfaces(data, r, operation, args)
            plurals = {'DescribeVpcs': ('Vpc', 'Vpcs'), 'DescribeSubnets': ('Subnet', 'Subnets'), 'DescribeSecurityGroups': ('SecurityGroup', 'SecurityGroups'),
                       'DescribeInternetGateways': ('InternetGateway', 'InternetGateways'), 'DescribeRouteTables': ('RouteTable', 'RouteTables'),
                       'DescribeVolumes': ('Volume', 'Volumes'), 'DescribeSnapshots': ('Snapshot', 'Snapshots'),
                       'DescribeNetworkInterfaces': ('NetworkInterface', 'NetworkInterfaces'), 'DescribeAddresses': ('Address', 'Addresses')}
            kind, key = plurals[operation]
            values = self.select(r, kind, args)
            if operation == 'DescribeSnapshots' and args.get('OwnerIds') and not set(args['OwnerIds']) & {'self', ACCOUNT}:
                values = []
            if operation == 'DescribeAddresses' and args.get('PublicIps'):
                values = [v for v in values if v['PublicIp'] in args['PublicIps']]
            if operation == 'DescribeSubnets':
                for subnet in values:
                    used = {i.get('PrivateIpAddress') for i in self.active(data) if i.get('SubnetId') == subnet['SubnetId']} | {a['PrivateIpAddress'] for i in r['interfaces'].values() if i['SubnetId'] == subnet['SubnetId'] for a in i.get('PrivateIpAddresses', [{'PrivateIpAddress': i['PrivateIpAddress']}])}
                    subnet['AvailableIpAddressCount'] = cidr(subnet['CidrBlock']).num_addresses - 5 - len(used)
            if operation == 'DescribeSecurityGroups' and args.get('GroupNames'):
                names = args['GroupNames']
                if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                    raise Error('InvalidParameterValue', 'Expected group names')
                values = [v for v in values if v['GroupName'] in names]
                if any(n not in [v['GroupName'] for v in values] for n in names):
                    raise Error('InvalidGroup.NotFound', 'Security group does not exist')
            return paginated({key: copy.deepcopy(self.filter(values, args.get('Filters', [])))}, key, args)

    def permissions(self, group, operation, args):
        key = 'IpPermissionsEgress' if operation.endswith('Egress') else 'IpPermissions'
        permissions = args.get('IpPermissions')
        if permissions is None:
            permissions = [{'IpProtocol': args.get('IpProtocol'), 'FromPort': args.get('FromPort'), 'ToPort': args.get('ToPort'), 'IpRanges': [{'CidrIp': args.get('CidrIp', '0.0.0.0/0')}]}]
        if not isinstance(permissions, list) or not 1 <= len(permissions) <= 32:
            raise Error('InvalidParameterValue', 'Expected 1 through 32 permissions')
        result = []
        for permission in permissions:
            if not isinstance(permission, dict) or set(permission) - {'IpProtocol', 'FromPort', 'ToPort', 'IpRanges', 'Ipv6Ranges', 'UserIdGroupPairs', 'PrefixListIds'}:
                raise Error('InvalidParameterValue', 'Invalid permission fields')
            protocol = str(permission.get('IpProtocol'))
            protocol = {'6': 'tcp', '17': 'udp', '1': 'icmp'}.get(protocol, protocol)
            if protocol not in ('tcp', 'udp', 'icmp', '-1') or any(permission.get(k) for k in ('Ipv6Ranges', 'UserIdGroupPairs', 'PrefixListIds')):
                raise Error('UnsupportedOperation', 'Only IPv4 CIDR permissions are supported', 501)
            item = {'IpProtocol': protocol, 'IpRanges': [], 'Ipv6Ranges': [], 'UserIdGroupPairs': [], 'PrefixListIds': []}
            if protocol != '-1':
                item.update(FromPort=integer(permission.get('FromPort'), -1 if protocol == 'icmp' else 0, 65535), ToPort=integer(permission.get('ToPort'), -1 if protocol == 'icmp' else 0, 65535))
                if item['FromPort'] > item['ToPort']:
                    raise Error('InvalidParameterValue', 'Invalid port range')
            ranges = permission.get('IpRanges', [{'CidrIp': '0.0.0.0/0'}])
            if not isinstance(ranges, list) or not 1 <= len(ranges) <= 32:
                raise Error('InvalidParameterValue', 'Expected IPv4 CIDR ranges')
            for block in ranges:
                if not isinstance(block, dict) or set(block) - {'CidrIp', 'Description'}:
                    raise Error('InvalidParameterValue', 'Invalid IP range')
                item['IpRanges'].append({'CidrIp': str(cidr(block.get('CidrIp'))), **({'Description': text(block['Description'])} if 'Description' in block else {})})
            result.append(item)
        def identity(item):
            return (item['IpProtocol'], item.get('FromPort'), item.get('ToPort'), tuple(sorted(b['CidrIp'] for b in item['IpRanges'])))
        existing = {identity(item): item for item in group[key]}
        seen = set()
        for item in result:
            signature = identity(item)
            exists = signature in existing or signature in seen
            if operation.startswith('Authorize') and exists:
                raise Error('InvalidPermission.Duplicate', 'The specified rule already exists')
            if operation.startswith('Revoke') and not exists:
                raise Error('InvalidPermission.NotFound', 'The specified rule does not exist')
            if signature in seen:
                raise Error('InvalidPermission.Duplicate', 'Repeated rule in request')
            seen.add(signature)
        if operation.startswith('Authorize') and len(group[key]) + len(result) > 32:
            raise Error('RulesPerSecurityGroupLimitExceeded', 'Local limit is 32 rules')
        for item in result:
            if operation.startswith('Authorize'):
                group[key].append(item)
            else:
                group[key].remove(existing[identity(item)])
        return {'Return': True}

    def routes(self, r, operation, args):
        if operation == 'DisassociateRouteTable':
            for table in r['route_tables'].values():
                found = [a for a in table['Associations'] if a['RouteTableAssociationId'] == args.get('AssociationId')]
                if found:
                    if found[0]['Main']:
                        raise Error('InvalidParameterValue', 'Cannot disassociate main route table')
                    table['Associations'].remove(found[0])
                    return {}
            raise Error('InvalidAssociationID.NotFound', 'Route table association not found')
        table = self.get(r, 'RouteTable', args.get('RouteTableId'))
        if operation == 'DeleteRouteTable':
            if table['Associations']:
                raise Error('DependencyViolation', 'Disassociate route table first')
            del r['route_tables'][table['RouteTableId']]
            return {}
        if operation == 'AssociateRouteTable':
            subnet = self.get(r, 'Subnet', args.get('SubnetId'))
            if subnet['VpcId'] != table['VpcId']:
                raise Error('InvalidParameterValue', 'Subnet and route table must belong to the same VPC')
            if any(a.get('SubnetId') == subnet['SubnetId'] for t in r['route_tables'].values() for a in t['Associations']):
                raise Error('Resource.AlreadyAssociated', 'Subnet already has an explicit association')
            association = {'Main': False, 'RouteTableAssociationId': identifier('rtbassoc'), 'RouteTableId': table['RouteTableId'], 'SubnetId': subnet['SubnetId'], 'AssociationState': {'State': 'associated'}}
            table['Associations'].append(association)
            return {'AssociationId': association['RouteTableAssociationId']}
        destination = str(cidr(args.get('DestinationCidrBlock')))
        existing = next((v for v in table['Routes'] if v['DestinationCidrBlock'] == destination), None)
        if existing and existing.get('GatewayId') == 'local':
            raise Error('InvalidParameterValue', 'Cannot modify or delete local route')
        if operation == 'CreateRoute' and existing:
            raise Error('RouteAlreadyExists', 'Route already exists')
        if operation != 'CreateRoute' and not existing:
            raise Error('InvalidRoute.NotFound', 'Route does not exist')
        if operation == 'DeleteRoute':
            table['Routes'].remove(existing)
            return {}
        if bool(args.get('GatewayId')) + bool(args.get('NetworkInterfaceId')) != 1:
            raise Error('InvalidParameterValue', 'Choose one route target')
        route = {'DestinationCidrBlock': destination, 'State': 'active', 'Origin': 'CreateRoute'}
        if args.get('GatewayId'):
            gateway = self.get(r, 'InternetGateway', args['GatewayId'])
            if not any(a['VpcId'] == table['VpcId'] for a in gateway['Attachments']):
                raise Error('InvalidParameterValue', 'Gateway must be attached to the VPC')
            route['GatewayId'] = gateway['InternetGatewayId']
        else:
            interface = self.get(r, 'NetworkInterface', args['NetworkInterfaceId'])
            if interface['VpcId'] != table['VpcId']:
                raise Error('InvalidParameterValue', 'Interface must be in the VPC')
            route['NetworkInterfaceId'] = interface['NetworkInterfaceId']
        if existing:
            table['Routes'].remove(existing)
        table['Routes'].append(route)
        return {'Return': True}

    def storage(self, data, r, operation, args):
        if operation == 'CreateVolume':
            replay = self.replay(r['volumes'], args)
            if replay:
                return copy.deepcopy(replay)
            if args.get('AvailabilityZone') not in [REGION + s for s in 'abcdef']:
                raise Error('InvalidParameterValue', 'Unsupported availability zone')
            snapshot = self.get(r, 'Snapshot', args['SnapshotId']) if args.get('SnapshotId') else None
            size = integer(args.get('Size', snapshot['VolumeSize'] if snapshot else 1), 1, 1000)
            if snapshot and size < snapshot['VolumeSize']:
                raise Error('InvalidParameterValue', 'Volume cannot be smaller than snapshot')
            kind = args.get('VolumeType', 'gp2')
            if kind not in ('standard', 'gp2', 'gp3', 'io1', 'io2', 'st1', 'sc1'):
                raise Error('InvalidParameterValue', 'Unsupported volume type')
            encrypted = boolean(args.get('Encrypted', snapshot['Encrypted'] if snapshot else False))
            if self.ec2.images and encrypted:
                raise Error('UnsupportedOperation', 'Docker filesystem volumes do not provide at-rest encryption', 501)
            item = {'VolumeId': identifier('vol'), 'Size': size, 'AvailabilityZone': args['AvailabilityZone'], 'VolumeType': kind, 'State': 'available',
                    'SnapshotId': args.get('SnapshotId', ''), 'CreateTime': now(), 'Encrypted': encrypted, 'Attachments': [], 'Tags': tag_specs(args.get('TagSpecifications'), 'volume')}
            if kind in ('gp2', 'gp3', 'io1', 'io2'):
                item['Iops'] = integer(args.get('Iops', 3000 if kind == 'gp3' else max(100, size * 3)), 100, 64000)
            if kind == 'gp3':
                item['Throughput'] = integer(args.get('Throughput', 125), 125, 1000)
            self.ec2.runtime.create_volume(item['VolumeId'], snapshot['SnapshotId'] if snapshot else None)
            item.update(self.token_fields(args))
            r['volumes'][item['VolumeId']] = item
            response = copy.deepcopy(item)
            response['State'] = 'creating'
            response.pop('Attachments')
            return response
        if operation in ('CreateSnapshot', 'DeleteSnapshot'):
            if operation == 'DeleteSnapshot':
                snapshot = self.get(r, 'Snapshot', args.get('SnapshotId'))
                if any(not image.get('_deregistered') and any(m.get('Ebs', {}).get('SnapshotId') == snapshot['SnapshotId'] for m in image.get('BlockDeviceMappings', [])) for image in ImageLifecycle(self).catalogue(data).values()):
                    raise Error('DependencyViolation', 'Deregister images using this snapshot first')
                self.ec2.runtime.delete_volume(snapshot['SnapshotId'])
                del r['snapshots'][snapshot['SnapshotId']]
                return {}
            volume = self.get(r, 'Volume', args.get('VolumeId'))
            item = {'SnapshotId': identifier('snap'), 'VolumeId': volume['VolumeId'], 'VolumeSize': volume['Size'], 'State': 'completed', 'StartTime': now(),
                    'Progress': '100%', 'OwnerId': ACCOUNT, 'Description': args.get('Description', ''), 'Encrypted': volume['Encrypted'], 'Tags': tag_specs(args.get('TagSpecifications'), 'snapshot')}
            self.ec2.runtime.create_volume(item['SnapshotId'], volume['VolumeId'])
            r['snapshots'][item['SnapshotId']] = item
            return {**item, 'State': 'pending', 'Progress': '60%'}
        if operation == 'DescribeVolumesModifications':
            items = [v['Modification'] for v in self.select(r, 'Volume', args) if 'Modification' in v]
            return paginated({'VolumesModifications': copy.deepcopy(items)}, 'VolumesModifications', args)
        volume = self.get(r, 'Volume', args.get('VolumeId'))
        if operation == 'DeleteVolume':
            if volume['Attachments']:
                raise Error('VolumeInUse', 'Detach the volume before deleting it')
            self.ec2.runtime.delete_volume(volume['VolumeId'])
            del r['volumes'][volume['VolumeId']]
            return {}
        if operation == 'ModifyVolume':
            size = integer(args.get('Size', volume['Size']), volume['Size'], 1000)
            modification = {'VolumeId': volume['VolumeId'], 'OriginalSize': volume['Size'], 'TargetSize': size, 'ModificationState': 'completed', 'Progress': 100, 'StartTime': now()}
            volume['Size'] = size
            for key in ('VolumeType', 'Iops', 'Throughput'):
                if key in args:
                    if key == 'VolumeType' and args[key] not in ('standard', 'gp2', 'gp3', 'io1', 'io2', 'st1', 'sc1'):
                        raise Error('InvalidParameterValue', 'Unsupported volume type')
                    volume[key] = args[key] if key == 'VolumeType' else integer(args[key], 100 if key == 'Iops' else 125, 64000 if key == 'Iops' else 1000)
            volume['Modification'] = modification
            return {'VolumeModification': {**modification, 'ModificationState': 'modifying', 'Progress': 0}}
        if operation == 'AttachVolume':
            instance = self.instance(data, args.get('InstanceId'))
            if instance['State']['Name'] == 'terminated':
                raise Error('IncorrectInstanceState', 'Instance is terminated')
            if volume['AvailabilityZone'] != instance.get('Placement', {}).get('AvailabilityZone', REGION + 'a'):
                raise Error('InvalidVolume.ZoneMismatch', 'Volume and instance must be in the same availability zone')
            device = text(args.get('Device'))
            if not re.fullmatch(r'/(?:dev/[a-zA-Z0-9_-]+|mnt/[a-zA-Z0-9_-]+|data(?:/[a-zA-Z0-9_-]+)?)', device):
                raise Error('InvalidParameterValue', 'Use /dev/NAME, /mnt/NAME or /data[/NAME]')
            if volume['Attachments'] or any(m['DeviceName'] == device for m in instance.get('BlockDeviceMappings', [])):
                raise Error('VolumeInUse', 'Volume or device is already attached')
            self.ec2.runtime.check_reconfigure(instance)
            attachment = {'VolumeId': volume['VolumeId'], 'InstanceId': instance['InstanceId'], 'Device': device, 'State': 'attached', 'AttachTime': now()}
            mapping = {'DeviceName': device, 'Ebs': {'VolumeId': volume['VolumeId'], 'Status': 'attached', 'AttachTime': attachment['AttachTime'], 'DeleteOnTermination': False}}
            # Rebuild is completed before committing record changes; no live host mounts.
            self.ec2.runtime.reconfigure(instance, [*instance.get('BlockDeviceMappings', []), mapping])
            instance.setdefault('BlockDeviceMappings', []).append(mapping)
            volume.update(Attachments=[attachment], State='in-use')
            return {**attachment, 'State': 'attaching'}
        attachment = next((a for a in volume['Attachments'] if (not args.get('InstanceId') or a['InstanceId'] == args['InstanceId']) and (not args.get('Device') or a['Device'] == args['Device'])), None)
        if not attachment:
            raise Error('InvalidAttachment.NotFound', 'Volume attachment does not exist')
        instance = self.instance(data, attachment['InstanceId'])
        self.ec2.runtime.check_reconfigure(instance)
        mappings = [m for m in instance.get('BlockDeviceMappings', []) if m['Ebs']['VolumeId'] != volume['VolumeId']]
        self.ec2.runtime.reconfigure(instance, mappings)
        instance['BlockDeviceMappings'] = mappings
        volume.update(Attachments=[], State='available')
        return {**attachment, 'State': 'detaching'}

    def keys(self, r, operation, args):
        names = args.get('KeyNames', [])
        keys = [k for k in r['keys'].values() if (not names or k['KeyName'] in names) and (not args.get('KeyPairIds') or k['KeyPairId'] in args['KeyPairIds'])]
        if operation == 'DescribeKeyPairs':
            if any(n not in [k['KeyName'] for k in keys] for n in names):
                raise Error('InvalidKeyPair.NotFound', 'Key pair not found')
            return {'KeyPairs': copy.deepcopy([{k: v for k, v in key.items() if args.get('IncludePublicKey') or k != 'PublicKey'} for key in self.filter(keys, args.get('Filters', []))])}
        if operation == 'DeleteKeyPair':
            key = next((v for v in r['keys'].values() if v['KeyName'] == args.get('KeyName') or v['KeyPairId'] == args.get('KeyPairId')), None)
            if key:
                del r['keys'][key['KeyPairId']]
            return {'Return': True, **({'KeyPairId': key['KeyPairId']} if key else {})}
        name = text(args.get('KeyName'))
        if any(k['KeyName'] == name for k in r['keys'].values()):
            raise Error('InvalidKeyPair.Duplicate', 'Key pair already exists')
        kind = args.get('KeyType', 'rsa')
        private = None
        if operation == 'CreateKeyPair':
            if kind not in ('rsa', 'ed25519') or args.get('KeyFormat', 'pem') != 'pem':
                raise Error('UnsupportedOperation', 'Supported keys: rsa/ed25519 in PEM/OpenSSH format', 501)
            try:
                with tempfile.TemporaryDirectory(prefix='localcloud-key-') as directory:
                    path = Path(directory) / 'key'
                    subprocess.run(['ssh-keygen', '-q', '-t', kind, '-N', '', '-f', str(path), *(['-m', 'PEM'] if kind == 'rsa' else [])], check=True, capture_output=True, timeout=20)
                    private, public = path.read_text(), path.with_suffix('.pub').read_text().strip()
            except (OSError, subprocess.SubprocessError):
                raise Error('RuntimeUnavailable', 'ssh-keygen is required for key-pair creation', 503)
        else:
            try:
                public = base64.b64decode(args.get('PublicKeyMaterial', ''), validate=True).decode().strip()
                kind = {'ssh-rsa': 'rsa', 'ssh-ed25519': 'ed25519'}[public.split()[0]]
            except (ValueError, KeyError, IndexError, UnicodeError):
                raise Error('InvalidKey.Format', 'Expected base64 OpenSSH public key')
        try:
            key_bytes = base64.b64decode(public.split()[1], validate=True)
            if len(key_bytes) < 32 or len(key_bytes) > 16384:
                raise ValueError()
        except (IndexError, ValueError):
            raise Error('InvalidKey.Format', 'Invalid public key')
        digest = hashlib.sha256(key_bytes).hexdigest()
        key = {'KeyPairId': identifier('key'), 'KeyName': name, 'KeyType': kind, 'KeyFingerprint': ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2)),
               'PublicKey': public, 'CreateTime': now(), 'Tags': tag_specs(args.get('TagSpecifications'), 'key-pair')}
        r['keys'][key['KeyPairId']] = key
        return {k: v for k, v in {**key, **({'KeyMaterial': private} if private else {})}.items() if k not in ('PublicKey', 'Tags', 'CreateTime', 'KeyType')}

    def allocate_ip(self, data, resources, subnet, requested=None):
        network = cidr(subnet['CidrBlock'])
        used = {i.get('PrivateIpAddress') for i in self.active(data) if i.get('SubnetId') == subnet['SubnetId']} | {a['PrivateIpAddress'] for i in resources['interfaces'].values() if i['SubnetId'] == subnet['SubnetId'] for a in i.get('PrivateIpAddresses', [{'PrivateIpAddress': i['PrivateIpAddress']}])}
        if requested:
            try:
                address = ipaddress.IPv4Address(requested)
            except (ValueError, TypeError):
                raise Error('InvalidParameterValue', 'Invalid private IPv4 address')
            if address not in network or int(address) - int(network.network_address) < 4 or address == network.broadcast_address or str(address) in used:
                raise Error('InvalidIPAddress.InUse', 'Private address is reserved, unavailable or outside subnet')
            return str(address)
        for index in range(4, network.num_addresses - 1):
            address = str(network.network_address + index)
            if address not in used:
                return address
        raise Error('InsufficientFreeAddressesInSubnet', 'Subnet is full')

    def launch_network(self, data, args):
        r = self.store(data)
        subnet = self.get(r, 'Subnet', args['SubnetId']) if args.get('SubnetId') else next((s for s in r['subnets'].values() if s['DefaultForAz']), None)
        if not subnet:
            raise Error('InvalidSubnetID.NotFound', 'Specify a subnet after deleting the default subnets')
        groups = [self.get(r, 'SecurityGroup', key) for key in args.get('SecurityGroupIds', [])]
        if not groups:
            groups = [g for g in r['groups'].values() if g['VpcId'] == subnet['VpcId'] and g['GroupName'] == 'default']
        if any(g['VpcId'] != subnet['VpcId'] for g in groups):
            raise Error('InvalidGroup.NotFound', 'Security groups must belong to the instance VPC')
        address = self.allocate_ip(data, r, subnet, args.get('PrivateIpAddress'))
        if args.get('KeyName') and not any(k['KeyName'] == args['KeyName'] for k in r['keys'].values()):
            raise Error('InvalidKeyPair.NotFound', 'Key pair not found')
        return {'VpcId': subnet['VpcId'], 'SubnetId': subnet['SubnetId'], 'PrivateIpAddress': address,
                'PrivateDnsName': 'ip-' + address.replace('.', '-') + '.ec2.internal',
                'SecurityGroups': [{'GroupId': g['GroupId'], 'GroupName': g['GroupName']} for g in groups],
                'Placement': {'AvailabilityZone': subnet['AvailabilityZone'], 'Tenancy': 'default', 'GroupName': ''},
                **({'KeyName': args['KeyName']} if args.get('KeyName') else {})}

    def block_devices(self, data, instance, mappings):
        if not isinstance(mappings, list) or len(mappings) > 8:
            raise Error('InvalidParameterValue', 'At most eight block device mappings are supported')
        r = self.store(data)
        created, output, devices = [], [], set()
        try:
            for mapping in mappings:
                if not isinstance(mapping, dict) or set(mapping) != {'DeviceName', 'Ebs'} or not isinstance(mapping['Ebs'], dict):
                    raise Error('InvalidParameterValue', 'Expected DeviceName and Ebs')
                device, ebs = mapping['DeviceName'], mapping['Ebs']
                if not isinstance(device, str) or not re.fullmatch(r'/(?:dev/[a-zA-Z0-9_-]+|mnt/[a-zA-Z0-9_-]+|data(?:/[a-zA-Z0-9_-]+)?)', device) or device in devices:
                    raise Error('InvalidParameterValue', 'Invalid or duplicate block device mount path')
                devices.add(device)
                if set(ebs) - {'VolumeSize', 'SnapshotId', 'VolumeType', 'DeleteOnTermination', 'Encrypted', 'Iops', 'Throughput'}:
                    raise Error('UnsupportedOperation', 'Unsupported Ebs mapping fields', 501)
                delete = boolean(ebs.get('DeleteOnTermination', True))
                args = {k: v for k, v in ebs.items() if k not in ('VolumeSize', 'DeleteOnTermination')}
                args.update(AvailabilityZone=instance['Placement']['AvailabilityZone'])
                if 'VolumeSize' in ebs:
                    args['Size'] = ebs['VolumeSize']
                result = self.storage(data, r, 'CreateVolume', args)
                key = result['VolumeId']
                created.append(key)
                attachment = {'VolumeId': key, 'InstanceId': instance['InstanceId'], 'Device': device, 'State': 'attached', 'AttachTime': now()}
                r['volumes'][key].update(Attachments=[attachment], State='in-use')
                output.append({'DeviceName': device, 'Ebs': {'VolumeId': key, 'Status': 'attached', 'AttachTime': attachment['AttachTime'], 'DeleteOnTermination': delete}})
            return output, created
        except Error:
            for key in created:
                self.ec2.runtime.delete_volume(key)
                r['volumes'].pop(key, None)
            raise

    def published_ingress(self, data, instance, existing):
        r = self.store(data)
        ports = {p['ContainerPort'] for p in existing}
        output = list(existing)
        for selected in instance['SecurityGroups']:
            group = self.get(r, 'SecurityGroup', selected['GroupId'])
            for rule in group['IpPermissions']:
                if rule['IpProtocol'] != 'tcp':
                    continue
                if rule['ToPort'] - rule['FromPort'] > 31:
                    raise Error('InvalidParameterValue', 'Docker ingress publication supports at most 32 individual ports')
                for port in range(max(1, rule['FromPort']), rule['ToPort'] + 1):
                    if port not in ports:
                        output.append({'HostPort': 0, 'ContainerPort': port})
                        ports.add(port)
        if len(output) > 32:
            raise Error('InvalidParameterValue', 'Docker ingress publication supports at most 32 ports')
        return output

    def terminate(self, data, instance):
        r = self.store(data)
        for volume in list(r['volumes'].values()):
            attachment = next((a for a in volume['Attachments'] if a['InstanceId'] == instance['InstanceId']), None)
            if attachment:
                volume.update(Attachments=[], State='available')
                mapping = next((m for m in instance.get('BlockDeviceMappings', []) if m['Ebs']['VolumeId'] == volume['VolumeId']), None)
                if mapping and mapping['Ebs'].get('DeleteOnTermination'):
                    self.ec2.runtime.delete_volume(volume['VolumeId'])
                    del r['volumes'][volume['VolumeId']]
        for interface in list(r['interfaces'].values()):
            if interface.get('Attachment', {}).get('InstanceId') == instance['InstanceId']:
                if interface['Attachment'].get('DeleteOnTermination'):
                    del r['interfaces'][interface['NetworkInterfaceId']]
                else:
                    interface.pop('Attachment')
                    interface['Status'] = 'available'
        for address in r['addresses'].values():
            if address.get('InstanceId') == instance['InstanceId']:
                self.clear_association(data, r, address)

    def primary_interface(self, data, instance):
        r = self.store(data)
        key = identifier('eni')
        item = {'NetworkInterfaceId': key, 'SubnetId': instance['SubnetId'], 'VpcId': instance['VpcId'], 'OwnerId': ACCOUNT,
                'PrivateIpAddress': instance['PrivateIpAddress'], 'PrivateDnsName': instance['PrivateDnsName'], 'Description': 'Primary network interface',
                'AvailabilityZone': instance['Placement']['AvailabilityZone'], 'InterfaceType': 'interface', 'RequesterManaged': False,
                'Status': 'in-use', 'SourceDestCheck': True, 'Groups': copy.deepcopy(instance['SecurityGroups']), 'TagSet': [],
                'MacAddress': '02:00:' + ':'.join(uuid.uuid4().hex[i:i + 2] for i in range(0, 8, 2)),
                'PrivateIpAddresses': [{'PrivateIpAddress': instance['PrivateIpAddress'], 'Primary': True}],
                'Attachment': {'AttachmentId': identifier('eni-attach'), 'InstanceId': instance['InstanceId'], 'DeviceIndex': 0, 'Status': 'attached', 'DeleteOnTermination': True, 'AttachTime': now()}}
        r['interfaces'][key] = item
        instance['NetworkInterfaces'] = [self.interface_view(item)]

    def interface_view(self, item):
        from .ec2_protocol import MODEL
        shape = MODEL['shapes'][MODEL['shapes']['Instance']['members']['NetworkInterfaces']['shape']]['member']['shape']
        def view(value, name):
            definition = MODEL['shapes'][name]
            if definition['type'] == 'structure':
                return {key: view(child, definition['members'][key]['shape']) for key, child in value.items() if key in definition['members']}
            if definition['type'] == 'list':
                return [view(child, definition['member']['shape']) for child in value]
            return value
        return view(copy.deepcopy(item), shape)

    def interfaces(self, data, r, operation, args):
        if operation == 'CreateNetworkInterface':
            replay = self.replay(r['interfaces'], args)
            if replay:
                return {'NetworkInterface': copy.deepcopy(replay)}
            subnet = self.get(r, 'Subnet', args.get('SubnetId'))
            address = self.allocate_ip(data, r, subnet, args.get('PrivateIpAddress'))
            groups = [self.get(r, 'SecurityGroup', g) for g in args.get('Groups', [])] or [g for g in r['groups'].values() if g['VpcId'] == subnet['VpcId'] and g['GroupName'] == 'default']
            if any(g['VpcId'] != subnet['VpcId'] for g in groups):
                raise Error('InvalidGroup.NotFound', 'Interface groups must belong to the subnet VPC')
            key = identifier('eni')
            item = {'NetworkInterfaceId': key, 'SubnetId': subnet['SubnetId'], 'VpcId': subnet['VpcId'], 'AvailabilityZone': subnet['AvailabilityZone'],
                    'PrivateIpAddress': address, 'PrivateIpAddresses': [{'PrivateIpAddress': address, 'Primary': True}], 'Description': args.get('Description', ''),
                    'InterfaceType': 'interface', 'RequesterManaged': False, 'MacAddress': '02:00:' + ':'.join(uuid.uuid4().hex[i:i+2] for i in range(0, 8, 2)),
                    'Status': 'available', 'OwnerId': ACCOUNT, 'SourceDestCheck': True, 'Groups': [{'GroupId': g['GroupId'], 'GroupName': g['GroupName']} for g in groups],
                    'TagSet': tag_specs(args.get('TagSpecifications'), 'network-interface')}
            r['interfaces'][key] = item
            item.update(self.token_fields(args))
            return {'NetworkInterface': copy.deepcopy(item)}
        if operation in ('AttachNetworkInterface', 'DetachNetworkInterface', 'DeleteNetworkInterface'):
            if operation == 'DetachNetworkInterface':
                interface = next((i for i in r['interfaces'].values() if i.get('Attachment', {}).get('AttachmentId') == args.get('AttachmentId')), None)
                if not interface:
                    raise Error('InvalidAttachmentID.NotFound', 'Interface attachment not found')
                if interface['Attachment']['DeviceIndex'] == 0:
                    raise Error('OperationNotPermitted', 'Cannot detach the primary network interface')
                instance = self.instance(data, interface['Attachment']['InstanceId'])
                interface.pop('Attachment')
                interface['Status'] = 'available'
                self.sync_network_views(data, r, interface)
                instance['NetworkInterfaces'] = [self.interface_view(i) for i in r['interfaces'].values() if i.get('Attachment', {}).get('InstanceId') == instance['InstanceId']]
                return {}
            interface = self.get(r, 'NetworkInterface', args.get('NetworkInterfaceId'))
            if interface.get('Attachment'):
                raise Error('InvalidNetworkInterface.InUse', 'Interface is attached')
            if operation == 'DeleteNetworkInterface':
                if any(a.get('NetworkInterfaceId') == interface['NetworkInterfaceId'] for a in r['addresses'].values()):
                    raise Error('InvalidNetworkInterface.InUse', 'Disassociate elastic addresses first')
                del r['interfaces'][interface['NetworkInterfaceId']]
                return {}
            instance = self.instance(data, args.get('InstanceId'))
            if interface['VpcId'] != instance.get('VpcId') or instance['State']['Name'] == 'terminated':
                raise Error('InvalidParameterValue', 'Instance and interface must be active in the same VPC')
            if interface.get('AvailabilityZone') != instance['Placement']['AvailabilityZone']:
                raise Error('InvalidParameterValue', 'Interface and instance must be in the same availability zone')
            index = integer(args.get('DeviceIndex'), 1, 7)
            if any(i.get('Attachment', {}).get('InstanceId') == instance['InstanceId'] and i['Attachment']['DeviceIndex'] == index for i in r['interfaces'].values()):
                raise Error('InvalidParameterValue', 'Device index is already occupied')
            attachment = {'AttachmentId': identifier('eni-attach'), 'InstanceId': instance['InstanceId'], 'DeviceIndex': index, 'Status': 'attached', 'DeleteOnTermination': False, 'AttachTime': now()}
            interface.update(Attachment=attachment, Status='in-use')
            self.sync_network_views(data, r, interface)
            return {'AttachmentId': attachment['AttachmentId']}
        if operation == 'AllocateAddress':
            if args.get('Domain', 'vpc') != 'vpc':
                raise Error('UnsupportedOperation', 'Only VPC elastic address records are supported', 501)
            used = {a['PublicIp'] for a in r['addresses'].values()}
            address = next(('127.240.' + str(i // 254) + '.' + str(i % 254 + 1) for i in range(4096) if '127.240.' + str(i // 254) + '.' + str(i % 254 + 1) not in used), None)
            if address is None:
                raise Error('AddressLimitExceeded', 'Local address pool exhausted')
            item = {'AllocationId': identifier('eipalloc'), 'PublicIp': address, 'Domain': 'vpc', 'PublicIpv4Pool': 'amazon', 'Tags': tag_specs(args.get('TagSpecifications'), 'elastic-ip')}
            r['addresses'][item['AllocationId']] = item
            return copy.deepcopy(item)
        address = self.get(r, 'Address', args['AllocationId']) if args.get('AllocationId') else next((a for a in r['addresses'].values() if a.get('AssociationId') == args.get('AssociationId') or a['PublicIp'] == args.get('PublicIp')), None)
        if not address:
            raise Error('InvalidAddress.NotFound', 'Elastic address not found')
        if operation == 'AssociateAddress':
            return self.associate_address(data, r, args, address)
        if operation == 'ReleaseAddress':
            if address.get('AssociationId'):
                raise Error('InvalidIPAddress.InUse', 'Disassociate address before releasing')
            del r['addresses'][address['AllocationId']]
        else:
            self.clear_association(data, r, address)
        return {}


def validate_model(value, shape_name):
    from .ec2_protocol import MODEL
    shape = MODEL['shapes'][shape_name]
    kind = shape['type']
    if kind == 'structure':
        if not isinstance(value, dict) or set(value) - set(shape['members']):
            raise ValueError('Unexpected EC2 resource fields')
        for key, child in value.items():
            validate_model(child, shape['members'][key]['shape'])
    elif kind == 'list':
        if not isinstance(value, list) or len(value) > 1000:
            raise ValueError('Invalid EC2 list')
        for child in value:
            validate_model(child, shape['member']['shape'])
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('Invalid EC2 boolean')
    elif kind in ('integer', 'long'):
        if type(value) is not int:
            raise ValueError('Invalid EC2 integer')
    elif kind in ('string', 'timestamp', 'blob'):
        if not isinstance(value, str) or len(value) > 32768:
            raise ValueError('Invalid EC2 text')
    elif kind in ('float', 'double'):
        if type(value) not in (int, float):
            raise ValueError('Invalid EC2 number')


def validate_input(operation, args):
    from .ec2_protocol import MODEL
    if operation not in MODEL['operations']:
        return
    shape = MODEL['shapes'][MODEL['operations'][operation]['input']]
    try:
        for key, value in args.items():
            if key in shape['members']:
                if operation == 'ModifyInstanceAttribute' and key == 'InstanceType' and isinstance(value, str):
                    # Management API and Query adapter use the local preset string.
                    value = {'Value': value}
                validate_model(value, shape['members'][key]['shape'])
    except (ValueError, TypeError, KeyError) as error:
        raise Error('InvalidParameterValue', str(error)) from error


def validate_instance_fields(item):
    from .ec2_protocol import MODEL
    fields = MODEL['shapes']['Instance']['members']
    for key in ('VpcId', 'SubnetId', 'PrivateIpAddress', 'PrivateDnsName', 'PublicIpAddress', 'SecurityGroups', 'Placement', 'KeyName', 'MetadataOptions', 'BlockDeviceMappings', 'NetworkInterfaces', 'LaunchTemplate'):
        if key in item:
            validate_model(item[key], fields[key]['shape'])


def validate_infrastructure(resources):
    required_collections = {'initialized', *[v[0] for v in COLLECTIONS.values()]}
    if not isinstance(resources, dict) or not required_collections <= set(resources) or set(resources) - required_collections - {'images', 'launch_templates'}:
        raise ValueError('Invalid EC2 infrastructure state')
    if type(resources.get('initialized', False)) is not bool:
        raise ValueError('Invalid EC2 initialization flag')
    from .ec2_images import validate_images
    from .ec2_templates import validate_templates
    validate_images(resources.get('images', {}))
    validate_templates(resources)
    from .ec2_protocol import MODEL
    shapes = {'Vpc': 'Vpc', 'Subnet': 'Subnet', 'SecurityGroup': 'SecurityGroup', 'InternetGateway': 'InternetGateway', 'RouteTable': 'RouteTable',
              'Volume': 'Volume', 'Snapshot': 'Snapshot', 'KeyPair': 'KeyPairInfo', 'NetworkInterface': 'NetworkInterface', 'Address': 'Address'}
    for kind, (collection, field, prefix, _) in COLLECTIONS.items():
        values = resources.get(collection, {})
        if not isinstance(values, dict) or len(values) > 1000:
            raise ValueError('Invalid EC2 resource collection')
        for key, item in values.items():
            if not isinstance(key, str) or not re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{17}', key) or not isinstance(item, dict) or item.get(field) != key:
                raise ValueError('Invalid EC2 resource identifier')
            required = {'Vpc': {'VpcId', 'CidrBlock', 'State', 'IsDefault', 'EnableDnsSupport', 'EnableDnsHostnames'},
                        'Subnet': {'SubnetId', 'VpcId', 'CidrBlock', 'AvailabilityZone', 'AvailableIpAddressCount', 'DefaultForAz', 'MapPublicIpOnLaunch'},
                        'SecurityGroup': {'GroupId', 'VpcId', 'GroupName', 'Description', 'OwnerId', 'IpPermissions', 'IpPermissionsEgress'},
                        'InternetGateway': {'InternetGatewayId', 'Attachments'}, 'RouteTable': {'RouteTableId', 'VpcId', 'Routes', 'Associations'},
                        'Volume': {'VolumeId', 'Size', 'AvailabilityZone', 'State', 'VolumeType', 'Attachments', 'Encrypted'},
                        'Snapshot': {'SnapshotId', 'VolumeId', 'VolumeSize', 'State', 'Encrypted'},
                        'KeyPair': {'KeyPairId', 'KeyName', 'KeyType', 'PublicKey'},
                        'NetworkInterface': {'NetworkInterfaceId', 'SubnetId', 'VpcId', 'PrivateIpAddress', 'Status'},
                        'Address': {'AllocationId', 'PublicIp', 'Domain'}}[kind]
            if not required <= set(item):
                raise ValueError('Missing EC2 resource fields')
            standard = {k: v for k, v in item.items() if k not in ({'EnableDnsSupport', 'EnableDnsHostnames'} if kind == 'Vpc' else {'Modification', '_auto_enable_io'} if kind == 'Volume' else {'_permissions'} if kind == 'Snapshot' else set()) | {'_client_token', '_request_hash'}}
            if '_auto_enable_io' in item and (kind != 'Volume' or type(item['_auto_enable_io']) is not bool):
                raise ValueError('Invalid volume auto-enable IO metadata')
            if '_permissions' in item:
                permissions = item['_permissions']
                if kind != 'Snapshot' or not isinstance(permissions, list) or len(permissions) > 100 or any(not isinstance(p, dict) or not (p == {'Group': 'all'} or set(p) == {'UserId'} and isinstance(p['UserId'], str) and re.fullmatch(r'[0-9]{12}', p['UserId'])) for p in permissions):
                    raise ValueError('Invalid snapshot permission metadata')
            if '_client_token' in item and (not isinstance(item['_client_token'], str) or not 1 <= len(item['_client_token']) <= 64 or not isinstance(item.get('_request_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', item['_request_hash'])):
                raise ValueError('Invalid resource idempotency metadata')
            validate_model(standard, shapes[kind])
            if kind == 'Vpc':
                cidr(item['CidrBlock'])
                boolean(item.get('EnableDnsSupport'))
                boolean(item.get('EnableDnsHostnames'))
            if kind == 'Subnet':
                cidr(item['CidrBlock'])
                if item['VpcId'] not in resources.get('vpcs', {}):
                    raise ValueError('Subnet has missing VPC')
            if kind == 'Volume':
                if item.get('State') not in ('available', 'in-use') or type(item.get('Size')) is not int or not 1 <= item['Size'] <= 1000:
                    raise ValueError('Invalid volume')
                if 'Modification' in item:
                    validate_model(item['Modification'], 'VolumeModification')
