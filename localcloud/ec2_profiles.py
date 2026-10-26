"""Bounded EC2 instance-profile control plane and private metadata transport."""
import copy
import ipaddress
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .engine import Error, wildcard
from .ec2_infrastructure import paginated

OPERATIONS = {'AssociateIamInstanceProfile', 'ReplaceIamInstanceProfileAssociation',
              'DisassociateIamInstanceProfile', 'DescribeIamInstanceProfileAssociations'}
PARAMETERS = {
    'AssociateIamInstanceProfile': {'IamInstanceProfile', 'InstanceId', 'InstanceName'},
    'ReplaceIamInstanceProfileAssociation': {'IamInstanceProfile', 'AssociationId'},
    'DisassociateIamInstanceProfile': {'AssociationId'},
    'DescribeIamInstanceProfileAssociations': {'AssociationIds', 'Filters', 'MaxResults', 'NextToken'},
}


def transport_origin(engine):
    """A loopback backend cannot be reached from an isolated workload."""
    try:
        parsed = urlsplit(engine.endpoint)
        parsed.port  # Reject malformed ports/unbracketed IPv6 origins.
    except ValueError:
        raise Error('InvalidParameterValue', 'Invalid credential broker public endpoint') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise Error('InvalidParameterValue', 'Configure a guest-reachable HTTP(S) --public-endpoint for instance-role credentials')
    host = parsed.hostname.lower()
    try:
        invalid = ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_unspecified
    except ValueError:
        invalid = host == 'localhost' or host.endswith('.localhost')
    if invalid:
        raise Error('InvalidParameterValue', 'Instance-role credentials require a guest-reachable --public-endpoint; loopback and wildcard addresses are not reachable from workloads')
    return engine.endpoint.rstrip('/')


def broker_environment(ec2, item, seed=None):
    if not getattr(ec2.engine, 'identity', None):
        return {}
    try:
        origin = transport_origin(ec2.engine)
    except Error:
        if item.get('IamInstanceProfile'):
            raise
        return {}
    values = {'LOCALCLOUD_IMDS_BROKER_URL': origin + '/_local/instance-credentials/' + item['InstanceId'],
              'LOCALCLOUD_IMDS_BROKER_TOKEN': ec2.engine.identity.instance_broker(item['InstanceId'])}
    if seed is not None:
        import json
        values['LOCALCLOUD_IMDS_INITIAL_CREDENTIALS'] = json.dumps(seed)
    return values


