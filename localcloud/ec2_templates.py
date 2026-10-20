"""Independent, bounded launch-template control plane, informed by REA replay.

Templates store only supported RunInstances settings. Unsupported settings fail
explicitly; a template is configuration, never a second execution backend.
"""
import base64
import copy
import fnmatch
import re
from datetime import datetime, timezone
from uuid import uuid4
from .engine import Error
from .ec2_infrastructure import paginated, tag_specs, validate_input

PARAMETERS = {
    'CreateLaunchTemplate': {'LaunchTemplateName', 'ClientToken', 'VersionDescription', 'LaunchTemplateData', 'TagSpecifications'},
    'DescribeLaunchTemplates': {'LaunchTemplateIds', 'LaunchTemplateNames', 'Filters', 'MaxResults', 'NextToken'},
    'DeleteLaunchTemplate': {'LaunchTemplateId', 'LaunchTemplateName'},
    'CreateLaunchTemplateVersion': {'LaunchTemplateId', 'LaunchTemplateName', 'ClientToken', 'SourceVersion', 'VersionDescription', 'LaunchTemplateData'},
    'DescribeLaunchTemplateVersions': {'LaunchTemplateId', 'LaunchTemplateName', 'Versions', 'MinVersion', 'MaxVersion', 'Filters', 'MaxResults', 'NextToken'},
    'DeleteLaunchTemplateVersions': {'LaunchTemplateId', 'LaunchTemplateName', 'Versions'},
    'ModifyLaunchTemplate': {'LaunchTemplateId', 'LaunchTemplateName', 'DefaultVersion', 'ClientToken'},
    'GetLaunchTemplateData': {'InstanceId'},
}
DATA_FIELDS = {'ImageId', 'InstanceType', 'KeyName', 'UserData', 'SecurityGroupIds', 'BlockDeviceMappings', 'MetadataOptions', 'TagSpecifications', 'NetworkInterfaces'}
TEXT_FIELDS = {'ImageId', 'InstanceType', 'KeyName', 'UserData'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def data_settings(settings):
    if not isinstance(settings, dict) or not settings or set(settings) - DATA_FIELDS:
        raise Error('UnsupportedOperation', 'LaunchTemplateData contains unsupported fields or is empty', 501)
    from .ec2_protocol import MODEL
    from .ec2_infrastructure import validate_model
    if 'CreateLaunchTemplate' in MODEL['operations']:
        request = MODEL['shapes'][MODEL['operations']['CreateLaunchTemplate']['input']]
        try:
            validate_model(settings, request['members']['LaunchTemplateData']['shape'])
        except (ValueError, TypeError, KeyError) as error:
            raise Error('InvalidParameterValue', str(error)) from error
    if any(not isinstance(settings[k], str) for k in TEXT_FIELDS & settings.keys()):
        raise Error('InvalidParameterValue', 'Template image, type, key and user data must be strings')
    if 'UserData' in settings:
        try:
            if len(base64.b64decode(settings['UserData'], validate=True)) > 16384:
                raise ValueError()
        except (ValueError, TypeError):
            raise Error('InvalidParameterValue', 'UserData must be base64 encoding at most 16384 bytes')
    if 'SecurityGroupIds' in settings and (not isinstance(settings['SecurityGroupIds'], list) or any(not isinstance(s, str) for s in settings['SecurityGroupIds'])):
        raise Error('InvalidParameterValue', 'SecurityGroupIds must be a list of IDs')
    # Templates use NetworkInterfaces for subnet/primary-IP defaults; the local
    # runner supports exactly one newly created primary interface.
    if 'NetworkInterfaces' in settings:
        interfaces = settings['NetworkInterfaces']
        if not isinstance(interfaces, list) or len(interfaces) != 1 or not isinstance(interfaces[0], dict):
            raise Error('UnsupportedOperation', 'Exactly one primary template network interface is supported', 501)
        nic = interfaces[0]
        if set(nic) - {'DeviceIndex', 'SubnetId', 'Groups', 'PrivateIpAddress', 'DeleteOnTermination'} or nic.get('DeviceIndex', 0) != 0 or nic.get('DeleteOnTermination', True) is not True:
            raise Error('UnsupportedOperation', 'Unsupported template primary network interface settings', 501)
        if 'SecurityGroupIds' in settings and 'Groups' in nic:
            raise Error('InvalidParameterCombination', 'Use top-level SecurityGroupIds or primary-interface Groups')
        for key in ('SubnetId', 'PrivateIpAddress'):
            if key in nic and not isinstance(nic[key], str):
                raise Error('InvalidParameterValue', key + ' must be a string')
        if 'Groups' in nic and (not isinstance(nic['Groups'], list) or any(not isinstance(s, str) for s in nic['Groups'])):
            raise Error('InvalidParameterValue', 'Groups must be a list of IDs')
    if 'MetadataOptions' in settings:
        from .ec2 import metadata_options
        metadata_options(settings['MetadataOptions'])
    if 'TagSpecifications' in settings:
        from .ec2 import launch_tags
        launch_tags(settings['TagSpecifications'])
    if 'BlockDeviceMappings' in settings:
        mappings = settings['BlockDeviceMappings']
        if not isinstance(mappings, list) or len(mappings) > 8:
            raise Error('InvalidParameterValue', 'At most eight block device mappings are supported')
        devices = set()
        for mapping in mappings:
            if not isinstance(mapping, dict) or set(mapping) - {'DeviceName', 'Ebs'} or 'Ebs' not in mapping or not isinstance(mapping['Ebs'], dict):
                raise Error('UnsupportedOperation', 'Only Ebs filesystem volume mappings are supported', 501)
            device = mapping.get('DeviceName', '')
            if not isinstance(device, str) or not re.fullmatch(r'(?:/dev/[a-zA-Z0-9]+|/mnt/[a-zA-Z0-9_-]+|/data(?:/[a-zA-Z0-9_-]+)?)', device) or device in devices:
                raise Error('InvalidParameterValue', 'Invalid or duplicated block device name')
            devices.add(device)
            ebs = mapping['Ebs']
            if set(ebs) - {'DeleteOnTermination', 'VolumeSize', 'SnapshotId', 'VolumeType', 'Encrypted', 'Iops', 'Throughput'}:
                raise Error('UnsupportedOperation', 'Unsupported block device fields', 501)
            if 'VolumeSize' in ebs and (type(ebs['VolumeSize']) is not int or not 1 <= ebs['VolumeSize'] <= 16384):
                raise Error('InvalidParameterValue', 'Invalid volume size')
            for key in ('DeleteOnTermination', 'Encrypted'):
                if key in ebs and type(ebs[key]) is not bool:
                    raise Error('InvalidParameterValue', key + ' must be boolean')
    return copy.deepcopy(settings)


class TemplateManager:
    def __init__(self, ec2):
        self.ec2, self.engine = ec2, ec2.engine

    def store(self, data):
        return self.ec2.infrastructure.store(data).setdefault('launch_templates', {})

    def find(self, data, selector):
        by_id, by_name = selector.get('LaunchTemplateId'), selector.get('LaunchTemplateName')
        if bool(by_id) == bool(by_name):
            raise Error('InvalidParameterCombination', 'Specify one launch template ID or name')
        templates = self.store(data)
        item = templates.get(by_id) if by_id else next((t for t in templates.values() if t['LaunchTemplateName'] == by_name), None)
        if item is None:
            raise Error('InvalidLaunchTemplateId.NotFound' if by_id else 'InvalidLaunchTemplateName.NotFoundException', 'Launch template does not exist')
        return item

    @staticmethod
    def version(item, selector=None):
        if selector in (None, '$Default'):
            number = item['DefaultVersionNumber']
        elif selector == '$Latest':
            number = item['LatestVersionNumber']
        else:
            try:
                if isinstance(selector, bool):
                    raise ValueError()
                number = int(selector)
                if str(number) != str(selector) or number < 1:
                    raise ValueError()
            except (ValueError, TypeError):
                raise Error('InvalidParameterValue', 'Invalid launch template version')
        version = next((v for v in item['Versions'] if v['VersionNumber'] == number), None)
        if version is None:
            raise Error('InvalidLaunchTemplateId.VersionNotFound', 'Launch template version does not exist')
        return version

    @staticmethod
    def summary(item):
        return {k: copy.deepcopy(v) for k, v in item.items() if k not in ('Versions',) and not k.startswith('_')}

    def version_view(self, item, version):
        return {**{k: copy.deepcopy(v) for k, v in version.items() if not k.startswith('_')}, 'LaunchTemplateId': item['LaunchTemplateId'], 'LaunchTemplateName': item['LaunchTemplateName'], 'DefaultVersion': version['VersionNumber'] == item['DefaultVersionNumber']}

    def resolve(self, data, selector):
        if not isinstance(selector, dict) or set(selector) - {'LaunchTemplateId', 'LaunchTemplateName', 'Version'}:
            raise Error('UnsupportedOperation', 'Unsupported launch template selector', 501)
        item = self.find(data, selector)
        settings = data_settings(self.version(item, selector.get('Version'))['LaunchTemplateData'])
        for nic in settings.pop('NetworkInterfaces', []):
            for source, target in [('SubnetId', 'SubnetId'), ('PrivateIpAddress', 'PrivateIpAddress'), ('Groups', 'SecurityGroupIds')]:
                if source in nic:
                    settings[target] = nic[source]
        settings['_LaunchTemplate'] = self.resolved_identity(data, selector)
        return settings

    def resolved_identity(self, data, selector):
        item = self.find(data, selector)
        return {'LaunchTemplateId': item['LaunchTemplateId'], 'LaunchTemplateName': item['LaunchTemplateName'], 'Version': str(self.version(item, selector.get('Version'))['VersionNumber'])}

    def call(self, operation, args, principal):
        if operation not in PARAMETERS or not isinstance(args, dict) or set(args) - PARAMETERS[operation]:
            raise Error('UnsupportedOperation', 'Unsupported launch template operation or parameters', 501)
        validate_input(operation, args)
        return self.engine.tools.call('ec2', operation, principal, '*', lambda: self.perform(operation, args, principal))

    def perform(self, operation, args, principal):
        with self.engine.transaction() as data:
            self.engine.authorize(data, principal, 'ec2:' + operation, '*')
            templates = self.store(data)
            if operation == 'CreateLaunchTemplate':
                name = args.get('LaunchTemplateName')
                if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9()._/-]{3,128}', name):
                    raise Error('InvalidLaunchTemplateName', 'Name must contain 3–128 allowed characters')
                replay = self.ec2.infrastructure.replay(templates, args)
                if replay:
                    return {'LaunchTemplate': self.summary(replay)}
                if any(t['LaunchTemplateName'] == name for t in templates.values()):
                    raise Error('InvalidLaunchTemplateName.AlreadyExistsException', 'Launch template name already exists')
                if len(templates) >= 1000:
                    raise Error('LaunchTemplateLimitExceeded', 'Local limit is 1000 launch templates')
                settings = data_settings(args.get('LaunchTemplateData'))
                item = {'LaunchTemplateId': 'lt-' + uuid4().hex[:17], 'LaunchTemplateName': name, 'CreateTime': stamp(), 'CreatedBy': 'arn:aws:iam::000000000000:root', 'DefaultVersionNumber': 1, 'LatestVersionNumber': 1, 'Tags': tag_specs(args.get('TagSpecifications'), 'launch-template'), 'Versions': [], **self.ec2.infrastructure.token_fields(args)}
                item['Versions'].append(self.new_version(1, settings, args.get('VersionDescription')))
                templates[item['LaunchTemplateId']] = item
                return {'LaunchTemplate': self.summary(item)}
            if operation == 'DescribeLaunchTemplates':
                ids, names = args.get('LaunchTemplateIds', []), args.get('LaunchTemplateNames', [])
                if ids and names:
                    raise Error('InvalidParameterCombination', 'Specify template IDs or names')
                items = [self.summary(t) for t in templates.values() if (not ids or t['LaunchTemplateId'] in ids) and (not names or t['LaunchTemplateName'] in names)]
                items = self.filter(items, args.get('Filters', []))
                return paginated({'LaunchTemplates': items}, 'LaunchTemplates', args)
            if operation == 'GetLaunchTemplateData':
                instance = self.ec2.infrastructure.instance(data, args.get('InstanceId'))
                settings = {k: copy.deepcopy(instance[k]) for k in DATA_FIELDS - {'NetworkInterfaces', 'TagSpecifications', 'BlockDeviceMappings'} if k in instance}
                settings['SecurityGroupIds'] = [g['GroupId'] for g in instance.get('SecurityGroups', [])]
                if 'MetadataOptions' in settings:
                    settings['MetadataOptions'].pop('State', None)
                if instance.get('SubnetId'):
                    settings['NetworkInterfaces'] = [{'DeviceIndex': 0, 'SubnetId': instance['SubnetId'], 'Groups': settings.pop('SecurityGroupIds'), 'PrivateIpAddress': instance['PrivateIpAddress']}]
                return {'LaunchTemplateData': settings}
            item = self.find(data, args)
            if operation == 'DeleteLaunchTemplate':
                del templates[item['LaunchTemplateId']]
                return {'LaunchTemplate': self.summary(item)}
            if operation == 'CreateLaunchTemplateVersion':
                replay = self.ec2.infrastructure.replay({str(v['VersionNumber']): v for v in item['Versions']}, args)
                if replay:
                    return {'LaunchTemplateVersion': self.version_view(item, replay)}
                if len(item['Versions']) >= 1000:
                    raise Error('LaunchTemplateVersionLimitExceeded', 'Local limit is 1000 versions per template')
                settings = {}
                if args.get('SourceVersion'):
                    settings = copy.deepcopy(self.version(item, args['SourceVersion'])['LaunchTemplateData'])
                overrides = args.get('LaunchTemplateData')
                if not isinstance(overrides, dict) or set(overrides) - DATA_FIELDS:
                    raise Error('UnsupportedOperation', 'Unsupported launch template fields', 501)
                settings.update(overrides)
                settings = data_settings(settings)
                number = item.get('_next_version', item['LatestVersionNumber']) + 1
                version = {**self.new_version(number, settings, args.get('VersionDescription')), **self.ec2.infrastructure.token_fields(args)}
                item['Versions'].append(version)
                item['LatestVersionNumber'] = number
                item['_next_version'] = number
                return {'LaunchTemplateVersion': self.version_view(item, version)}
            if operation == 'ModifyLaunchTemplate':
                if 'DefaultVersion' not in args:
                    raise Error('MissingParameter', 'DefaultVersion is required')
                item['DefaultVersionNumber'] = self.version(item, args['DefaultVersion'])['VersionNumber']
                return {'LaunchTemplate': self.summary(item)}
            if operation == 'DescribeLaunchTemplateVersions':
                versions = item['Versions']
                if args.get('Versions'):
                    numbers = {self.version(item, v)['VersionNumber'] for v in args['Versions']}
                    versions = [v for v in versions if v['VersionNumber'] in numbers]
                for key, comparison in [('MinVersion', lambda n, b: n >= b), ('MaxVersion', lambda n, b: n <= b)]:
                    if key in args:
                        try:
                            bound = int(args[key])
                            if bound < 1:
                                raise ValueError()
                        except (TypeError, ValueError):
                            raise Error('InvalidParameterValue', 'Version bounds must be positive integers')
                        versions = [v for v in versions if comparison(v['VersionNumber'], bound)]
                if args.get('Filters'):
                    raise Error('UnsupportedOperation', 'Version filters are not supported', 501)
                return paginated({'LaunchTemplateVersions': [self.version_view(item, v) for v in sorted(versions, key=lambda v: v['VersionNumber'], reverse=True)]}, 'LaunchTemplateVersions', args)
            if operation == 'DeleteLaunchTemplateVersions':
                if not isinstance(args.get('Versions'), list) or not args['Versions']:
                    raise Error('MissingParameter', 'Versions are required')
                success, failure = [], []
                for value in dict.fromkeys(args['Versions']):
                    try:
                        version = self.version(item, value)
                        if version['VersionNumber'] == item['DefaultVersionNumber']:
                            raise Error('CannotDeleteDefaultVersion', 'Cannot delete default version')
                        item['Versions'].remove(version)
                        success.append({'LaunchTemplateId': item['LaunchTemplateId'], 'LaunchTemplateName': item['LaunchTemplateName'], 'VersionNumber': version['VersionNumber']})
                    except Error as error:
                        failure.append({'LaunchTemplateId': item['LaunchTemplateId'], 'LaunchTemplateName': item['LaunchTemplateName'], 'VersionNumber': int(value) if str(value).isdigit() else 0, 'ResponseError': {'Code': error.code, 'Message': error.message}})
                item['_next_version'] = max(item.get('_next_version', item['LatestVersionNumber']), item['LatestVersionNumber'])
                item['LatestVersionNumber'] = max(v['VersionNumber'] for v in item['Versions'])
                return {'SuccessfullyDeletedLaunchTemplateVersions': success, 'UnsuccessfullyDeletedLaunchTemplateVersions': failure}
            raise Error('UnsupportedOperation', operation, 501)

    @staticmethod
    def new_version(number, settings, description):
        if description is not None and (not isinstance(description, str) or len(description) > 255):
            raise Error('InvalidParameterValue', 'Invalid version description')
        return {'VersionNumber': number, 'CreateTime': stamp(), 'CreatedBy': 'arn:aws:iam::000000000000:root', 'LaunchTemplateData': settings, **({'VersionDescription': description} if description is not None else {})}

    @staticmethod
    def filter(items, filters):
        for spec in filters:
            if not isinstance(spec, dict) or set(spec) != {'Name', 'Values'} or not isinstance(spec['Values'], list) or not spec['Values'] or any(not isinstance(v, str) for v in spec['Values']):
                raise Error('InvalidParameterValue', 'Invalid filters')
            name = spec['Name']
            if name not in ('launch-template-name', 'launch-template-id', 'tag-key') and not name.startswith('tag:'):
                raise Error('UnsupportedOperation', 'Unsupported launch template filter', 501)
            def values(item):
                if name.startswith('tag:'):
                    return [t['Value'] for t in item.get('Tags', []) if t['Key'] == name[4:]]
                if name == 'tag-key':
                    return [t['Key'] for t in item.get('Tags', [])]
                return [item['LaunchTemplateName' if name == 'launch-template-name' else 'LaunchTemplateId']]
            items = [item for item in items if any(fnmatch.fnmatchcase(value, pattern) for value in values(item) for pattern in spec['Values'])]
        return items


