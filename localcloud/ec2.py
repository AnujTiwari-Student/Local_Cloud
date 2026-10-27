"""Small EC2 lifecycle provider; optional bounded Docker execution."""
import copy
import base64
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from .engine import Error, integer, valid_name, wildcard
from .ec2_infrastructure import PARAMETERS, paginated, COLLECTIONS
from .ec2_templates import PARAMETERS as TEMPLATE_PARAMETERS, TemplateManager
from .ec2_images import ImageLifecycle

CODES = {'pending': 0, 'running': 16, 'shutting-down': 32, 'terminated': 48, 'stopping': 64, 'stopped': 80}
OPERATIONS = {'RunInstances', 'DescribeInstances', 'StartInstances', 'StopInstances', 'TerminateInstances', 'NameInstance', 'ModifyInstanceAttribute', 'DescribeImages', 'CreateTags', 'DeleteTags', 'DescribeTags', 'DescribeInstanceAttribute'}
from .ec2_profiles import OPERATIONS as PROFILE_OPERATIONS, Profiles
OPERATIONS |= PROFILE_OPERATIONS
OPERATIONS |= set(PARAMETERS) | set(TEMPLATE_PARAMETERS) | {'RebootInstances', 'GetConsoleOutput', 'DescribeInstanceStatus', 'ModifyInstanceMetadataOptions'}


def state(name):
    return {'Code': CODES[name], 'Name': name}


def metadata_options(options):
    if not isinstance(options, dict) or set(options) - {'HttpTokens', 'HttpEndpoint', 'HttpPutResponseHopLimit', 'State', 'HttpProtocolIpv6', 'InstanceMetadataTags'}:
        raise Error('InvalidParameterValue', 'Unsupported metadata options')
    tokens, endpoint = options.get('HttpTokens', 'optional'), options.get('HttpEndpoint', 'enabled')
    if tokens not in ('optional', 'required') or endpoint not in ('enabled', 'disabled') or options.get('HttpProtocolIpv6', 'disabled') != 'disabled' or options.get('InstanceMetadataTags', 'disabled') != 'disabled':
        raise Error('InvalidParameterValue', 'Unsupported metadata option value')
    return {'HttpTokens': tokens, 'HttpEndpoint': endpoint, 'HttpPutResponseHopLimit': integer(options.get('HttpPutResponseHopLimit', 1), 1, 64), 'HttpProtocolIpv6': 'disabled', 'InstanceMetadataTags': 'disabled', 'State': 'applied'}


def validated_tags(tags, delete=False):
    if not isinstance(tags, list) or len(tags) > 50:
        raise Error('InvalidParameterValue', 'Tags must be a list of at most 50 key/value pairs')
    seen = set()
    for tag in tags:
        if (not isinstance(tag, dict) or set(tag) - {'Key', 'Value'} or 'Key' not in tag
                or not isinstance(tag['Key'], str) or not 1 <= len(tag['Key']) <= 128
                or tag['Key'].startswith('aws:') or tag['Key'] in seen
                or ('Value' in tag and (not isinstance(tag['Value'], str) or len(tag['Value']) > 256))
                or (not delete and 'Value' not in tag)):
            raise Error('InvalidParameterValue', 'Invalid or duplicate tag key/value')
        seen.add(tag['Key'])
    return copy.deepcopy(tags)


def launch_tags(specifications):
    if not isinstance(specifications, list) or len(specifications) > 1:
        raise Error('UnsupportedOperation', 'Only instance tag specifications are supported', 501)
    if not specifications:
        return []
    spec = specifications[0]
    if not isinstance(spec, dict) or set(spec) != {'ResourceType', 'Tags'} or spec['ResourceType'] != 'instance':
        raise Error('UnsupportedOperation', 'Only instance tag specifications are supported', 501)
    return validated_tags(spec['Tags'])


def filtered(items, filters, kind):
    fields = {'instance': {'instance-id': 'InstanceId', 'image-id': 'ImageId', 'instance-type': 'InstanceType',
                           'instance-state-name': 'State.Name', 'instance-state-code': 'State.Code', 'client-token': 'ClientToken'},
              'image': {'image-id': 'ImageId', 'name': 'Name', 'state': 'State', 'owner-id': 'OwnerId', 'architecture': 'Architecture'},
              'tag': {'resource-id': 'ResourceId', 'resource-type': 'ResourceType', 'key': 'Key', 'value': 'Value'}}[kind]
    if not isinstance(filters, list) or len(filters) > 30:
        raise Error('InvalidParameterValue', 'Filters must be a list of at most 30 items')
    result = list(items)
    for entry in filters:
        if (not isinstance(entry, dict) or set(entry) != {'Name', 'Values'} or not isinstance(entry['Name'], str)
                or not isinstance(entry['Values'], list) or not 1 <= len(entry['Values']) <= 100
                or any(not isinstance(v, str) or len(v) > 256 for v in entry['Values'])):
            raise Error('InvalidParameterValue', 'Each filter needs Name and nonempty string Values')
        name, patterns = entry['Name'], entry['Values']
        if name not in fields and not (kind != 'tag' and (name.startswith('tag:') or name in ('tag-key', 'tag-value'))):
            raise Error('InternalFailure', 'Unsupported filter: ' + name, 501)
        def matches(item):
            if name.startswith('tag:'):
                values = [t['Value'] for t in item.get('Tags', []) if t['Key'] == name[4:]]
            elif name in ('tag-key', 'tag-value'):
                values = [t['Key' if name == 'tag-key' else 'Value'] for t in item.get('Tags', [])]
            else:
                value = item
                for key in fields[name].split('.'):
                    value = value.get(key, '') if isinstance(value, dict) else ''
                values = [str(value)]
            return any(wildcard(v, pattern) for v in values for pattern in patterns)
        result = [item for item in result if matches(item)]
    return result


