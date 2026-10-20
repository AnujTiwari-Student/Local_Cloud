"""EC2 ENI attributes and private-address contracts informed by REA replay.

ENI and elastic-IP operations manage control-plane records in mock and Docker
modes. They do not install host routes, firewall rules or guest interfaces.
"""
import copy
from .engine import Error, integer


class Networking:
    def sync_network_views(self, data, resources, interface):
        identifier = interface.get('Attachment', {}).get('InstanceId')
        for address in resources['addresses'].values():
            if address.get('NetworkInterfaceId') == interface['NetworkInterfaceId']:
                if identifier:
                    address['InstanceId'] = identifier
                else:
                    address.pop('InstanceId', None)
        instance = data.get('instances', {}).get(identifier)
        if not instance:
            return
        instance['NetworkInterfaces'] = [self.interface_view(i) for i in resources['interfaces'].values() if i.get('Attachment', {}).get('InstanceId') == identifier]
        if interface.get('Attachment', {}).get('DeviceIndex') == 0:
            instance['SecurityGroups'] = copy.deepcopy(interface['Groups'])
            if interface.get('Association'):
                instance['PublicIpAddress'] = interface['Association']['PublicIp']
            else:
                instance.pop('PublicIpAddress', None)

    def interface_attributes(self, data, resources, operation, args):
        from .ec2_infrastructure import boolean
        interface = self.get(resources, 'NetworkInterface', args.get('NetworkInterfaceId'))
        if operation == 'DescribeNetworkInterfaceAttribute':
            field = {'description': 'Description', 'sourceDestCheck': 'SourceDestCheck', 'groupSet': 'Groups', 'attachment': 'Attachment'}.get(args.get('Attribute'))
            if not field:
                raise Error('InvalidParameterValue', 'Supported attributes: description, sourceDestCheck, groupSet, attachment')
            value = interface.get(field)
            return {'NetworkInterfaceId': interface['NetworkInterfaceId'], **({field: {'Value': value} if field in ('Description', 'SourceDestCheck') else copy.deepcopy(value)} if value is not None else {})}
        fields = set(args) - {'NetworkInterfaceId'}
        if len(fields) != 1:
            raise Error('InvalidParameterCombination', 'Modify one network interface attribute at a time')
        field = next(iter(fields))
        value = args[field]
        if field in ('Description', 'SourceDestCheck'):
            if not isinstance(value, dict) or set(value) != {'Value'}:
                raise Error('InvalidParameterValue', 'Expected attribute Value')
            if field == 'Description':
                if not isinstance(value['Value'], str) or len(value['Value']) > 255 or '\x00' in value['Value']:
                    raise Error('InvalidParameterValue', 'Invalid interface description')
                interface[field] = value['Value']
            else:
                interface[field] = boolean(value['Value'])
        elif field == 'Groups':
            if not isinstance(value, list) or not 1 <= len(value) <= 5 or len(set(value)) != len(value):
                raise Error('InvalidParameterValue', 'Expected 1 through 5 distinct groups')
            groups = [self.get(resources, 'SecurityGroup', key) for key in value]
            if any(g['VpcId'] != interface['VpcId'] for g in groups):
                raise Error('InvalidGroup.NotFound', 'Groups must belong to the interface VPC')
            interface['Groups'] = [{'GroupId': g['GroupId'], 'GroupName': g['GroupName']} for g in groups]
        elif field == 'Attachment':
            attachment = interface.get('Attachment')
            if not isinstance(value, dict) or set(value) != {'AttachmentId', 'DeleteOnTermination'} or not attachment or value['AttachmentId'] != attachment['AttachmentId']:
                raise Error('InvalidAttachmentID.NotFound', 'Select the current attachment')
            attachment['DeleteOnTermination'] = boolean(value['DeleteOnTermination'])
        else:
            raise Error('UnsupportedOperation', 'Unsupported interface attribute', 501)
        self.sync_network_views(data, resources, interface)
        return {}

    def private_addresses(self, data, resources, operation, args):
        from .ec2_infrastructure import boolean
        interface = self.get(resources, 'NetworkInterface', args.get('NetworkInterfaceId'))
        addresses = args.get('PrivateIpAddresses')
        count = args.get('SecondaryPrivateIpAddressCount')
        assign = operation == 'AssignPrivateIpAddresses'
        if assign and ((addresses is None) == (count is None)):
            raise Error('InvalidParameterCombination', 'Specify addresses or a secondary address count')
        if not assign or addresses is not None:
            if not isinstance(addresses, list) or not 1 <= len(addresses) <= 30 or any(not isinstance(a, str) for a in addresses) or len(set(addresses)) != len(addresses):
                raise Error('InvalidParameterValue', 'Expected 1 through 30 distinct private IPv4 addresses')
        else:
            count = integer(count, 1, 30)
            addresses = [None] * count
        current = interface.setdefault('PrivateIpAddresses', [{'PrivateIpAddress': interface['PrivateIpAddress'], 'Primary': True}])
        if assign:
            if len(current) + len(addresses) > 31:
                raise Error('PrivateIpAddressLimitExceeded', 'Local interface limit is 30 secondary addresses')
            subnet = self.get(resources, 'Subnet', interface['SubnetId'])
            reassignment = boolean(args.get('AllowReassignment', False))
            for address in addresses:
                owner = next((i for i in resources['interfaces'].values() if i['SubnetId'] == interface['SubnetId'] and any(a['PrivateIpAddress'] == address for a in i.get('PrivateIpAddresses', []))), None) if address else None
                if owner:
                    if owner is interface:
                        continue  # Repeating an assignment is idempotent.
                    if not reassignment or owner['PrivateIpAddress'] == address:
                        raise Error('InvalidIPAddress.InUse', 'Address already belongs to another interface')
                    if any(a.get('NetworkInterfaceId') == owner['NetworkInterfaceId'] and a.get('PrivateIpAddress') == address for a in resources['addresses'].values()):
                        raise Error('InvalidIPAddress.InUse', 'Disassociate elastic address before reassignment')
                    owner['PrivateIpAddresses'] = [a for a in owner['PrivateIpAddresses'] if a['PrivateIpAddress'] != address]
                    self.sync_network_views(data, resources, owner)
                allocated = self.allocate_ip(data, resources, subnet, address)
                current.append({'PrivateIpAddress': allocated, 'Primary': False})
            result = {'NetworkInterfaceId': interface['NetworkInterfaceId'], 'AssignedPrivateIpAddresses': [{'PrivateIpAddress': a['PrivateIpAddress']} for a in current if not a['Primary']]}
        else:
            existing = {a['PrivateIpAddress'] for a in current}
            if interface['PrivateIpAddress'] in addresses or not set(addresses) <= existing:
                raise Error('InvalidParameterValue', 'Cannot remove primary or unassigned addresses')
            if any(a.get('NetworkInterfaceId') == interface['NetworkInterfaceId'] and a.get('PrivateIpAddress') in addresses for a in resources['addresses'].values()):
                raise Error('InvalidIPAddress.InUse', 'Disassociate elastic address before removing private address')
            interface['PrivateIpAddresses'] = [a for a in current if a['PrivateIpAddress'] not in addresses]
            result = {}
        self.sync_network_views(data, resources, interface)
        return result

    def clear_association(self, data, resources, address):
        interface = resources['interfaces'].get(address.get('NetworkInterfaceId'))
        if interface:
            if interface.get('Association', {}).get('AssociationId') == address.get('AssociationId'):
                interface.pop('Association', None)
            for item in interface['PrivateIpAddresses']:
                if item.get('Association', {}).get('AssociationId') == address.get('AssociationId'):
                    item.pop('Association', None)
            self.sync_network_views(data, resources, interface)
        instance = data.get('instances', {}).get(address.get('InstanceId'))
        if instance and instance.get('PublicIpAddress') == address['PublicIp']:
            instance.pop('PublicIpAddress', None)
        for field in ('AssociationId', 'InstanceId', 'NetworkInterfaceId', 'NetworkInterfaceOwnerId', 'PrivateIpAddress'):
            address.pop(field, None)

    def associate_address(self, data, resources, args, address):
        from .ec2_infrastructure import identifier, boolean, ACCOUNT
        if bool(args.get('InstanceId')) == bool(args.get('NetworkInterfaceId')):
            raise Error('InvalidParameterCombination', 'Specify either InstanceId or NetworkInterfaceId')
        if args.get('NetworkInterfaceId'):
            interface = self.get(resources, 'NetworkInterface', args['NetworkInterfaceId'])
            instance = data.get('instances', {}).get(interface.get('Attachment', {}).get('InstanceId'))
        else:
            instance = self.instance(data, args['InstanceId'])
            interfaces = [i for i in resources['interfaces'].values() if i.get('Attachment', {}).get('InstanceId') == instance['InstanceId']]
            interface = next((i for i in interfaces if i['Attachment']['DeviceIndex'] == 0), None)
            if interface is None:
                raise Error('InvalidNetworkInterfaceID.NotFound', 'Instance has no primary interface')
        if instance and instance['State']['Name'] == 'terminated':
            raise Error('IncorrectInstanceState', 'Instance is terminated')
        private = args.get('PrivateIpAddress', interface['PrivateIpAddress'])
        target = next((i for i in interface['PrivateIpAddresses'] if i['PrivateIpAddress'] == private), None)
        if not target:
            raise Error('InvalidAddress.NotFound', 'Private address does not belong to the interface')
        if address.get('AssociationId'):
            if address.get('NetworkInterfaceId') == interface['NetworkInterfaceId'] and address.get('PrivateIpAddress') == private:
                return {'AssociationId': address['AssociationId']}
            if not boolean(args.get('AllowReassociation', True)):
                raise Error('Resource.AlreadyAssociated', 'Address already associated')
            self.clear_association(data, resources, address)
        for previous in resources['addresses'].values():
            if previous.get('NetworkInterfaceId') == interface['NetworkInterfaceId'] and previous.get('PrivateIpAddress') == private:
                self.clear_association(data, resources, previous)
        address.update(NetworkInterfaceId=interface['NetworkInterfaceId'],NetworkInterfaceOwnerId=ACCOUNT,PrivateIpAddress=private,AssociationId=identifier('eipassoc'))
        if instance:
            address['InstanceId'] = instance['InstanceId']
        association = {'AssociationId': address['AssociationId'], 'AllocationId': address['AllocationId'], 'PublicIp': address['PublicIp'], 'IpOwnerId': ACCOUNT}
        target['Association'] = copy.deepcopy(association)
        if private == interface['PrivateIpAddress']:
            interface['Association'] = association
        self.sync_network_views(data, resources, interface)
        return {'AssociationId': address['AssociationId']}
