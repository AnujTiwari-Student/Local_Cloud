"""Small EC2 lifecycle provider; optional bounded Docker execution."""
import copy
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from .engine import Error, integer, valid_name

CODES = {'running': 16, 'stopped': 80, 'terminated': 48}
OPERATIONS = {'RunInstances', 'DescribeInstances', 'StartInstances', 'StopInstances', 'TerminateInstances', 'NameInstance', 'ModifyInstanceAttribute'}


def state(name):
    return {'Code': CODES[name], 'Name': name}


class EC2:
    def __init__(self, engine, images=None):
        self.engine = engine
        self.images = images or {}

    def docker(self, *args):
        try:
            result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise Error('RuntimeUnavailable', 'Docker is unavailable or timed out', 503)
        if result.returncode:
            raise Error('RuntimeFailure', 'Docker operation failed; check the daemon, image and container', 503)
        return result.stdout.strip()

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
        allowed = {'ImageId', 'InstanceType', 'MinCount', 'MaxCount', 'PortMappings', 'Name'} if operation == 'RunInstances' else {'InstanceIds', 'InstanceName', 'ContainerId', 'ContainerName', 'HostPort'} | ({'Name'} if operation == 'NameInstance' else {'InstanceType'} if operation == 'ModifyInstanceAttribute' else set())
        if not isinstance(args, dict) or set(args) - allowed:
            raise Error('UnsupportedOperation', 'Unsupported EC2 parameters', 501)
        ids = args.get('InstanceIds', [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or not re.fullmatch(r'i-[0-9a-f]{17}', i) for i in ids):
            raise Error('InvalidInstanceID.Malformed', 'Expected instance IDs')
        selectors = [k for k in ('InstanceName', 'ContainerId', 'ContainerName', 'HostPort') if k in args]
        if len(selectors) + bool(ids) > 1:
            raise Error('InvalidParameterValue', 'Choose exactly one instance selector')
        if operation not in ('RunInstances', 'DescribeInstances') and not ids and not selectors:
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
                    matched = key == 'InstanceName' and item.get('Name') == value
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
            for identifier in ids:
                if identifier not in instances:
                    raise Error('InvalidInstanceID.NotFound', identifier, 400)
            if 'Name' in args:
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
                    raise Error('InvalidParameterValue', 'Docker mode supports one instance per launch')
                if sum(i['State']['Name'] != 'terminated' for i in instances.values()) + count > 100:
                    raise Error('InstanceLimitExceeded', 'Local limit is 100 active instances')
                ports = port_mappings(args.get('PortMappings', []))
                if ports and not self.images:
                    raise Error('InvalidParameterValue', 'Published ports require Docker EC2 mode (--ec2-image)')
                occupied = {p['HostPort'] for i in instances.values() if i['State']['Name'] != 'terminated' for p in i.get('PortMappings', [])}
                if any(p['HostPort'] in occupied for p in ports):
                    raise Error('InvalidParameterValue', 'A host port is already assigned to another instance; choose another port or terminate that instance')
                created = []
                for _ in range(count):
                    identifier = 'i-' + uuid.uuid4().hex[:17]
                    item = {'InstanceId': identifier, 'ImageId': image, 'InstanceType': kind,
                            'State': state('running'), 'LaunchTime': datetime.now(timezone.utc).isoformat(),
                            'Backend': 'docker' if self.images else 'mock', 'Owner': uuid.uuid4().hex}
                    if 'Name' in args:
                        item['Name'] = args['Name']
                    if ports:
                        item['PortMappings'] = ports
                    if self.images:
                        publish = [part for p in ports for part in ('--publish', f"127.0.0.1:{p['HostPort']}:{p['ContainerPort']}/tcp")]
                        try:
                            item['ContainerId'] = self.docker('run', '-d', '--name', 'localcloud-' + identifier,
                                        '--label', 'localcloud.owner=' + item['Owner'],
                                        '--memory', {'t3.micro': '128m', 't3.small': '256m', 't3.medium': '512m'}[kind],
                                        '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL',
                                        '--security-opt', 'no-new-privileges', '--read-only', '--user', '65534:65534',
                                        '--network', 'bridge' if ports else 'none', *publish, self.images[image])
                        except Error:
                            # Docker can create a stopped container before a port-bind error.
                            # Remove it only after verifying our unique ownership label.
                            try:
                                name = self.container(item)
                                self.docker('rm', '-f', name)
                            except (Error, ValueError, TypeError):
                                pass
                            raise
                    instances[identifier] = item
                    created.append(self.public(item))
                return {'ReservationId': 'r-' + uuid.uuid4().hex[:17], 'Instances': created}
            chosen = [instances[i] for i in ids] if ids else list(instances.values())
            if operation == 'DescribeInstances':
                for item in chosen:
                    if item['Backend'] == 'docker' and item['State']['Name'] != 'terminated':
                        if not self.images:
                            raise Error('RuntimeUnavailable', 'Restart with Docker EC2 image mappings to inspect these instances', 503)
                        name = self.container(item)
                        if not item.get('ContainerId'):
                            item['ContainerId'] = self.docker('inspect', '--format', '{{.Id}}', name)
                        running = self.docker('inspect', '--format', '{{.State.Running}}', name)
                        item['State'] = state('running' if running == 'true' else 'stopped')
                return {'Reservations': [{'Instances': [self.public(i) for i in chosen]}] if chosen else []}
            if any(i['Backend'] == 'docker' for i in chosen) and (not self.images or len(chosen) != 1):
                raise Error('InvalidParameterValue', 'Docker lifecycle requires Docker mode and exactly one instance')
            target = {'StartInstances': 'running', 'StopInstances': 'stopped', 'TerminateInstances': 'terminated'}[operation]
            if target != 'terminated' and any(i['State']['Name'] == 'terminated' for i in chosen):
                raise Error('IncorrectInstanceState', 'Terminated instances cannot be started or stopped')
            changes = []
            for item in chosen:
                previous = copy.deepcopy(item['State'])
                if item['Backend'] == 'docker' and previous['Name'] != target:
                    name = self.container(item)
                    self.docker(*({'running': ['start', name], 'stopped': ['stop', '-t', '5', name],
                                  'terminated': ['rm', '-f', name]}[target]))
                item['State'] = state(target)
                changes.append({'InstanceId': item['InstanceId'], 'PreviousState': previous, 'CurrentState': state(target)})
            return {{'StartInstances': 'StartingInstances', 'StopInstances': 'StoppingInstances', 'TerminateInstances': 'TerminatingInstances'}[operation]: changes}

    @staticmethod
    def public(item):
        result = {k: copy.deepcopy(v) for k, v in item.items() if k != 'Owner'}
        if item['Backend'] == 'docker':
            result['ContainerName'] = 'localcloud-' + item['InstanceId']
        result['Endpoints'] = [f"http://127.0.0.1:{p['HostPort']}" for p in item.get('PortMappings', [])]
        return result


def validate_instances(instances):
    if not isinstance(instances, dict):
        raise ValueError('Invalid EC2 instance collection')
    required = {'InstanceId', 'ImageId', 'InstanceType', 'State', 'LaunchTime', 'Backend', 'Owner'}
    names = []
    for key, item in instances.items():
        if (not isinstance(item, dict) or set(item) - required - {'Name'} or not required <= set(item) or key != item['InstanceId']
                or not re.fullmatch(r'i-[0-9a-f]{17}', key)
                or item['Backend'] != 'mock' or item['State'] not in [state(n) for n in CODES]
                or not all(isinstance(item[k], str) for k in required - {'State'})
                or not re.fullmatch(r'ami-[a-zA-Z0-9-]{1,80}', item['ImageId'])
                or item['InstanceType'] not in ('t3.micro', 't3.small', 't3.medium')
                or not re.fullmatch(r'[0-9a-f]{32}', item['Owner'])):
            raise ValueError('Invalid EC2 record; portable state supports mock instances only')
        if 'Name' in item:
            valid_name(item['Name'])
            names.append(item['Name'])
    if len(names) != len(set(names)):
        raise ValueError('Duplicate EC2 instance names')


def query_request(body):
    from urllib.parse import parse_qs
    fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=100)
    if any(len(v) != 1 for v in fields.values()):
        raise Error('InvalidParameterValue', 'Duplicate query parameters')
    fields = {k: v[0] for k, v in fields.items()}
    operation = fields.pop('Action', '')
    version = fields.pop('Version', '2016-11-15')
    if version != '2016-11-15':
        raise Error('UnsupportedOperation', 'Only EC2 API version 2016-11-15 is supported', 501)
    ids = []
    for key in list(fields):
        if re.fullmatch(r'InstanceId\.[1-9][0-9]*', key):
            ids.append((int(key.split('.')[1]), fields.pop(key)))
    if ids:
        fields['InstanceIds'] = [v for _, v in sorted(ids)]
    return operation, fields


