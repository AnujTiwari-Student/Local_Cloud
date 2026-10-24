"""Persistent AMI metadata lifecycle, independently reconstructed from API captures.

Mock images are portable configuration records. This is not a VM disk image
importer and does not let registered names escape the Docker image allowlist.
"""
import copy
import re
from .engine import Error, wildcard

PARAMETERS = {
    'RegisterImage': {'Name', 'Description', 'Architecture', 'RootDeviceName', 'VirtualizationType', 'BlockDeviceMappings', 'EnaSupport', 'BootMode', 'TagSpecifications'},
    'CreateImage': {'InstanceId', 'Name', 'Description', 'NoReboot', 'TagSpecifications'},
    'DeregisterImage': {'ImageId'},
    'DescribeImageAttribute': {'ImageId', 'Attribute'},
    'ModifyImageAttribute': {'ImageId', 'Description', 'LaunchPermission'},
    'ResetImageAttribute': {'ImageId', 'Attribute'},
}


class ImageLifecycle:
    def __init__(self, infrastructure):
        self.infra = infrastructure
        self.ec2 = infrastructure.ec2

    def store(self, data):
        return self.infra.store(data).setdefault('images', {})

    def catalogue(self, data):
        configured = self.ec2.images or {'ami-local': None}
        catalogue = {key: {'ImageId': key, 'Name': key, 'State': 'available', 'OwnerId': '000000000000',
                           'Public': False, 'Architecture': 'x86_64', 'ImageType': 'machine',
                           'Tags': [{'Key': 'ec2_vm_manager', 'Value': self.ec2.backend}]}
                     for key in configured}
        for instance in data.get('instances', {}).values():
            if instance['Backend'] == 'mock':
                key = instance['ImageId']
                catalogue.setdefault(key, {'ImageId': key, 'Name': key, 'State': 'available', 'OwnerId': '000000000000',
                                          'Public': False, 'Architecture': 'x86_64', 'ImageType': 'machine', 'Tags': []})
        catalogue.update(self.store(data))
        return catalogue

    def get(self, data, key):
        item = self.catalogue(data).get(key)
        if not item:
            raise Error('InvalidAMIID.NotFound', f'The image {key!r} does not exist')
        if item.get('_deregistered'):
            raise Error('InvalidAMIID.Unavailable', f'The image {key!r} is no longer available')
        return item

    def resolve(self, data, key):
        """Restore only this database's committed AMI to the runtime allowlist."""
        item = self.get(data, key)
        if '_docker_image' in item:
            if self.ec2.kubernetes is not None:
                raise Error('UnsupportedOperation', 'Docker root images cannot be used by the Kubernetes executor', 501)
            runtime = self.ec2.runtime
            if item.get('_docker_owner') != runtime.owner or not re.fullmatch(r'localcloud-ec2-ami:ami-[0-9a-f]{17}', item['_docker_image']):
                raise Error('RuntimeFailure', 'AMI runtime ownership mismatch', 409)
            runtime.owned('image', item['_docker_image'])
            self.ec2.images[key] = item['_docker_image']
        elif self.ec2.images and key not in self.ec2.images:
            raise Error('InvalidAMIID.NotFound', 'Mock AMI cannot be launched by a container executor')
        return item

    def tagtarget(self, data, key):
        item = self.get(data, key)
        images = self.store(data)
        images.setdefault(key, copy.deepcopy(item))
        return images[key]

    def describe(self, data, args):
        from .ec2_infrastructure import paginated
        catalogue = self.catalogue(data)
        ids, owners = args.get('ImageIds', []), args.get('Owners', [])
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            raise Error('InvalidParameterValue', 'ImageIds must be strings')
        if not isinstance(owners, list) or any(not isinstance(key, str) for key in owners):
            raise Error('InvalidParameterValue', 'Owners must be strings')
        if any(key not in catalogue for key in ids):
            raise Error('InvalidAMIID.NotFound', 'One or more image IDs do not exist')
        images = [copy.deepcopy(item) for key, item in catalogue.items() if not item.get('_deregistered')
                  and (not ids or key in ids) and (not owners or set(owners) & {'self', '000000000000'})]
        fields = {'image-id': 'ImageId', 'name': 'Name', 'description': 'Description', 'state': 'State',
                  'architecture': 'Architecture', 'owner-id': 'OwnerId', 'root-device-type': 'RootDeviceType',
                  'virtualization-type': 'VirtualizationType', 'is-public': 'Public'}
        filters = args.get('Filters', [])
        if not isinstance(filters, list):
            raise Error('InvalidParameterValue', 'Filters must be a list')
        for rule in filters:
            if not isinstance(rule, dict) or set(rule) != {'Name', 'Values'} or not isinstance(rule['Name'], str) or not isinstance(rule['Values'], list) or not rule['Values'] or any(not isinstance(v, str) for v in rule['Values']):
                raise Error('InvalidParameterValue', 'Invalid image filter')
            name, values = rule['Name'], rule['Values']
            if name.startswith('tag:'):
                images = [i for i in images if any(t['Key'] == name[4:] and any(wildcard(t['Value'], v) for v in values) for t in i.get('Tags', []))]
            elif name == 'tag-key':
                images = [i for i in images if any(any(wildcard(t['Key'], v) for v in values) for t in i.get('Tags', []))]
            elif name in fields:
                images = [i for i in images if any(wildcard(str(i.get(fields[name], '')).lower() if isinstance(i.get(fields[name]), bool) else str(i.get(fields[name], '')), v) for v in values)]
            else:
                raise Error('UnsupportedOperation', 'Unsupported image filter: ' + name, 501)
        images = [{k: v for k, v in item.items() if not k.startswith('_')} for item in images]
        return paginated({'Images': images}, 'Images', args)

    def perform(self, operation, args, data, principal):
        from .ec2_infrastructure import identifier, now, text, tag_specs, boolean
        images = self.store(data)
        if operation in ('RegisterImage', 'CreateImage'):
            if self.ec2.images and operation == 'RegisterImage':
                raise Error('UnsupportedOperation', 'AMI registration and creation currently support mock mode; Docker AMIs must be configured with --ec2-image', 501)
            name = text(args.get('Name'), 128)
            description = args.get('Description', '')
            if not isinstance(description, str) or len(description) > 255 or '\x00' in description:
                raise Error('InvalidParameterValue', 'Description must be bounded text')
            architecture = args.get('Architecture', 'x86_64')
            virtualization = args.get('VirtualizationType', 'hvm')
            boot = args.get('BootMode', 'uefi')
            if architecture not in ('x86_64', 'arm64', 'i386') or virtualization not in ('hvm', 'paravirtual') or boot not in ('legacy-bios', 'uefi', 'uefi-preferred'):
                raise Error('InvalidParameterValue', 'Unsupported image architecture, boot mode or virtualization type')
            root = args.get('RootDeviceName', '/dev/sda1')
            if not isinstance(root, str) or not re.fullmatch(r'/dev/[a-zA-Z0-9_-]{1,32}', root):
                raise Error('InvalidParameterValue', 'RootDeviceName must be a /dev/ device name')
            mappings = copy.deepcopy(args.get('BlockDeviceMappings', []))
            if not isinstance(mappings, list) or len(mappings) > 8:
                raise Error('InvalidParameterValue', 'At most eight block mappings are supported')
            seen = set()
            for mapping in mappings:
                if not isinstance(mapping, dict) or set(mapping) != {'DeviceName', 'Ebs'} or not isinstance(mapping['DeviceName'], str) or not re.fullmatch(r'/dev/[a-zA-Z0-9_-]{1,32}', mapping['DeviceName']) or mapping['DeviceName'] in seen:
                    raise Error('InvalidParameterValue', 'Expected unique EBS device mappings')
                seen.add(mapping['DeviceName'])
                ebs = mapping['Ebs']
                if not isinstance(ebs, dict) or set(ebs) - {'SnapshotId', 'VolumeSize', 'VolumeType', 'DeleteOnTermination', 'Encrypted', 'Iops', 'Throughput'}:
                    raise Error('UnsupportedOperation', 'Unsupported AMI EBS mapping', 501)
                if 'SnapshotId' not in ebs:
                    raise Error('MissingParameter', 'AMI EBS mappings require SnapshotId')
                self.infra.get(self.infra.store(data), 'Snapshot', ebs['SnapshotId'])
                if 'VolumeSize' in ebs and (type(ebs['VolumeSize']) is not int or not 1 <= ebs['VolumeSize'] <= 1024):
                    raise Error('InvalidParameterValue', 'Invalid mapping volume size')
                for field in ('DeleteOnTermination', 'Encrypted'):
                    if field in ebs:
                        boolean(ebs[field])
            item = {'ImageId': identifier('ami'), 'Name': name, 'Description': description, 'State': 'available',
                    'OwnerId': '000000000000', 'CreationDate': now(), 'Public': False, 'Architecture': architecture,
                    'ImageType': 'machine', 'VirtualizationType': virtualization, 'RootDeviceName': root,
                    'RootDeviceType': 'ebs' if mappings else 'instance-store', 'BlockDeviceMappings': mappings,
                    'BootMode': boot, 'Tags': tag_specs(args.get('TagSpecifications'), 'image'), 'ProductCodes': []}
            if 'EnaSupport' in args:
                item['EnaSupport'] = boolean(args['EnaSupport'])
            if operation == 'CreateImage':
                instance = self.infra.instance(data, args.get('InstanceId'))
                if instance['Backend'] == 'kubernetes':
                    raise Error('UnsupportedOperation', 'Kubernetes root image capture is not implemented', 501)
                if instance['State']['Name'] not in ('running', 'stopped'):
                    raise Error('IncorrectInstanceState', 'Image creation requires a running or stopped instance')
                if 'NoReboot' in args:
                    boolean(args['NoReboot'])
                if instance.get('BlockDeviceMappings'):
                    raise Error('UnsupportedOperation', 'CreateImage with attached volumes requires explicit snapshots and RegisterImage', 501)
                item['SourceInstanceId'] = instance['InstanceId']
                if instance['Backend'] == 'docker':
                    if instance['State']['Name'] != 'stopped':
                        raise Error('IncorrectInstanceState', 'Stop the Docker instance before capturing its root filesystem')
                    name = self.ec2.container(instance)
                    if self.ec2.docker('inspect', '--format', '{{.State.Running}}', name) != 'false':
                        raise Error('IncorrectInstanceState', 'Docker container must be stopped')
                    runtime = self.ec2.runtime
                    image = 'localcloud-ec2-ami:' + item['ImageId']
                    try:
                        self.ec2.docker('commit', '--change', 'LABEL ' + runtime.label(), name, image)
                        runtime.owned('image', image)
                    except Error:
                        if runtime.owned('image', image, absent=True):
                            self.ec2.docker('image', 'rm', image)
                        raise
                    item.update(_docker_image=image, _docker_owner=runtime.owner)
            images[item['ImageId']] = item
            return {'ImageId': item['ImageId']}
        item = self.get(data, args.get('ImageId'))
        if operation == 'DeregisterImage':
            if item.get('_docker_image'):
                if any(i['ImageId'] == item['ImageId'] and i['State']['Name'] != 'terminated' for i in data.get('instances', {}).values()):
                    raise Error('DependencyViolation', 'Terminate instances launched from this Docker AMI before deregistering it')
                self.resolve(data, item['ImageId'])
                self.ec2.docker('image', 'rm', item['_docker_image'])
                self.ec2.images.pop(item['ImageId'], None)
            item = copy.deepcopy(item)
            item.pop('_docker_image', None)
            item.pop('_docker_owner', None)
            item.update(_deregistered=True, State='deregistered')
            images[item['ImageId']] = item
            return {'Return': True}
        if operation == 'DescribeImageAttribute':
            attribute = args.get('Attribute')
            if attribute == 'launchPermission':
                return {'ImageId': item['ImageId'], 'LaunchPermissions': copy.deepcopy(item.get('_launch_permissions', []))}
            fields = {'description': 'Description', 'architecture': 'Architecture', 'kernel': 'KernelId',
                      'ramdisk': 'RamdiskId', 'rootDeviceName': 'RootDeviceName', 'bootMode': 'BootMode'}
            if attribute == 'blockDeviceMapping':
                return {'ImageId': item['ImageId'], 'BlockDeviceMappings': copy.deepcopy(item.get('BlockDeviceMappings', []))}
            if attribute not in fields:
                raise Error('UnsupportedOperation', 'Unsupported image attribute', 501)
            key = fields[attribute]
            return {'ImageId': item['ImageId'], key: {'Value': item.get(key, '')}}
        item = copy.deepcopy(item)
        if operation == 'ResetImageAttribute':
            if args.get('Attribute') != 'launchPermission':
                raise Error('UnsupportedOperation', 'Only launchPermission can be reset', 501)
            item['_launch_permissions'], item['Public'] = [], False
        elif operation == 'ModifyImageAttribute':
            if ('Description' in args) + ('LaunchPermission' in args) != 1:
                raise Error('InvalidParameterCombination', 'Choose exactly one image attribute')
            if 'Description' in args:
                value = args['Description']
                if not isinstance(value, dict) or set(value) != {'Value'} or not isinstance(value['Value'], str) or len(value['Value']) > 255 or '\x00' in value['Value']:
                    raise Error('InvalidParameterValue', 'Invalid image description')
                item['Description'] = value['Value']
            else:
                changes = args['LaunchPermission']
                if not isinstance(changes, dict) or not changes or set(changes) - {'Add', 'Remove'}:
                    raise Error('InvalidParameterValue', 'Expected Add or Remove launch permissions')
                permissions = copy.deepcopy(item.get('_launch_permissions', []))
                for action, entries in changes.items():
                    if not isinstance(entries, list) or len(entries) > 100:
                        raise Error('InvalidParameterValue', 'Invalid launch permission list')
                    for entry in entries:
                        if not isinstance(entry, dict) or not (set(entry) == {'Group'} and entry['Group'] == 'all' or set(entry) == {'UserId'} and isinstance(entry['UserId'], str) and re.fullmatch(r'\d{12}', entry['UserId'])):
                            raise Error('InvalidParameterValue', 'Expected all group or a 12-digit UserId')
                        if action == 'Add' and entry not in permissions:
                            permissions.append(entry)
                        elif action == 'Remove' and entry in permissions:
                            permissions.remove(entry)
                item['_launch_permissions'] = permissions
                item['Public'] = {'Group': 'all'} in permissions
        else:
            raise Error('UnsupportedOperation', operation, 501)
        images[item['ImageId']] = item
        return {}


