"""Bounded IAM roles, instance profiles and local temporary credentials.

Credential secrets and per-instance broker capabilities live in private SQLite
rows rather than portable JSON state. They are local emulator credentials only.
"""
import copy
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from .engine import Error, integer, wildcard

ACCOUNT = '000000000000'
IAM_OPERATIONS = frozenset(('CreateRole', 'GetRole', 'ListRoles', 'DeleteRole', 'UpdateAssumeRolePolicy',
    'PutRolePolicy', 'GetRolePolicy', 'ListRolePolicies', 'DeleteRolePolicy',
    'CreateInstanceProfile', 'GetInstanceProfile', 'ListInstanceProfiles', 'DeleteInstanceProfile',
    'AddRoleToInstanceProfile', 'RemoveRoleFromInstanceProfile', 'ListInstanceProfilesForRole'))
STS_OPERATIONS = frozenset(('AssumeRole', 'GetCallerIdentity'))


def timestamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def name(value, maximum=64):
    if not isinstance(value, str) or not re.fullmatch(r'[\w+=,.@-]{1,' + str(maximum) + '}', value, flags=re.ASCII):
        raise Error('ValidationError', 'Invalid IAM name')
    return value


def path(value):
    if not isinstance(value, str) or not re.fullmatch(r'/[\x21-\x7e]*/|/', value) or len(value) > 512:
        raise Error('ValidationError', 'IAM paths must begin and end with /')
    return value


def strings(value, field):
    result = [value] if isinstance(value, str) else value
    if not isinstance(result, list) or not result or any(not isinstance(v, str) or not v for v in result):
        raise Error('MalformedPolicyDocument', field + ' must contain strings')
    return result


def policy(document, trust=False):
    if isinstance(document, str):
        try: document = json.loads(document)
        except (ValueError, TypeError): raise Error('MalformedPolicyDocument', 'Policy document must be valid JSON') from None
    if not isinstance(document, dict) or set(document) - {'Version', 'Statement', 'Id'}:
        raise Error('MalformedPolicyDocument', 'Expected a policy object with Statement')
    statements = document.get('Statement', [])
    if isinstance(statements, dict): statements = [statements]
    if not isinstance(statements, list) or not statements:
        raise Error('MalformedPolicyDocument', 'At least one policy statement is required')
    normalized = []
    for statement in statements:
        if not isinstance(statement, dict): raise Error('MalformedPolicyDocument', 'Expected a statement object')
        allowed = {'Sid', 'Effect', 'Action', 'Principal'} if trust else {'Sid', 'Effect', 'Action', 'Resource'}
        if set(statement) - allowed:
            raise Error('UnsupportedOperation', 'Policy conditions, NotAction, NotResource and other extended policy fields are unsupported', 501)
        if statement.get('Effect') not in ('Allow', 'Deny'):
            raise Error('MalformedPolicyDocument', 'Effect must be Allow or Deny')
        normalized_statement = copy.deepcopy(statement)
        normalized_statement['Action'] = strings(statement.get('Action'), 'Action')
        if trust:
            principal = statement.get('Principal')
            if principal == '*': principal = {'AWS': '*'}
            if not isinstance(principal, dict) or not principal or set(principal) - {'Service', 'AWS'}:
                raise Error('UnsupportedOperation', 'Trust Principal supports AWS and Service only', 501)
            normalized_statement['Principal'] = {key: strings(value, 'Principal') for key, value in principal.items()}
        else:
            normalized_statement['Resource'] = strings(statement.get('Resource'), 'Resource')
        normalized.append(normalized_statement)
    return {'Version': document.get('Version', '2012-10-17'), 'Statement': normalized}