def validate_templates(resources):
    templates = resources.get('launch_templates', {})
    if not isinstance(templates, dict) or len(templates) > 1000:
        raise ValueError('Invalid launch templates')
    names = set()
    for identifier, item in templates.items():
        allowed = {'LaunchTemplateId', 'LaunchTemplateName', 'CreateTime', 'CreatedBy', 'DefaultVersionNumber', 'LatestVersionNumber', 'Tags', 'Versions', '_client_token', '_request_hash', '_next_version'}
        if not isinstance(item, dict) or set(item) - allowed or item.get('LaunchTemplateId') != identifier or not re.fullmatch(r'lt-[0-9a-f]{17}', identifier):
            raise ValueError('Invalid launch template')
        name = item.get('LaunchTemplateName')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9()._/-]{3,128}', name) or name in names:
            raise ValueError('Invalid launch template name')
        names.add(name)
        for text_key in ('CreateTime', 'CreatedBy'):
            if not isinstance(item.get(text_key), str) or len(item[text_key]) > 512:
                raise ValueError('Invalid launch template metadata')
        try:
            from .ec2 import validated_tags
            validated_tags(item.get('Tags', []))
        except Error as error:
            raise ValueError(error.message) from error
        if not isinstance(item.get('Versions'), list) or not 1 <= len(item['Versions']) <= 1000:
            raise ValueError('Invalid launch template versions')
        numbers = set()
        for version in item['Versions']:
            if not isinstance(version, dict) or set(version) - {'VersionNumber', 'CreateTime', 'CreatedBy', 'VersionDescription', 'LaunchTemplateData', '_client_token', '_request_hash'}:
                raise ValueError('Invalid template version fields')
            number = version.get('VersionNumber')
            if type(number) is not int or number < 1 or number in numbers:
                raise ValueError('Invalid template version number')
            numbers.add(number)
            if any(not isinstance(version.get(k), str) or len(version[k]) > 512 for k in ('CreateTime', 'CreatedBy')) or ('VersionDescription' in version and (not isinstance(version['VersionDescription'], str) or len(version['VersionDescription']) > 255)):
                raise ValueError('Invalid launch template version metadata')
            try:
                data_settings(version.get('LaunchTemplateData'))
            except Error as error:
                raise ValueError(error.message) from error
        if type(item.get('DefaultVersionNumber')) is not int or item['DefaultVersionNumber'] not in numbers or type(item.get('LatestVersionNumber')) is not int or item['LatestVersionNumber'] != max(numbers) or type(item.get('_next_version', item['LatestVersionNumber'])) is not int or item.get('_next_version', item['LatestVersionNumber']) < item['LatestVersionNumber']:
            raise ValueError('Invalid default/latest template version')