class Profiles:
    def __init__(self, ec2):
        self.ec2 = ec2
        self.engine = ec2.engine

    def require_runtime(self, item=None):
        if self.ec2.backend == 'mock':
            return
        if self.ec2.backend == 'kubernetes':
            enabled = self.ec2.kubernetes.helper_image
        else:
            enabled = self.ec2.runtime.imds and self.ec2.runtime.helper_image
        if not enabled:
            raise Error('UnsupportedOperation', 'Instance-role credentials require a configured EC2 metadata helper', 501)
        origin = transport_origin(self.engine)
        if item is not None and self.ec2.backend == 'docker':
            name = self.ec2.container(item)
            if self.ec2.docker('inspect', '--format', '{{.HostConfig.NetworkMode}}', name) == 'none':
                raise Error('UnsupportedOperation', 'This guest has networking disabled; launch a new instance with metadata and a guest-reachable public endpoint configured', 501)
            import json
            values = json.loads(self.ec2.docker('inspect', '--format', '{{json .Config.Env}}', self.ec2.runtime.imds_name(item)))
            expected = 'LOCALCLOUD_IMDS_BROKER_URL=' + origin + '/_local/instance-credentials/' + item['InstanceId']
            if expected not in values:
                raise Error('UnsupportedOperation', 'This metadata helper was created without the current credential broker; launch a new instance with the public endpoint configured', 501)

    def profile(self, data, spec, principal):
        try:
            profile = self.engine.identity.profile(data, spec)
        except Error as error:
            if error.code == 'NoSuchEntity':
                raise Error('InvalidParameterValue', 'Invalid IAM instance profile') from None
            raise
        for role in profile.get('Roles', []):
            self.engine.authorize(data, principal, 'iam:PassRole', role['Arn'])
        return {'Arn': profile['Arn'], 'Id': profile['InstanceProfileId']}

    def attach(self, data, item, profile):
        associations = data.setdefault('ec2_iam_associations', {})
        association = {'AssociationId': 'iip-assoc-' + uuid.uuid4().hex[:17],
                       'InstanceId': item['InstanceId'], 'IamInstanceProfile': copy.deepcopy(profile),
                       'State': 'associated', 'Timestamp': datetime.now(timezone.utc).isoformat()}
        associations[association['AssociationId']] = association
        item['IamInstanceProfile'] = copy.deepcopy(profile)
        return association

    @staticmethod
    def active(data, instance_id):
        return [a for a in data.get('ec2_iam_associations', {}).values()
                if a['InstanceId'] == instance_id and a['State'] == 'associated']

    def call(self, operation, args, principal):
        if not isinstance(args, dict) or set(args) - PARAMETERS[operation]:
            raise Error('UnsupportedOperation', 'Unsupported instance-profile parameters', 501)
        from .ec2_infrastructure import validate_input
        validate_input(operation, args)
        return self.engine.tools.call('ec2', operation, principal, '*', lambda: self.perform(operation, args, principal))

    def perform(self, operation, args, principal):
        with self.engine.transaction() as data:
            self.engine.authorize(data, principal, 'ec2:' + operation, '*')
            associations = data.setdefault('ec2_iam_associations', {})
            if operation == 'DescribeIamInstanceProfileAssociations':
                ids = args.get('AssociationIds', [])
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    raise Error('InvalidParameterValue', 'AssociationIds must be strings')
                values = [copy.deepcopy(associations[i]) for i in ids if i in associations] if ids else copy.deepcopy(list(associations.values()))
                values = [a for a in values if a['State'] == 'associated']
                filters = args.get('Filters', [])
                if not isinstance(filters, list):
                    raise Error('InvalidParameterValue', 'Filters must be a list')
                for entry in filters:
                    names = {'instance-id': 'InstanceId', 'state': 'State', 'association-id': 'AssociationId'}
                    if not isinstance(entry, dict) or set(entry) != {'Name', 'Values'} or entry.get('Name') not in names or not isinstance(entry['Values'], list) or not entry['Values'] or any(not isinstance(v, str) for v in entry['Values']):
                        raise Error('InvalidParameterValue', 'Unsupported association filter')
                    values = [a for a in values if any(wildcard(a[names[entry['Name']]], value) for value in entry['Values'])]
                return paginated({'IamInstanceProfileAssociations': values}, 'IamInstanceProfileAssociations', args)
            if operation == 'AssociateIamInstanceProfile':
                key = args.get('InstanceId')
                if args.get('InstanceName'):
                    if key:
                        raise Error('InvalidParameterValue', 'Choose one instance selector')
                    matches = [i for i in data.get('instances', {}).values() if i.get('Name') == args['InstanceName'] or any(t.get('Key') == 'Name' and t.get('Value') == args['InstanceName'] for t in i.get('Tags', []))]
                    if len(matches) > 1:
                        raise Error('AmbiguousInstance', 'Multiple instances match that name')
                    key = matches[0]['InstanceId'] if matches else None
                item = data.get('instances', {}).get(key)
                if item is None:
                    raise Error('InvalidInstanceID.NotFound', 'Instance does not exist')
                if item['State']['Name'] not in ('running', 'stopped', 'pending'):
                    raise Error('IncorrectInstanceState', 'Instance cannot accept a profile in its current state')
                if self.active(data, key):
                    raise Error('IncorrectState', 'An IAM instance profile is already associated')
                self.require_runtime(item)
                profile = self.profile(data, args.get('IamInstanceProfile'), principal)
                result = copy.deepcopy(self.attach(data, item, profile))
                if item['Backend'] == 'kubernetes':
                    self.ec2.kube_runtime(item).reconcile(item)
                result['State'] = 'associating'
                return {'IamInstanceProfileAssociation': result}
            key = args.get('AssociationId')
            association = associations.get(key)
            if association is None:
                raise Error('InvalidAssociationID.NotFound', 'Association ID does not exist')
            item = data.get('instances', {}).get(association['InstanceId'])
            if association['State'] != 'associated' or item is None or item['State']['Name'] in ('terminated', 'shutting-down'):
                raise Error('IncorrectState', 'The association is not active')
            if operation == 'ReplaceIamInstanceProfileAssociation':
                self.require_runtime(item)
                profile = self.profile(data, args.get('IamInstanceProfile'), principal)
                association['IamInstanceProfile'] = copy.deepcopy(profile)
                item['IamInstanceProfile'] = copy.deepcopy(profile)
                self.engine.identity.revoke_instance(item['InstanceId'], broker=False)
                result = copy.deepcopy(association)
                result['State'] = 'associating'
                return {'IamInstanceProfileAssociation': result}
            association['State'] = 'disassociated'
            item.pop('IamInstanceProfile', None)
            self.engine.identity.revoke_instance(item['InstanceId'], broker=False)
            result = copy.deepcopy(association)
            result['State'] = 'disassociating'
            return {'IamInstanceProfileAssociation': result}

    def terminate(self, data, item):
        for association in self.active(data, item['InstanceId']):
            association['State'] = 'disassociated'
        item.pop('IamInstanceProfile', None)
        self.engine.identity.revoke_instance(item['InstanceId'])