class Identity:
    def __init__(self, engine):
        self.engine = engine
        engine.db.execute('CREATE TABLE IF NOT EXISTS identity_sessions (accesskey TEXT PRIMARY KEY, secret TEXT NOT NULL, token TEXT NOT NULL, role TEXT NOT NULL, session TEXT NOT NULL, instance TEXT, expiration REAL NOT NULL, created REAL NOT NULL, userid TEXT NOT NULL)')
        engine.db.execute('CREATE INDEX IF NOT EXISTS identity_session_instance ON identity_sessions(instance)')
        engine.db.execute('CREATE TABLE IF NOT EXISTS identity_brokers (instanceid TEXT PRIMARY KEY, token TEXT NOT NULL)')
        engine.db.commit()

    @staticmethod
    def store(state):
        return state.setdefault('identity', {'roles': {}, 'profiles': {}})

    @staticmethod
    def trusted(document, principal, service=False):
        allowed, denied = False, False
        for statement in policy(document, trust=True)['Statement']:
            if not any(wildcard('sts:AssumeRole'.lower(), action.lower()) for action in statement['Action']): continue
            candidates = statement['Principal'].get('Service' if service else 'AWS', [])
            root = 'arn:aws:iam::' + ACCOUNT + ':root'
            matches = any(candidate == principal or (not service and (candidate == '*' or (candidate == root and str(principal).startswith('arn:aws:iam::' + ACCOUNT + ':')))) for candidate in candidates)
            if matches:
                allowed |= statement['Effect'] == 'Allow'
                denied |= statement['Effect'] == 'Deny'
        return allowed and not denied

    @staticmethod
    def validate_state(state):
        identity = state.get('identity')
        if identity is None:
            return
        try:
            if not isinstance(identity, dict) or set(identity) != {'roles', 'profiles'} or not all(isinstance(identity[k], dict) for k in ('roles', 'profiles')):
                raise ValueError('Invalid IAM state')
            for key, role in identity['roles'].items():
                if not isinstance(role, dict) or set(role) != {'RoleName', 'Path', 'RoleId', 'Arn', 'CreateDate', 'AssumeRolePolicyDocument', 'Description', 'MaxSessionDuration', 'Policies'}:
                    raise ValueError('Invalid IAM role')
                name(key)
                if role['RoleName'] != key or role['Arn'] != 'arn:aws:iam::' + ACCOUNT + ':role' + path(role['Path']) + key or not re.fullmatch(r'AROA[A-Z0-9]{17}', role['RoleId']):
                    raise ValueError('Invalid IAM role identity')
                policy(role['AssumeRolePolicyDocument'], trust=True)
                if not isinstance(role['Description'], str) or not isinstance(role['CreateDate'], str) or type(role['MaxSessionDuration']) is not int or not 3600 <= role['MaxSessionDuration'] <= 43200 or not isinstance(role['Policies'], dict):
                    raise ValueError('Invalid IAM role fields')
                for key, document in role['Policies'].items():
                    name(key, 128)
                    if policy(document) != document:
                        raise ValueError('IAM policy must be normalized')
            for key, profile in identity['profiles'].items():
                if not isinstance(profile, dict) or set(profile) != {'InstanceProfileName', 'Path', 'InstanceProfileId', 'Arn', 'CreateDate', 'Roles'}:
                    raise ValueError('Invalid IAM instance profile')
                name(key, 128)
                if profile['InstanceProfileName'] != key or profile['Arn'] != 'arn:aws:iam::' + ACCOUNT + ':instance-profile' + path(profile['Path']) + key or not re.fullmatch(r'AIPA[A-Z0-9]{17}', profile['InstanceProfileId']):
                    raise ValueError('Invalid instance profile identity')
                if not isinstance(profile['Roles'], list) or len(profile['Roles']) > 1 or any(member not in identity['roles'] for member in profile['Roles']) or not isinstance(profile['CreateDate'], str):
                    raise ValueError('Invalid instance profile roles')
        except (Error, KeyError, TypeError) as error:
            raise ValueError('Invalid IAM state: ' + str(error)) from error

    def role(self, state, role_name):
        value = self.store(state)['roles'].get(name(role_name))
        if value is None: raise Error('NoSuchEntity', 'Role does not exist', 404)
        return value

    def role_arn(self, state, arn):
        if not isinstance(arn, str): raise Error('ValidationError', 'RoleArn must be a string')
        for role in self.store(state)['roles'].values():
            if role['Arn'] == arn: return role
        raise Error('NoSuchEntity', 'Role does not exist', 404)

    @staticmethod
    def public_role(role):
        return copy.deepcopy({key: value for key, value in role.items() if key != 'Policies'})

    def profile(self, state, reference):
        if not isinstance(reference, dict) or set(reference) - {'Name', 'Arn'} or len(reference) != 1:
            raise Error('InvalidParameterValue', 'Specify exactly one instance profile Name or Arn')
        profiles = self.store(state)['profiles']
        if 'Name' in reference:
            profile = profiles.get(name(reference['Name'], 128))
        else:
            profile = next((p for p in profiles.values() if p['Arn'] == reference['Arn']), None)
        if profile is None: raise Error('NoSuchEntity', 'Instance profile does not exist', 404)
        result = copy.deepcopy(profile)
        result['Roles'] = [self.public_role(self.role(state, role_name)) for role_name in profile['Roles']]
        return result

    def statements(self, state, principal):
        """Role policies are evaluated dynamically so edits affect live sessions."""
        roles = state.get('identity', {}).get('roles', {})
        for role in roles.values():
            if principal == role['Arn']:
                return [copy.deepcopy(statement) for document in role.get('Policies', {}).values() for statement in document['Statement']]
        return []

    @staticmethod
    def _allowed(args, allowed, required=()):
        if not isinstance(args, dict) or set(args) - set(allowed):
            raise Error('UnsupportedOperation', 'Unsupported IAM/STS parameters', 501)
        missing = [key for key in required if key not in args]
        if missing: raise Error('ValidationError', 'Missing required parameter: ' + missing[0])

    def call(self, service, operation, args, principal='test'):
        operations = IAM_OPERATIONS if service == 'iam' else STS_OPERATIONS if service == 'sts' else ()
        if operation not in operations: raise Error('UnsupportedOperation', operation, 501)
        if service == 'sts': return self._sts(operation, args, principal)
        parameters = {
            'CreateRole': ({'RoleName', 'Path', 'AssumeRolePolicyDocument', 'Description', 'MaxSessionDuration'}, ('RoleName', 'AssumeRolePolicyDocument')),
            'UpdateAssumeRolePolicy': ({'RoleName', 'PolicyDocument'}, ('RoleName', 'PolicyDocument')),
            'GetRole': ({'RoleName'}, ('RoleName',)), 'DeleteRole': ({'RoleName'}, ('RoleName',)),
            'ListRoles': ({'PathPrefix', 'Marker', 'MaxItems'}, ()),
            'PutRolePolicy': ({'RoleName', 'PolicyName', 'PolicyDocument'}, ('RoleName', 'PolicyName', 'PolicyDocument')),
            'GetRolePolicy': ({'RoleName', 'PolicyName'}, ('RoleName', 'PolicyName')),
            'DeleteRolePolicy': ({'RoleName', 'PolicyName'}, ('RoleName', 'PolicyName')),
            'ListRolePolicies': ({'RoleName', 'Marker', 'MaxItems'}, ('RoleName',)),
            'CreateInstanceProfile': ({'InstanceProfileName', 'Path'}, ('InstanceProfileName',)),
            'GetInstanceProfile': ({'InstanceProfileName'}, ('InstanceProfileName',)),
            'DeleteInstanceProfile': ({'InstanceProfileName'}, ('InstanceProfileName',)),
            'ListInstanceProfiles': ({'PathPrefix', 'Marker', 'MaxItems'}, ()),
            'ListInstanceProfilesForRole': ({'RoleName', 'Marker', 'MaxItems'}, ('RoleName',)),
            'AddRoleToInstanceProfile': ({'InstanceProfileName', 'RoleName'}, ('InstanceProfileName', 'RoleName')),
            'RemoveRoleFromInstanceProfile': ({'InstanceProfileName', 'RoleName'}, ('InstanceProfileName', 'RoleName')),
        }
        self._allowed(args, *parameters[operation])
        with self.engine.transaction() as state:
            store = self.store(state)
            arn = '*'
            if 'RoleName' in args:
                role_name = name(args['RoleName'])
                existing = store['roles'].get(role_name)
                arn = existing['Arn'] if existing else 'arn:aws:iam::' + ACCOUNT + ':role' + path(args.get('Path', '/')) + role_name
            if 'InstanceProfileName' in args:
                profile_name = name(args['InstanceProfileName'], 128)
                existing = store['profiles'].get(profile_name)
                arn = existing['Arn'] if existing else 'arn:aws:iam::' + ACCOUNT + ':instance-profile' + path(args.get('Path', '/')) + profile_name
            self.engine.authorize(state, principal, 'iam:' + operation, arn)
            if operation == 'CreateRole':
                if role_name in store['roles']: raise Error('EntityAlreadyExists', 'Role already exists', 409)
                role_path = path(args.get('Path', '/'))
                description = args.get('Description', '')
                if not isinstance(description, str) or len(description) > 1000: raise Error('ValidationError', 'Invalid role description')
                role = {'RoleName': role_name, 'Path': role_path, 'RoleId': 'AROA' + secrets.token_hex(9)[:17].upper(),
                    'Arn': 'arn:aws:iam::' + ACCOUNT + ':role' + role_path + role_name, 'CreateDate': timestamp(),
                    'AssumeRolePolicyDocument': policy(args['AssumeRolePolicyDocument'], trust=True),
                    'Description': description, 'MaxSessionDuration': integer(args.get('MaxSessionDuration', 3600), 3600, 43200), 'Policies': {}}
                store['roles'][role_name] = role
                return {'Role': self.public_role(role)}
            if operation in ('ListRoles', 'ListInstanceProfiles', 'ListInstanceProfilesForRole'):
                prefix = args.get('PathPrefix', '/')
                if not isinstance(prefix, str) or not prefix.startswith('/'): raise Error('ValidationError', 'Invalid PathPrefix')
                if operation == 'ListRoles': values = [self.public_role(r) for r in store['roles'].values() if r['Path'].startswith(prefix)]; field = 'Roles'
                else:
                    if operation == 'ListInstanceProfilesForRole': self.role(state, args['RoleName'])
                    values = [self.profile(state, {'Name': p['InstanceProfileName']}) for p in store['profiles'].values() if p['Path'].startswith(prefix) and (operation != 'ListInstanceProfilesForRole' or args['RoleName'] in p['Roles'])]; field = 'InstanceProfiles'
                return self._page(values, field, args)
            if operation == 'CreateInstanceProfile':
                if profile_name in store['profiles']: raise Error('EntityAlreadyExists', 'Instance profile already exists', 409)
                profile_path = path(args.get('Path', '/'))
                profile = {'InstanceProfileName': profile_name, 'Path': profile_path,
                    'InstanceProfileId': 'AIPA' + secrets.token_hex(9)[:17].upper(), 'Arn': 'arn:aws:iam::' + ACCOUNT + ':instance-profile' + profile_path + profile_name,
                    'CreateDate': timestamp(), 'Roles': []}
                store['profiles'][profile_name] = profile
                return {'InstanceProfile': self.profile(state, {'Name': profile_name})}
            if operation in ('GetInstanceProfile', 'DeleteInstanceProfile', 'AddRoleToInstanceProfile', 'RemoveRoleFromInstanceProfile'):
                public = self.profile(state, {'Name': args['InstanceProfileName']})
                profile = store['profiles'][args['InstanceProfileName']]
                if operation == 'GetInstanceProfile': return {'InstanceProfile': public}
                if operation == 'DeleteInstanceProfile':
                    if profile['Roles']: raise Error('DeleteConflict', 'Remove roles before deleting the instance profile', 409)
                    del store['profiles'][args['InstanceProfileName']]
                else:
                    role = self.role(state, args['RoleName'])
                    if operation == 'AddRoleToInstanceProfile':
                        if role['RoleName'] in profile['Roles']: raise Error('LimitExceeded', 'Role is already in the instance profile', 409)
                        if profile['Roles']: raise Error('LimitExceeded', 'An instance profile supports one role', 409)
                        profile['Roles'].append(role['RoleName'])
                    elif role['RoleName'] not in profile['Roles']: raise Error('NoSuchEntity', 'Role is not in the instance profile', 404)
                    else:
                        profile['Roles'].remove(role['RoleName'])
                        self._revoke_profile_instances(state, profile['Arn'])
                return {}
            role = self.role(state, args['RoleName'])
            if operation == 'GetRole': return {'Role': self.public_role(role)}
            if operation == 'UpdateAssumeRolePolicy':
                role['AssumeRolePolicyDocument'] = policy(args['PolicyDocument'], trust=True)
                self.engine.db.execute('DELETE FROM identity_sessions WHERE role=?', (role['Arn'],))
                return {}
            if operation == 'DeleteRole':
                if role['Policies'] or any(role['RoleName'] in p['Roles'] for p in store['profiles'].values()): raise Error('DeleteConflict', 'Remove inline policies and instance profile membership first', 409)
                del store['roles'][role['RoleName']]
                self.engine.db.execute('DELETE FROM identity_sessions WHERE role=?', (role['Arn'],))
                return {}
            policies = role['Policies']
            if operation == 'ListRolePolicies': return self._page(sorted(policies), 'PolicyNames', args)
            policy_name = name(args['PolicyName'], 128)
            if operation == 'PutRolePolicy': policies[policy_name] = policy(args['PolicyDocument']); return {}
            if policy_name not in policies: raise Error('NoSuchEntity', 'Inline policy does not exist', 404)
            if operation == 'GetRolePolicy': return {'RoleName': role['RoleName'], 'PolicyName': policy_name, 'PolicyDocument': copy.deepcopy(policies[policy_name])}
            del policies[policy_name]
            return {}

    @staticmethod
    def _page(values, field, args):
        limit = integer(args.get('MaxItems', 100), 1, 1000)
        marker = args.get('Marker', '0')
        if not isinstance(marker, str) or not marker.isascii() or not marker.isdigit(): raise Error('InvalidInput', 'Invalid pagination marker')
        offset = int(marker)
        if offset > len(values): raise Error('InvalidInput', 'Invalid pagination marker')
        end = offset + limit
        return {field: values[offset:end], 'IsTruncated': end < len(values), **({'Marker': str(end)} if end < len(values) else {})}

    def _sts(self, operation, args, principal):
        if operation == 'GetCallerIdentity':
            self._allowed(args, ())
            identity = getattr(self.engine.request_context, 'identity', None)
            if identity:
                return {'Account': ACCOUNT, 'Arn': identity['arn'], 'UserId': identity['userid']}
            arn = principal if isinstance(principal, str) and principal.startswith('arn:aws:') else 'arn:aws:iam::' + ACCOUNT + ':user/' + str(principal)
            return {'Account': ACCOUNT, 'Arn': arn, 'UserId': str(principal)}
        self._allowed(args, ('RoleArn', 'RoleSessionName', 'DurationSeconds'), ('RoleArn', 'RoleSessionName'))
        session = name(args['RoleSessionName'])
        if len(session) < 2: raise Error('ValidationError', 'RoleSessionName must have at least two characters')
        with self.engine.transaction() as state:
            role = self.role_arn(state, args['RoleArn'])
            self.engine.authorize(state, principal, 'sts:AssumeRole', role['Arn'])
            caller = principal if str(principal).startswith('arn:') else 'arn:aws:iam::' + ACCOUNT + ':user/' + str(principal)
            if not self.trusted(role['AssumeRolePolicyDocument'], caller): raise Error('AccessDenied', 'Role trust policy does not allow this caller', 403)
            duration = integer(args.get('DurationSeconds', 3600), 900, role['MaxSessionDuration'])
            credentials, record = self._issue(role, session, duration=duration)
            return {'Credentials': credentials, 'AssumedRoleUser': {'Arn': record['arn'], 'AssumedRoleId': record['userid']}, 'PackedPolicySize': 0}

    def _issue(self, role, session, instance=None, duration=3600):
        now = time.time()
        accesskey = 'ASIA' + secrets.token_hex(8).upper()
        secret, token = secrets.token_urlsafe(30), secrets.token_urlsafe(48)
        expiration = now + duration
        userid = role['RoleId'] + ':' + session
        self.engine.db.execute('DELETE FROM identity_sessions WHERE expiration<=?', (now,))
        self.engine.db.execute('INSERT INTO identity_sessions VALUES (?,?,?,?,?,?,?,?,?)', (accesskey, secret, token, role['Arn'], session, instance, expiration, now, userid))
        credentials = {'AccessKeyId': accesskey, 'SecretAccessKey': secret, 'SessionToken': token, 'Expiration': timestamp(expiration)}
        record = self._record((accesskey, secret, token, role['Arn'], session, instance, expiration, now, userid))
        return credentials, record

    @staticmethod
    def _record(row):
        accesskey, secret, token, role, session, instance, expiration, created, userid = row
        arn = 'arn:aws:sts::' + ACCOUNT + ':assumed-role/' + role.rsplit('/', 1)[-1] + '/' + session
        return {'principal': role, 'role_arn': role, 'access_key_id': accesskey, 'secret_access_key': secret,
            'session_token': token, 'expiration': expiration, 'arn': arn, 'userid': userid, 'instance_id': instance, 'created': created}

    def lookup_credentials(self, accesskey, token):
        with self.engine.lock:
            row = self.engine.db.execute('SELECT * FROM identity_sessions WHERE accesskey=?', (accesskey,)).fetchone()
            if row is None or not isinstance(token, str) or not hmac.compare_digest(row[2], token): raise Error('InvalidClientTokenId', 'The security token is invalid', 403)
            if row[6] <= time.time(): raise Error('ExpiredToken', 'The security token has expired', 403)
            state = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            if not any(role['Arn'] == row[3] and row[8] == role['RoleId'] + ':' + row[4] for role in state.get('identity', {}).get('roles', {}).values()): raise Error('InvalidClientTokenId', 'The role no longer exists or its identity changed', 403)
            return self._record(row)

    def instance_broker(self, instanceid):
        with self.engine.lock:
            nested = self.engine.db.in_transaction or getattr(self.engine.request_context, 'state_transaction_depth', 0) > 0
            row = self.engine.db.execute('SELECT token FROM identity_brokers WHERE instanceid=?', (instanceid,)).fetchone()
            if row: return row[0]
            token = secrets.token_urlsafe(48)
            self.engine.db.execute('INSERT INTO identity_brokers VALUES (?,?)', (instanceid, token))
            if not nested: self.engine.db.commit()
            return token

    def broker_credentials(self, instanceid, token):
        with self.engine.lock:
            row = self.engine.db.execute('SELECT token FROM identity_brokers WHERE instanceid=?', (instanceid,)).fetchone()
            if row is None or not isinstance(token, str) or not hmac.compare_digest(row[0], token): raise Error('Unauthorized', 'Invalid instance metadata capability', 401)
            return self.instance_credentials(instanceid)

    def instance_credentials(self, instanceid):
        with self.engine.transaction() as state:
            instance = state.get('instances', {}).get(instanceid)
            return self.instance_credentials_from_state(state, instance)

    def instance_credentials_from_state(self, state, instance):
        """Issue bootstrap credentials inside an existing state transaction."""
        instanceid = instance['InstanceId'] if instance is not None else None
        if instance is None or instance['State']['Name'] in ('terminated', 'shutting-down'): raise Error('NotFound', 'Instance is not available', 404)
        reference = instance.get('IamInstanceProfile')
        if not reference: raise Error('NotFound', 'No IAM instance profile is associated', 404)
        profile = self.profile(state, {'Arn': reference['Arn']})
        if profile['InstanceProfileId'] != reference.get('Id', profile['InstanceProfileId']): raise Error('NotFound', 'Instance profile identity changed', 404)
        if not profile['Roles']: raise Error('NotFound', 'Instance profile has no role', 404)
        role = self.role(state, profile['Roles'][0]['RoleName'])
        if not self.trusted(role['AssumeRolePolicyDocument'], 'ec2.amazonaws.com', service=True): raise Error('AccessDenied', 'Role trust policy does not allow EC2', 403)
        now = time.time()
        row = self.engine.db.execute('SELECT * FROM identity_sessions WHERE instance=? AND role=? AND expiration>? ORDER BY created DESC LIMIT 1', (instanceid, role['Arn'], now + 300)).fetchone()
        if row is None:
            _, record = self._issue(role, instanceid, instance=instanceid)
        else: record = self._record(row)
        return {'RoleName': role['RoleName'], 'Info': {'Code': 'Success', 'LastUpdated': timestamp(record['created']), 'InstanceProfileArn': profile['Arn'], 'InstanceProfileId': profile['InstanceProfileId']},
            'Credentials': {'Code': 'Success', 'LastUpdated': timestamp(record['created']), 'Type': 'AWS-HMAC', 'AccessKeyId': record['access_key_id'],
                'SecretAccessKey': record['secret_access_key'], 'Token': record['session_token'], 'Expiration': timestamp(record['expiration'])}}

    def _revoke_profile_instances(self, state, profile_arn):
        for instanceid, instance in state.get('instances', {}).items():
            if instance.get('IamInstanceProfile', {}).get('Arn') == profile_arn:
                self.engine.db.execute('DELETE FROM identity_sessions WHERE instance=?', (instanceid,))

    def revoke_instance(self, instanceid, broker=True):
        """Call inside the caller's transaction when changing an association."""
        with self.engine.lock:
            self.engine.db.execute('DELETE FROM identity_sessions WHERE instance=?', (instanceid,))
            if broker:
                self.engine.db.execute('DELETE FROM identity_brokers WHERE instanceid=?', (instanceid,))

    def revoke_role(self, rolearn):
        with self.engine.lock:
            self.engine.db.execute('DELETE FROM identity_sessions WHERE role=?', (rolearn,))