def query_response(operation, result):
    from xml.etree.ElementTree import Element, SubElement, tostring
    root = Element(operation + 'Response', xmlns='http://ec2.amazonaws.com/doc/2016-11-15/')
    SubElement(root, 'requestId').text = str(uuid.uuid4())
    names = {'Instances': 'instancesSet', 'Reservations': 'reservationSet', 'State': 'instanceState',
             'StartingInstances': 'instancesSet', 'StoppingInstances': 'instancesSet', 'TerminatingInstances': 'instancesSet'}
    def write(parent, values):
        for key, value in values.items():
            if key in ('Backend', 'PortMappings', 'Endpoints', 'ContainerId', 'ContainerName') or (key == 'Name' and 'InstanceId' in values):
                continue
            node = SubElement(parent, names.get(key, key[0].lower() + key[1:]))
            if isinstance(value, dict):
                write(node, value)
            elif isinstance(value, list):
                for item in value:
                    write(SubElement(node, 'item'), item)
            else:
                node.text = str(value)
    write(root, result)
    return tostring(root, encoding='unicode')


def port_mappings(value):
    if not isinstance(value, list) or len(value) > 8:
        raise Error('InvalidParameterValue', 'PortMappings must be a list of at most eight TCP mappings')
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'HostPort', 'ContainerPort'}:
            raise Error('InvalidParameterValue', 'Each port mapping requires HostPort and ContainerPort')
        if any(type(item[k]) is not int or not 1024 <= item[k] <= 65535 for k in item):
            raise Error('InvalidParameterValue', 'Ports must be integers between 1024 and 65535')
        result.append(dict(item))
    if len({p['HostPort'] for p in result}) != len(result):
        raise Error('InvalidParameterValue', 'Duplicate host port')
    return result


def parse_publish(value):
    if not re.fullmatch(r'[0-9]+:[0-9]+', value):
        raise ValueError('--publish must be HOST_PORT:CONTAINER_PORT (localhost TCP only)')
    host, container = map(int, value.split(':'))
    return port_mappings([{'HostPort': host, 'ContainerPort': container}])[0]