class EC2:
    def __init__(self, engine, images=None, kubernetes=None):
        self.engine = engine
        self.images = images or {}
        self.kubernetes = None
        from .ec2_runtime import Runtime
        from .ec2_infrastructure import Infrastructure
        self.runtime = Runtime(self)
        self.infrastructure = Infrastructure(self)
        self.templates = TemplateManager(self)
        self.image_lifecycle = ImageLifecycle(self.infrastructure)
        self.profiles = Profiles(self)
        from .ec2_packet_runtime import PacketRuntime
        self.packet_runtime = PacketRuntime(self)
        if kubernetes is not None:
            if not self.images:
                raise ValueError('Kubernetes EC2 requires configured AMI image mappings')
            from .ec2_kubernetes import EC2Kubernetes
            self.kubernetes = EC2Kubernetes(self, **kubernetes)

    @property
    def backend(self):
        return 'kubernetes' if self.kubernetes is not None else ('docker' if self.images else 'mock')

    def kube_runtime(self, item):
        if self.kubernetes is None:
            raise Error('RuntimeUnavailable', 'Restart with Kubernetes EC2 configuration for this instance', 503)
        return self.kubernetes

    def container_args(self, item, mappings=None):
        ports = item.get('PortMappings', [])
        publish = [part for p in ports for part in ('--publish', f"127.0.0.1:{p['HostPort']}:{p['ContainerPort']}/tcp")]
        if item.get('PacketNetworking'):
            if not self.packet_runtime.enabled:
                raise Error('RuntimeUnavailable', 'Restart with --ec2-networking enforce for this instance', 503)
            network = 'container:' + self.packet_runtime.holder(item)
            publish = []
        else:
            network = self.runtime.network(item['VpcId']) if item.get('DockerVpcNetwork') else 'bridge' if ports or item.get('IamInstanceProfile') else 'none'
        if network == 'none' and self.runtime.imds:
            from .ec2_profiles import transport_origin
            try:
                transport_origin(self.engine)
                network = 'bridge'
            except Error:
                pass
        profile = ['--user', '0:0', *[v for capability in ('CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'SETUID', 'SETGID', 'NET_BIND_SERVICE', 'SYS_CHROOT', 'AUDIT_WRITE', 'KILL') for v in ('--cap-add', capability)]] if item.get('GuestMode') else ['--read-only', '--user', '65534:65534']
        image = item.get('RootImage') or self.images[item['ImageId']]
        if item.get('RootImage'):
            self.runtime.owned('image', image)
        command = ['--entrypoint', '/bin/sh', image, '-c', 'trap "exit 0" TERM INT; while :; do sleep 3600 & wait $!; done'] if item.get('GuestMode') else [image]
        return ['--name', 'localcloud-' + item['InstanceId'], '--label', 'localcloud.owner=' + item['Owner'],
                '--memory', {'t3.micro': '128m', 't3.small': '256m', 't3.medium': '512m'}[item['InstanceType']],
                '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                *profile, '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m,mode=1777',
                '--network', network, *publish, *self.runtime.mount_args(mappings if mappings is not None else item.get('BlockDeviceMappings', [])), *command]

    def docker(self, *args, environment=None):
        try:
            options = {'env': {**os.environ, **environment}} if environment is not None else {}
            result = subprocess.run(['docker', *args], capture_output=True, text=True, errors='replace', timeout=30, **options)
        except (OSError, subprocess.TimeoutExpired):
            raise Error('RuntimeUnavailable', 'Docker is unavailable or timed out', 503)
        if result.returncode:
            raise Error('RuntimeFailure', 'Docker operation failed; check the daemon, image and container', 503)
        output = result.stdout + result.stderr if args and args[0] in ('logs', 'exec') else result.stdout
        return output.strip()

    def container(self, instance):
        # Resolve only containers owned by this persistent instance record.
        name = 'localcloud-' + instance['InstanceId']
        labels = json.loads(self.docker('inspect', '--format', '{{json .Config.Labels}}', name))
        if not isinstance(labels, dict) or labels.get('localcloud.owner') != instance['Owner']:
            raise Error('RuntimeFailure', 'Container ownership mismatch', 409)
        return name

    def call(self, operation, args, principal):
        if operation not in OPERATIONS:
            raise Error('UnsupportedOperation', operation, 501)
        if isinstance(args, dict) and 'DryRun' in args:
            if type(args['DryRun']) is not bool:
                raise Error('InvalidParameterValue', 'DryRun must be boolean')
            if args['DryRun']:
                def check():
                    with self.engine.transaction() as data:
                        self.engine.authorize(data, principal, 'ec2:' + operation, '*')
                        raise Error('DryRunOperation', 'Request would have succeeded, but DryRun flag is set', 412)
                return self.engine.tools.call('ec2', operation, principal, '*', check)
            args = {key: value for key, value in args.items() if key != 'DryRun'}
        if operation in PROFILE_OPERATIONS:
            return self.profiles.call(operation, args, principal)
        if operation in TEMPLATE_PARAMETERS:
            return self.templates.call(operation, args, principal)
        if operation == 'RunInstances' and isinstance(args, dict) and 'LaunchTemplate' in args:
            with self.engine.lock:
                data = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                defaults = self.templates.resolve(data, args['LaunchTemplate'])
                identity = defaults.pop('_LaunchTemplate')
                args = {**defaults, **args, 'LaunchTemplate': identity}
        if operation in PARAMETERS:
            return self.infrastructure.call(operation, args, principal)
        if operation in ('CreateTags', 'DeleteTags') and isinstance(args, dict) and isinstance(args.get('Resources'), list) and any(not str(key).startswith('i-') for key in args['Resources']):
            return self.infrastructure.resource_tags(operation, args, principal)
        selectors_allowed = {'InstanceIds', 'InstanceName', 'ContainerId', 'ContainerName', 'HostPort'}
        allowed = {
            'RunInstances': {'ImageId', 'InstanceType', 'MinCount', 'MaxCount', 'PortMappings', 'Name', 'ClientToken', 'TagSpecifications', 'UserData', 'SubnetId', 'SecurityGroupIds', 'PrivateIpAddress', 'KeyName', 'MetadataOptions', 'BlockDeviceMappings', 'LaunchTemplate', 'IamInstanceProfile'},
            'DescribeImages': {'ImageIds', 'Filters', 'Owners', 'MaxResults', 'NextToken'},
            'CreateTags': {'Resources', 'Tags'}, 'DeleteTags': {'Resources', 'Tags'},
            'DescribeTags': {'Filters', 'MaxResults', 'NextToken'},
            'ModifyInstanceMetadataOptions': selectors_allowed | {'HttpTokens', 'HttpEndpoint', 'HttpPutResponseHopLimit'},
            'GetConsoleOutput': selectors_allowed | {'Latest'},
            'DescribeInstanceStatus': selectors_allowed | {'IncludeAllInstances', 'Filters', 'MaxResults', 'NextToken'},
        }.get(operation, selectors_allowed | ({'Name'} if operation == 'NameInstance' else {'InstanceType'} if operation == 'ModifyInstanceAttribute' else {'Attribute'} if operation == 'DescribeInstanceAttribute' else {'Filters', 'MaxResults', 'NextToken'} if operation == 'DescribeInstances' else set()))
        if not isinstance(args, dict) or set(args) - allowed:
            raise Error('UnsupportedOperation', 'Unsupported EC2 parameters', 501)
        from .ec2_infrastructure import validate_input
        validate_input(operation, args)
        ids = args.get('Resources', []) if operation in ('CreateTags', 'DeleteTags') else args.get('InstanceIds', [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or not re.fullmatch(r'i-[0-9a-f]{17}', i) for i in ids):
            raise Error('InvalidInstanceID.Malformed', 'Expected instance IDs')
        selectors = [k for k in ('InstanceName', 'ContainerId', 'ContainerName', 'HostPort') if k in args]
        if len(selectors) + bool(ids) > 1:
            raise Error('InvalidParameterValue', 'Choose exactly one instance selector')
        if operation not in ('RunInstances', 'DescribeInstances', 'DescribeImages', 'DescribeTags', 'DescribeInstanceStatus') and not ids and not selectors:
            raise Error('MissingParameter', 'InstanceIds is required')
        if len(ids) != len(set(ids)):
            raise Error('InvalidParameterValue', 'Duplicate instance IDs')
        resource = '*' if not ids else f'arn:aws:ec2:us-east-1:000000000000:instance/{ids[0]}'
        with self.engine.lock:
            if selectors:
                key = selectors[0]
                value = args[key]
                if key == 'HostPort':
                    if type(value) is not int or not 1024 <= value <= 65535:
                        raise Error('InvalidParameterValue', 'HostPort must be an integer between 1024 and 65535')
                elif not isinstance(value, str) or not value:
                    raise Error('InvalidParameterValue', 'Selector must be a nonempty string')
                if key == 'InstanceName':
                    valid_name(value)
                if key == 'ContainerId' and not re.fullmatch(r'[0-9a-f]{12,64}', value):
                    raise Error('InvalidParameterValue', 'Use a full Docker ID or at least 12 hex characters')
                records = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]).get('instances', {})
                matches = []
                for item in records.values():
                    matched = key == 'InstanceName' and (item.get('Name') == value or any(t['Key'] == 'Name' and t['Value'] == value for t in item.get('Tags', [])))
                    if item['Backend'] == 'docker':
                        if key == 'ContainerName':
                            matched = value == 'localcloud-' + item['InstanceId']
                        elif key == 'ContainerId':
                            container_id = item.get('ContainerId', '')
                            if not container_id and item['State']['Name'] != 'terminated' and self.images:
                                name = self.container(item)
                                container_id = self.docker('inspect', '--format', '{{.Id}}', name)
                            matched = bool(container_id) and container_id.startswith(value)
                        elif key == 'HostPort':
                            matched = item['State']['Name'] != 'terminated' and any(p['HostPort'] == value for p in item.get('PortMappings', []))
                    if matched:
                        matches.append(item['InstanceId'])
                if not matches:
                    raise Error('InvalidInstanceID.NotFound', 'No managed instance matches that selector')
                if len(matches) != 1:
                    raise Error('AmbiguousInstance', 'Multiple instances match; use an EC2 instance ID')
                ids = matches
                resource = f'arn:aws:ec2:us-east-1:000000000000:instance/{ids[0]}'
            return self.engine.tools.call('ec2', operation, principal, resource,
                                         lambda: self.perform(operation, args, principal, ids))

    def perform(self, operation, args, principal, ids):
        with self.engine.transaction() as data:
            for identifier in ids or ['*']:
                resource = '*' if identifier == '*' else f'arn:aws:ec2:us-east-1:000000000000:instance/{identifier}'
                self.engine.authorize(data, principal, 'ec2:' + operation, resource)
            instances = data.setdefault('instances', {})
            if operation == 'DescribeImages':
                return self.image_lifecycle.describe(data, args)
            for identifier in ids:
                if identifier not in instances:
                    raise Error('InvalidInstanceID.NotFound', "The instance ID '" + identifier + "' does not exist", 400)
            if operation in ('DescribeInstances', 'DescribeInstanceStatus', 'GetConsoleOutput', 'RebootInstances',
                             'ModifyInstanceMetadataOptions', 'ModifyInstanceAttribute',
                             'StartInstances', 'StopInstances', 'TerminateInstances'):
                for item in ([instances[key] for key in ids] if ids else instances.values()):
                    if item['Backend'] in ('docker', 'kubernetes') and item['Backend'] != self.backend:
                        raise Error('RuntimeUnavailable', 'Restart with the matching EC2 executor for this state database', 503)
            if operation in ('CreateTags', 'DeleteTags'):
                tags = validated_tags(args.get('Tags'), delete=operation == 'DeleteTags')
                for identifier in ids:
                    item = instances[identifier]
                    current = {tag['Key']: tag['Value'] for tag in item.get('Tags', [])}
                    if operation == 'CreateTags':
                        current.update({t['Key']: t['Value'] for t in tags})
                    else:
                        for tag in tags:
                            if 'Value' not in tag or current.get(tag['Key']) == tag['Value']:
                                current.pop(tag['Key'], None)
                    if len(current) > 50:
                        raise Error('TagLimitExceeded', 'At most 50 tags are supported')
                    item['Tags'] = [{'Key': k, 'Value': v} for k, v in current.items()]
                return {}
            if operation == 'DescribeTags':
                tags = [{'ResourceId': i['InstanceId'], 'ResourceType': 'instance', **tag}
                        for i in instances.values() for tag in i.get('Tags', [])]
                for kind, (collection, field, prefix, _) in COLLECTIONS.items():
                    resource_type = {'SecurityGroup': 'security-group', 'InternetGateway': 'internet-gateway', 'RouteTable': 'route-table', 'KeyPair': 'key-pair', 'NetworkInterface': 'network-interface', 'Address': 'elastic-ip'}.get(kind, kind.lower())
                    tags.extend({'ResourceId': i[field], 'ResourceType': resource_type, **tag} for i in data.get('ec2', {}).get(collection, {}).values() for tag in i.get('TagSet' if kind == 'NetworkInterface' else 'Tags', []))
                tags.extend({'ResourceId': i['ImageId'], 'ResourceType': 'image', **tag} for i in self.image_lifecycle.store(data).values() if not i.get('_deregistered') for tag in i.get('Tags', []))
                tags.extend({'ResourceId': i['LaunchTemplateId'], 'ResourceType': 'launch-template', **tag} for i in data.get('ec2', {}).get('launch_templates', {}).values() for tag in i.get('Tags', []))
                return paginated({'Tags': filtered(tags, args.get('Filters', []), 'tag')}, 'Tags', args)
            if operation == 'DescribeInstanceStatus':
                chosen = [instances[i] for i in ids] if ids else list(instances.values())
                chosen = filtered(chosen, args.get('Filters', []), 'instance')
                values = []
                for item in chosen:
                    if item['Backend'] == 'kubernetes' and item['State']['Name'] != 'terminated':
                        self.kube_runtime(item).refresh(item)
                    if item['Backend'] == 'docker' and item['State']['Name'] != 'terminated':
                        item['State'] = state('running' if self.docker('inspect', '--format', '{{.State.Running}}', self.container(item)) == 'true' else 'stopped')
                    running = item['State']['Name'] == 'running'
                    if running or args.get('IncludeAllInstances', False):
                        health = {'Status': 'ok' if running else 'not-applicable', 'Details': [{'Name': 'reachability', 'Status': 'passed'}] if running else []}
                        values.append({'InstanceId': item['InstanceId'], 'AvailabilityZone': item.get('Placement', {}).get('AvailabilityZone', 'us-east-1a'), 'InstanceState': item['State'], 'InstanceStatus': health, 'SystemStatus': health})
                return paginated({'InstanceStatuses': values}, 'InstanceStatuses', args)
            if operation == 'GetConsoleOutput':
                if len(ids) != 1:
                    raise Error('InvalidParameterValue', 'Select one instance')
                item = instances[ids[0]]
                output = base64.b64decode(item.get('ConsoleOutput', '')).decode('utf-8', errors='replace')
                if item['Backend'] == 'kubernetes' and item['State']['Name'] != 'terminated':
                    output += '\n' + self.kube_runtime(item).console(item)
                if item['Backend'] == 'docker' and item['State']['Name'] != 'terminated':
                    output += '\n' + self.docker('logs', '--tail', '200', self.container(item))
                output = output[-65536:]
                return {'InstanceId': item['InstanceId'], 'Timestamp': datetime.now(timezone.utc).isoformat(), 'Output': base64.b64encode(output.encode()).decode()}
            if operation == 'RebootInstances':
                for key in ids:
                    item = instances[key]
                    if item['Backend'] == 'kubernetes':
                        self.kube_runtime(item).refresh(item)
                    if item['State']['Name'] != 'running':
                        raise Error('IncorrectInstanceState', 'Only running instances can be rebooted')
                for key in ids:
                    item = instances[key]
                    if item['Backend'] == 'kubernetes':
                        self.kube_runtime(item).lifecycle(item, 'reboot')
                    if item['Backend'] == 'docker':
                        self.runtime.imds_action(item, 'stop')
                        self.docker('restart', '-t', '5', self.container(item))
                        self.runtime.start_imds(item)
                        self.runtime.start_guest(item)
                return {}
            if operation == 'ModifyInstanceMetadataOptions':
                if len(ids) != 1:
                    raise Error('InvalidParameterValue', 'Select one instance')
                item = instances[ids[0]]
                if item['State']['Name'] == 'terminated':
                    raise Error('IncorrectInstanceState', 'Instance is terminated')
                options = metadata_options({**item.get('MetadataOptions', {}), **{k: v for k, v in args.items() if k in ('HttpTokens', 'HttpEndpoint', 'HttpPutResponseHopLimit')}})
                if item['Backend'] == 'kubernetes':
                    self.kube_runtime(item).update_metadata(item, options)
                    item['MetadataOptions'] = options
                    return {'InstanceId': item['InstanceId'], 'InstanceMetadataOptions': options}
                old = item.get('MetadataOptions')
                if item.get('ImdsEnabled'):
                    self.runtime.imds_action(item, 'rm')
                item['MetadataOptions'] = options
                if item.get('ImdsEnabled') and item['State']['Name'] == 'running':
                    try:
                        self.runtime.start_imds(item)
                    except Error:
                        item['MetadataOptions'] = old
                        raise
                return {'InstanceId': item['InstanceId'], 'InstanceMetadataOptions': {**options, 'State': 'applied'}}
            if operation == 'DescribeInstanceAttribute':
                if len(ids) != 1 or args.get('Attribute') not in ('instanceType', 'userData'):
                    raise Error('UnsupportedOperation', 'Supported attributes: instanceType, userData', 501)
                item = instances[ids[0]]
                key = 'InstanceType' if args['Attribute'] == 'instanceType' else 'UserData'
                return {'InstanceId': ids[0], key: {'Value': item.get(key, '')}}
            if 'Name' in args and operation != 'RunInstances':
                valid_name(args['Name'])
                if any(i.get('Name') == args['Name'] and i['InstanceId'] not in ids for i in instances.values()):
                    raise Error('InvalidParameterValue', 'Instance name already exists; choose a unique name')
            if operation == 'NameInstance':
                if len(ids) != 1 or 'Name' not in args:
                    raise Error('InvalidParameterValue', 'Naming requires one instance and a Name')
                instances[ids[0]]['Name'] = args['Name']
                return {'Instance': self.public(instances[ids[0]])}
            if operation == 'ModifyInstanceAttribute':
                kind = args.get('InstanceType')
                if len(ids) != 1 or kind not in ('t3.micro', 't3.small', 't3.medium'):
                    raise Error('InvalidParameterValue', 'Select one instance and a supported InstanceType')
                item = instances[ids[0]]
                if item['State']['Name'] != 'stopped':
                    raise Error('IncorrectInstanceState', 'Stop the instance before modifying its type')
                if item['Backend'] == 'kubernetes':
                    self.kube_runtime(item).modify_type(item, kind)
                if item['Backend'] == 'docker':
                    if not self.images:
                        raise Error('RuntimeUnavailable', 'Docker EC2 mode is required', 503)
                    name = self.container(item)
                    if self.docker('inspect', '--format', '{{.State.Running}}', name) == 'true':
                        raise Error('IncorrectInstanceState', 'Docker container is still running; refresh its state and stop it')
                    memory = {'t3.micro': 128, 't3.small': 256, 't3.medium': 512}[kind]
                    self.docker('update', '--memory', str(memory) + 'm', '--memory-swap', str(memory * 2) + 'm', name)
                item['InstanceType'] = kind
                return {'Return': True, 'Instance': self.public(item)}
            if operation == 'RunInstances':
                token = args.get('ClientToken', '')
                if not isinstance(token, str) or len(token) > 64 or any(ord(c) < 32 or ord(c) > 126 for c in token):
                    raise Error('InvalidParameterValue', 'ClientToken must be at most 64 printable ASCII characters')
                image_data = self.image_lifecycle.store(data).get(args.get('ImageId'))
                if self.images or image_data:
                    image_data = self.image_lifecycle.resolve(data, args.get('ImageId'))
                    if image_data.get('BlockDeviceMappings') and 'BlockDeviceMappings' not in args:
                        args = {**args, 'BlockDeviceMappings': copy.deepcopy(image_data['BlockDeviceMappings'])}
                normalized = dict(ImageId=args.get('ImageId'), InstanceType=args.get('InstanceType', 't3.micro'),
                                  MinCount=integer(args.get('MinCount', 1), 1, 10), MaxCount=integer(args.get('MaxCount', 1), 1, 10),
                                  PortMappings=args.get('PortMappings', []), TagSpecifications=args.get('TagSpecifications', []),
                                  UserData=args.get('UserData', ''))
                normalized.update({k: v for k, v in args.items() if k in ('SubnetId', 'SecurityGroupIds', 'PrivateIpAddress', 'KeyName', 'MetadataOptions', 'BlockDeviceMappings', 'LaunchTemplate', 'IamInstanceProfile')})
                if 'Name' in args:
                    normalized['Name'] = args['Name']
                fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                replay = [i for i in instances.values() if token and i.get('ClientToken') == token]
                if replay:
                    if any(i.get('LaunchFingerprint') != fingerprint for i in replay):
                        raise Error('IdempotentParameterMismatch', 'ClientToken was used with different launch parameters')
                    return {'ReservationId': replay[0]['ReservationId'], 'OwnerId': '000000000000', 'Groups': [], 'Instances': [self.public(i) for i in replay]}
                if 'Name' in args:
                    valid_name(args['Name'])
                    if any(i.get('Name') == args['Name'] for i in instances.values()):
                        raise Error('InvalidParameterValue', 'Instance name already exists; choose a unique name')
                tags = launch_tags(args.get('TagSpecifications', []))
                user_data = args.get('UserData', '')
                if not isinstance(user_data, str):
                    raise Error('InvalidParameterValue', 'UserData must be base64 text')
                try:
                    decoded = base64.b64decode(user_data, validate=True)
                except ValueError:
                    raise Error('InvalidParameterValue', 'UserData must be valid base64')
                if len(decoded) > 16384 or b'\x00' in decoded:
                    raise Error('InvalidParameterValue', 'UserData must be at most 16 KiB without NUL bytes')
                image = args.get('ImageId', '')
                if not isinstance(image, str) or not re.fullmatch(r'ami-[a-zA-Z0-9-]{1,80}', image):
                    raise Error('InvalidAMIID.Malformed', 'Use an ami- identifier')
                minimum = integer(args.get('MinCount', 1), 1, 10)
                count = integer(args.get('MaxCount', 1), minimum, 10)
                if 'Name' in args and count != 1:
                    raise Error('InvalidParameterValue', 'A named launch requires --count 1')
                kind = args.get('InstanceType', 't3.micro')
                if kind not in ('t3.micro', 't3.small', 't3.medium'):
                    raise Error('InvalidParameterValue', 'Supported types: t3.micro, t3.small, t3.medium')
                if self.images and image not in self.images:
                    raise Error('InvalidAMIID.NotFound', 'Image is not in the configured allowlist')
                if self.images and count != 1:
                    raise Error('InvalidParameterValue', 'Container executors support one instance per launch')
                if sum(i['State']['Name'] != 'terminated' for i in instances.values()) + count > 100:
                    raise Error('InstanceLimitExceeded', 'Local limit is 100 active instances')
                ports = port_mappings(args.get('PortMappings', []), dynamic=self.kubernetes is not None)
                if ports and not self.images:
                    raise Error('InvalidParameterValue', 'Published ports require Docker EC2 mode (--ec2-image)')
                occupied = {p['HostPort'] for i in instances.values() if i['State']['Name'] != 'terminated' for p in i.get('PortMappings', [])}
                if self.backend == 'docker' and any(p['HostPort'] in occupied for p in ports):
                    raise Error('InvalidParameterValue', 'A host port is already assigned to another instance; choose another port or terminate that instance')
                profile = None
                if 'IamInstanceProfile' in args:
                    self.profiles.require_runtime()
                    profile = self.profiles.profile(data, args['IamInstanceProfile'], principal)
                created = []
                reservation = 'r-' + uuid.uuid4().hex[:17]
                for _ in range(count):
                    identifier = 'i-' + uuid.uuid4().hex[:17]
                    item = {'InstanceId': identifier, 'ImageId': image, 'InstanceType': kind,
                            'State': state('running'), 'LaunchTime': datetime.now(timezone.utc).isoformat(),
                            'Backend': self.backend, 'Owner': uuid.uuid4().hex,
                            'ReservationId': reservation, 'Tags': tags, 'ClientToken': token,
                            'LaunchFingerprint': fingerprint, 'UserData': user_data, 'AmiLaunchIndex': len(created)}
                    if profile is not None:
                        self.profiles.attach(data, item, profile)
                    if 'LaunchTemplate' in args:
                        item['LaunchTemplate'] = copy.deepcopy(args['LaunchTemplate'])
                    item.update(self.infrastructure.launch_network(data, args))
                    self.infrastructure.primary_interface(data, item)
                    if self.backend == 'docker' and self.runtime.guest_mode:
                        item['GuestMode'] = True
                        if args.get('KeyName'):
                            item['SshPublicKey'] = next(k['PublicKey'] for k in data['ec2']['keys'].values() if k['KeyName'] == args['KeyName'])
                    item['MetadataOptions'] = metadata_options(args.get('MetadataOptions', {}))
                    if ports:
                        item['PortMappings'] = ports
                    if self.images:
                        ingress = self.infrastructure.published_ingress(data, item, ports)
                        if ingress:
                            item['PortMappings'] = ingress
                        if item.get('SshPublicKey') and not any(p['ContainerPort'] == 22 for p in item.get('PortMappings', [])):
                            item.setdefault('PortMappings', []).append({'HostPort': 0, 'ContainerPort': 22})
                    created_volumes = []
                    if args.get('BlockDeviceMappings'):
                        item['BlockDeviceMappings'], created_volumes = self.infrastructure.block_devices(data, item, args['BlockDeviceMappings'])
                    if self.backend == 'docker' and args.get('SubnetId'):
                        item['DockerVpcNetwork'] = True
                    if self.backend == 'docker' and self.runtime.imds:
                        item['ImdsEnabled'] = True
                    if 'Name' in args:
                        item['Name'] = args['Name']
                    if self.kubernetes is not None:
                        try:
                            self.kubernetes.launch(item, decoded)
                        except Exception as failure:
                            for volume in created_volumes:
                                try:
                                    self.runtime.delete_volume(volume)
                                except Error as cleanup_error:
                                    failure.add_note('Kubernetes launch volume cleanup failed: ' + str(cleanup_error))
                            raise
                    elif self.images:
                        publish = [part for p in ports for part in ('--publish', f"127.0.0.1:{p['HostPort']}:{p['ContainerPort']}/tcp")]
                        try:
                            if self.packet_runtime.enabled:
                                self.packet_runtime.prepare(data, item)
                            item['ContainerId'] = self.docker('run', '-d', *self.container_args(item))
                            if any(p['HostPort'] == 0 for p in item.get('PortMappings', [])):
                                published = json.loads(self.docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', self.packet_runtime.holder(item) if item.get('PacketNetworking') else 'localcloud-' + identifier))
                                for port in item['PortMappings']:
                                    port['HostPort'] = int(published[str(port['ContainerPort']) + '/tcp'][0]['HostPort'])
                            seed = None
                            if item.get('IamInstanceProfile'):
                                try:
                                    seed = self.engine.identity.instance_credentials_from_state(data, item)
                                except Error:
                                    # Empty profiles and roles without EC2 trust
                                    # can be attached but cannot issue credentials.
                                    pass
                            self.runtime.start_imds(item, seed=seed)
                            self.runtime.start_guest(item)
                            if decoded:
                                try:
                                    script = decoded.decode('utf-8')
                                except UnicodeDecodeError:
                                    raise Error('InvalidParameterValue', 'Docker UserData scripts must be UTF-8')
                                console = self.docker('exec', '--user', '0:0' if item.get('GuestMode') else '65534:65534', 'localcloud-' + identifier, '/bin/sh', '-c', script)
                                item['ConsoleOutput'] = base64.b64encode(console.encode()[-65536:]).decode()
                        except (Error, ValueError, TypeError, KeyError) as error:
                            # Docker can create a stopped container before a port-bind error.
                            # Remove it only after verifying our unique ownership label.
                            failure = error if isinstance(error, Error) else Error('RuntimeFailure', 'Docker returned invalid runtime metadata', 503)
                            if item.get('PacketNetworking'):
                                try:
                                    self.packet_runtime.remove(item)
                                except Error as cleanup_error:
                                    failure.add_note('Packet namespace cleanup failed: ' + str(cleanup_error))
                            try:
                                self.runtime.imds_action(item, 'rm')
                            except (Error, ValueError, TypeError) as cleanup_error:
                                failure.add_note('Launch metadata-helper cleanup failed: ' + str(cleanup_error))
                            try:
                                name = self.container(item)
                                self.docker('rm', '-f', name)
                            except (Error, ValueError, TypeError) as cleanup_error:
                                failure.add_note('Launch container cleanup failed: ' + str(cleanup_error))
                            for key in created_volumes:
                                try:
                                    self.runtime.delete_volume(key)
                                except (Error, ValueError, TypeError) as cleanup_error:
                                    failure.add_note('Launch volume cleanup failed for ' + key + ': ' + str(cleanup_error))
                            if isinstance(error, Error):
                                raise
                            raise failure from error
                    instances[identifier] = item
                    response = self.public(item)
                    response['State'] = state('pending')
                    created.append(response)
                return {'ReservationId': reservation, 'OwnerId': '000000000000', 'Groups': [], 'Instances': created}
            chosen = [instances[i] for i in ids] if ids else list(instances.values())
            if operation == 'DescribeInstances':
                for item in chosen:
                    if item['Backend'] == 'kubernetes' and item['State']['Name'] != 'terminated':
                        self.kube_runtime(item).refresh(item)
                    if 'ec2' in data and 'NetworkInterfaces' in item:
                        item['NetworkInterfaces'] = [self.infrastructure.interface_view(n) for n in data['ec2']['interfaces'].values() if n.get('Attachment', {}).get('InstanceId') == item['InstanceId']]
                    if item['Backend'] == 'docker' and item['State']['Name'] != 'terminated':
                        if not self.images:
                            raise Error('RuntimeUnavailable', 'Restart with Docker EC2 image mappings to inspect these instances', 503)
                        name = self.container(item)
                        if not item.get('ContainerId'):
                            item['ContainerId'] = self.docker('inspect', '--format', '{{.Id}}', name)
                        running = self.docker('inspect', '--format', '{{.State.Running}}', name)
                        item['State'] = state('running' if running == 'true' else 'stopped')
                chosen = filtered(chosen, args.get('Filters', []), 'instance')
                page = paginated({'Instances': chosen}, 'Instances', args)
                reservations = {}
                for item in page['Instances']:
                    key = item.get('ReservationId', 'r-' + item['InstanceId'][2:])
                    reservations.setdefault(key, {'ReservationId': key, 'OwnerId': '000000000000', 'Groups': [], 'Instances': []})['Instances'].append(self.public(item))
                return {'Reservations': list(reservations.values()), **({'NextToken': page['NextToken']} if 'NextToken' in page else {})}
            if any(i['Backend'] == 'docker' for i in chosen) and (not self.images or len(chosen) != 1):
                raise Error('InvalidParameterValue', 'Docker lifecycle requires Docker mode and exactly one instance')
            target = {'StartInstances': 'running', 'StopInstances': 'stopped', 'TerminateInstances': 'terminated'}[operation]
            if target != 'terminated' and any(i['State']['Name'] == 'terminated' for i in chosen):
                raise Error('IncorrectInstanceState', 'Terminated instances cannot be started or stopped')
            changes = []
            for item in chosen:
                if item['Backend'] == 'kubernetes':
                    runtime = self.kube_runtime(item)
                    if item['State']['Name'] != 'terminated':
                        runtime.refresh(item)
                    previous = copy.deepcopy(item['State'])
                    if target in ('stopped', 'terminated') and previous['Name'] not in ('stopped', 'terminated'):
                        try:
                            console = runtime.console(item)
                            if isinstance(console, str) and console:
                                item['ConsoleOutput'] = base64.b64encode(console.encode()[-65536:]).decode()
                        except Error:
                            pass
                    if target == 'terminated':
                        runtime.lifecycle(item, 'terminate')
                        self.profiles.terminate(data, item)
                        self.infrastructure.terminate(data, item)
                    elif previous['Name'] != target:
                        runtime.lifecycle(item, 'start' if target == 'running' else 'stop')
                    changes.append({'InstanceId': item['InstanceId'], 'PreviousState': previous, 'CurrentState': copy.deepcopy(item['State'])})
                    continue
                # Refresh external Docker exits before deciding whether start/stop is needed.
                if item['Backend'] == 'docker' and item['State']['Name'] != 'terminated':
                    name = self.container(item)
                    item['State'] = state('running' if self.docker('inspect', '--format', '{{.State.Running}}', name) == 'true' else 'stopped')
                previous = copy.deepcopy(item['State'])
                if item['Backend'] == 'docker' and previous['Name'] != target:
                    name = self.container(item)
                    if target == 'terminated':
                        try:
                            console = base64.b64decode(item.get('ConsoleOutput', '')).decode('utf-8', errors='replace') + '\n' + self.docker('logs', '--tail', '200', name)
                            item['ConsoleOutput'] = base64.b64encode(console.encode()[-65536:]).decode()
                        except Error:
                            pass
                    self.runtime.imds_action(item, 'rm' if target == 'terminated' else 'stop')
                    if item.get('PacketNetworking') and target == 'running':
                        self.packet_runtime.start(data, item)
                    self.docker(*({'running': ['start', name], 'stopped': ['stop', '-t', '5', name],
                                  'terminated': ['rm', '-f', name]}[target]))
                    if item.get('PacketNetworking') and target == 'stopped':
                        self.packet_runtime.stop(item)
                    if target == 'running':
                        self.runtime.start_imds(item)
                        self.runtime.start_guest(item)
                if target == 'terminated':
                    self.profiles.terminate(data, item)
                    self.infrastructure.terminate(data, item)
                    if item['Backend'] == 'docker':
                        if item.get('PacketNetworking'):
                            self.packet_runtime.remove(item)
                        self.runtime.delete_root_image(item)
                        item.pop('RootImage', None)
                item['State'] = state(target)
                reported = {'running': 'pending', 'stopped': 'stopping', 'terminated': 'shutting-down'}[target] if item['Backend'] == 'mock' else target
                changes.append({'InstanceId': item['InstanceId'], 'PreviousState': previous, 'CurrentState': state(reported)})
            return {{'StartInstances': 'StartingInstances', 'StopInstances': 'StoppingInstances', 'TerminateInstances': 'TerminatingInstances'}[operation]: changes}

    @staticmethod
    def public(item):
        result = {k: copy.deepcopy(v) for k, v in item.items() if k not in ('Owner', 'LaunchFingerprint', 'UserData', 'ReservationId', 'SshPublicKey', 'ConsoleOutput')}
        if item['Backend'] == 'docker':
            result['ContainerName'] = 'localcloud-' + item['InstanceId']
        if item['Backend'] != 'kubernetes':
            result['Endpoints'] = [f"http://127.0.0.1:{p['HostPort']}" for p in item.get('PortMappings', [])]
        return result


def validate_instances(instances):
    if not isinstance(instances, dict):
        raise ValueError('Invalid EC2 instance collection')
    required = {'InstanceId', 'ImageId', 'InstanceType', 'State', 'LaunchTime', 'Backend', 'Owner'}
    names = []
    for key, item in instances.items():
        optional = {'Name', 'ReservationId', 'Tags', 'ClientToken', 'LaunchFingerprint', 'UserData', 'AmiLaunchIndex',
                    'VpcId', 'SubnetId', 'PrivateIpAddress', 'PrivateDnsName', 'PublicIpAddress', 'SecurityGroups', 'Placement',
                    'KeyName', 'MetadataOptions', 'BlockDeviceMappings', 'NetworkInterfaces', 'ConsoleOutput', 'LaunchTemplate', 'IamInstanceProfile'}
        if (not isinstance(item, dict) or set(item) - required - optional or not required <= set(item) or key != item['InstanceId']
                or not re.fullmatch(r'i-[0-9a-f]{17}', key)
                or item['Backend'] != 'mock' or item['State'] not in [state(n) for n in ('running', 'stopped', 'terminated')]
                or not all(isinstance(item[k], str) for k in required - {'State'})
                or not re.fullmatch(r'ami-[a-zA-Z0-9-]{1,80}', item['ImageId'])
                or item['InstanceType'] not in ('t3.micro', 't3.small', 't3.medium')
                or not re.fullmatch(r'[0-9a-f]{32}', item['Owner'])):
            raise ValueError('Invalid EC2 record; portable state supports mock instances only')
        try:
            validated_tags(item.get('Tags', []))
            if 'ConsoleOutput' in item and (not isinstance(item['ConsoleOutput'], str) or len(item['ConsoleOutput']) > 87384 or len(base64.b64decode(item['ConsoleOutput'], validate=True)) > 65536):
                raise ValueError('Invalid console output')
            if 'IamInstanceProfile' in item and (not isinstance(item['IamInstanceProfile'], dict) or set(item['IamInstanceProfile']) != {'Arn', 'Id'} or any(not isinstance(v, str) for v in item['IamInstanceProfile'].values())):
                raise ValueError('Invalid instance profile')
            if 'MetadataOptions' in item:
                metadata_options(item['MetadataOptions'])
            from .ec2_infrastructure import validate_instance_fields
            validate_instance_fields(item)
            if ('ReservationId' in item and not re.fullmatch(r'r-[0-9a-f]{17}', item['ReservationId'])
                    or 'LaunchFingerprint' in item and not re.fullmatch(r'[0-9a-f]{64}', item['LaunchFingerprint'])
                    or not isinstance(item.get('ClientToken', ''), str) or len(item.get('ClientToken', '')) > 64
                    or not isinstance(item.get('UserData', ''), str)
                    or len(base64.b64decode(item.get('UserData', ''), validate=True)) > 16384
                    or type(item.get('AmiLaunchIndex', 0)) is not int or not 0 <= item.get('AmiLaunchIndex', 0) < 10):
                raise ValueError('Invalid EC2 launch metadata')
        except (Error, TypeError) as error:
            raise ValueError('Invalid EC2 metadata') from error
        if 'Name' in item:
            valid_name(item['Name'])
            names.append(item['Name'])
    if len(names) != len(set(names)):
        raise ValueError('Duplicate EC2 instance names')


def query_request(body):
    from urllib.parse import parse_qs
    fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=4096)
    if any(len(v) != 1 for v in fields.values()):
        raise Error('InvalidParameterValue', 'Duplicate query parameters')
    fields = {k: v[0] for k, v in fields.items()}
    operation = fields.pop('Action', '')
    version = fields.pop('Version', '2016-11-15')
    if version != '2016-11-15':
        raise Error('UnsupportedOperation', 'Only EC2 API version 2016-11-15 is supported', 501)
    # EC2 Query uses indexed lists and nested tag/filter structures, unlike JSON.
    tree = {}
    for field, value in fields.items():
        node = tree
        path = field.split('.')
        for part in path[:-1]:
            existing = node.setdefault(part, {})
            if not isinstance(existing, dict):
                raise Error('InvalidParameterValue', 'Conflicting query parameters')
            node = existing
        if path[-1] in node:
            raise Error('InvalidParameterValue', 'Conflicting query parameters')
        node[path[-1]] = value
    aliases = {'InstanceId': 'InstanceIds', 'ImageId': 'ImageIds', 'ResourceId': 'Resources',
               'TagSpecification': 'TagSpecifications', 'Tag': 'Tags', 'Filter': 'Filters',
               'Value': 'Values', 'Owner': 'Owners'}
    def convert(value):
        if not isinstance(value, dict):
            return value
        if value and all(k.isdigit() for k in value):
            indexes = sorted(map(int, value))
            if indexes != list(range(1, len(indexes) + 1)):
                raise Error('InvalidParameterValue', 'Query list indices must start at 1 without gaps')
            return [convert(value[str(i)]) for i in indexes]
        result = {}
        for key, child in value.items():
            name = aliases.get(key, key) if isinstance(child, dict) and child and all(i.isdigit() for i in child) else key
            if name in result:
                raise Error('InvalidParameterValue', 'Conflicting query parameters')
            result[name] = convert(child)
        return result
    fields = convert(tree)
    from .ec2_protocol import decode
    fields = decode(operation, fields)
    if operation in ('ModifyInstanceAttribute', 'DescribeInstanceAttribute', 'GetConsoleOutput', 'ModifyInstanceMetadataOptions'):
        if 'InstanceId' in fields:
            if 'InstanceIds' in fields:
                raise Error('InvalidParameterValue', 'Choose one instance selector')
            fields['InstanceIds'] = [fields.pop('InstanceId')]
        if isinstance(fields.get('InstanceType'), dict) and set(fields['InstanceType']) == {'Value'}:
            fields['InstanceType'] = fields['InstanceType']['Value']
    return operation, fields


def query_response(operation, result, request_id=None):
    from .ec2_protocol import encode
    response = encode(operation, result, request_id or str(uuid.uuid4()))
    if response is not None:
        return response
    from xml.etree.ElementTree import Element, SubElement, tostring
    root = Element(operation + 'Response', xmlns='http://ec2.amazonaws.com/doc/2016-11-15/')
    SubElement(root, 'requestId').text = request_id or str(uuid.uuid4())
    if operation in ('ModifyInstanceAttribute', 'CreateTags', 'DeleteTags'):
        return tostring(root, encoding='unicode')
    names = {'Instances': 'instancesSet', 'Reservations': 'reservationSet', 'State': 'instanceState',
             'StartingInstances': 'instancesSet', 'StoppingInstances': 'instancesSet', 'TerminatingInstances': 'instancesSet',
             'Tags': 'tagSet', 'Images': 'imagesSet', 'SecurityGroups': 'groupSet', 'Groups': 'groupSet'}
    def write(parent, values):
        for key, value in values.items():
            if key in ('Backend', 'PortMappings', 'Endpoints', 'ContainerId', 'ContainerName') or (key == 'Name' and 'InstanceId' in values):
                continue
            image_names = {'State': 'imageState', 'OwnerId': 'imageOwnerId', 'Public': 'isPublic'} if operation == 'DescribeImages' and 'ImageId' in values else {}
            node = SubElement(parent, image_names.get(key, names.get(key, key[0].lower() + key[1:])))
            if isinstance(value, dict):
                write(node, value)
            elif isinstance(value, list):
                for item in value:
                    write(SubElement(node, 'item'), item)
            else:
                node.text = str(value).lower() if isinstance(value, bool) else str(value)
    write(root, result)
    return tostring(root, encoding='unicode')


def port_mappings(value, dynamic=False):
    if not isinstance(value, list) or len(value) > 32:
        raise Error('InvalidParameterValue', 'PortMappings must be a list of at most 32 TCP mappings')
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'HostPort', 'ContainerPort'}:
            raise Error('InvalidParameterValue', 'Each port mapping requires HostPort and ContainerPort')
        if any(type(item[k]) is not int or not ((dynamic and k == 'HostPort' and item[k] == 0) or
                  (1024 if k == 'HostPort' else 1) <= item[k] <= 65535) for k in item):
            raise Error('InvalidParameterValue', 'Host ports must be 1024–65535 and container ports 1–65535')
        result.append(dict(item))
    fixed = [p['HostPort'] for p in result if p['HostPort'] != 0]
    if len(set(fixed)) != len(fixed) or (dynamic and len({p['ContainerPort'] for p in result}) != len(result)):
        raise Error('InvalidParameterValue', 'Duplicate host port')
    return result


def parse_publish(value):
    if not re.fullmatch(r'[0-9]+:[0-9]+', value):
        raise ValueError('--publish must be HOST_PORT:CONTAINER_PORT (localhost TCP only)')
    host, container = map(int, value.split(':'))
    return port_mappings([{'HostPort': host, 'ContainerPort': container}], dynamic=True)[0]
