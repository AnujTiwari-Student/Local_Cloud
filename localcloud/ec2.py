"""Small EC2 lifecycle provider; optional bounded Docker execution."""
import copy
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from .engine import Error, integer

CODES = {'running': 16, 'stopped': 80, 'terminated': 48}
OPERATIONS = {'RunInstances', 'DescribeInstances', 'StartInstances', 'StopInstances', 'TerminateInstances'}


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
        allowed = {'ImageId', 'InstanceType', 'MinCount', 'MaxCount'} if operation == 'RunInstances' else {'InstanceIds'}
        if not isinstance(args, dict) or set(args) - allowed:
            raise Error('UnsupportedOperation', 'Unsupported EC2 parameters', 501)
        ids = args.get('InstanceIds', [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or not re.fullmatch(r'i-[0-9a-f]{17}', i) for i in ids):
            raise Error('InvalidInstanceID.Malformed', 'Expected instance IDs')
        if operation not in ('RunInstances', 'DescribeInstances') and not ids:
            raise Error('MissingParameter', 'InstanceIds is required')
        if len(ids) != len(set(ids)):
            raise Error('InvalidParameterValue', 'Duplicate instance IDs')
        resource = '*' if not ids else f'arn:aws:ec2:us-east-1:000000000000:instance/{ids[0]}'
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
            if operation == 'RunInstances':
                image = args.get('ImageId', '')
                if not isinstance(image, str) or not re.fullmatch(r'ami-[a-zA-Z0-9-]{1,80}', image):
                    raise Error('InvalidAMIID.Malformed', 'Use an ami- identifier')
                minimum = integer(args.get('MinCount', 1), 1, 10)
                count = integer(args.get('MaxCount', 1), minimum, 10)
                kind = args.get('InstanceType', 't3.micro')
                if kind not in ('t3.micro', 't3.small', 't3.medium'):
                    raise Error('InvalidParameterValue', 'Supported types: t3.micro, t3.small, t3.medium')
                if self.images and image not in self.images:
                    raise Error('InvalidAMIID.NotFound', 'Image is not in the configured allowlist')
                if self.images and count != 1:
                    raise Error('InvalidParameterValue', 'Docker mode supports one instance per launch')
                if sum(i['State']['Name'] != 'terminated' for i in instances.values()) + count > 100:
                    raise Error('InstanceLimitExceeded', 'Local limit is 100 active instances')
                created = []
                for _ in range(count):
                    identifier = 'i-' + uuid.uuid4().hex[:17]
                    item = {'InstanceId': identifier, 'ImageId': image, 'InstanceType': kind,
                            'State': state('running'), 'LaunchTime': datetime.now(timezone.utc).isoformat(),
                            'Backend': 'docker' if self.images else 'mock', 'Owner': uuid.uuid4().hex}
                    if self.images:
                        self.docker('run', '-d', '--name', 'localcloud-' + identifier,
                                    '--label', 'localcloud.owner=' + item['Owner'],
                                    '--memory', {'t3.micro': '128m', 't3.small': '256m', 't3.medium': '512m'}[kind],
                                    '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL',
                                    '--security-opt', 'no-new-privileges', '--read-only', '--user', '65534:65534',
                                    '--network', 'none', self.images[image])
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
        return {k: copy.deepcopy(v) for k, v in item.items() if k != 'Owner'}


def validate_instances(instances):
    if not isinstance(instances, dict):
        raise ValueError('Invalid EC2 instance collection')
    required = {'InstanceId', 'ImageId', 'InstanceType', 'State', 'LaunchTime', 'Backend', 'Owner'}
    for key, item in instances.items():
        if (not isinstance(item, dict) or set(item) != required or key != item['InstanceId']
                or not re.fullmatch(r'i-[0-9a-f]{17}', key)
                or item['Backend'] != 'mock' or item['State'] not in [state(n) for n in CODES]
                or not all(isinstance(item[k], str) for k in required - {'State'})
                or not re.fullmatch(r'ami-[a-zA-Z0-9-]{1,80}', item['ImageId'])
                or item['InstanceType'] not in ('t3.micro', 't3.small', 't3.medium')
                or not re.fullmatch(r'[0-9a-f]{32}', item['Owner'])):
            raise ValueError('Invalid EC2 record; portable state supports mock instances only')


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
            if key == 'Backend':
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