def validate_images(images):
    """Validate portable mock AMIs without accepting host paths or runtime images."""
    from .ec2_protocol import MODEL
    from .ec2_infrastructure import validate_model
    if not isinstance(images, dict) or len(images) > 1000:
        raise ValueError('Invalid image catalogue')
    allowed = set(MODEL['shapes']['Image']['members']) | {'_deregistered', '_launch_permissions'}
    for key, item in images.items():
        if not isinstance(key, str) or not re.fullmatch(r'ami-[A-Za-z0-9-]{1,80}', key) or not isinstance(item, dict) or item.get('ImageId') != key or set(item) - allowed:
            raise ValueError('Invalid image record')
        public = {k: v for k, v in item.items() if not k.startswith('_')}
        try:
            validate_model(public, 'Image')
        except (Error, ValueError):
            raise ValueError('Invalid image fields')
        if item.get('OwnerId') != '000000000000' or item.get('State') not in ('available', 'deregistered') or type(item.get('Public')) is not bool:
            raise ValueError('Invalid local image ownership or state')
        if '_deregistered' in item and type(item['_deregistered']) is not bool:
            raise ValueError('Invalid image deregistration state')
        entries = item.get('_launch_permissions', [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('Invalid image permissions')
        for entry in entries:
            if not isinstance(entry, dict) or not (set(entry) == {'Group'} and entry['Group'] == 'all' or set(entry) == {'UserId'} and isinstance(entry['UserId'], str) and re.fullmatch(r'\d{12}', entry['UserId'])):
                raise ValueError('Invalid portable image permission')
