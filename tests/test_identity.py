"""Bounded IAM/STS and instance-role credential lifecycle contracts."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from localcloud.engine import Engine, Error
from localcloud.identity import Identity

EC2_TRUST = {'Statement': {'Effect': 'Allow', 'Principal': {'Service': 'ec2.amazonaws.com'}, 'Action': 'sts:AssumeRole'}}
ROOT_TRUST = {'Statement': {'Effect': 'Allow', 'Principal': {'AWS': 'arn:aws:iam::000000000000:root'}, 'Action': 'sts:AssumeRole'}}
ALLOW = {'Statement': {'Effect': 'Allow', 'Action': 'sqs:SendMessage', 'Resource': '*'}}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'identity.db')
        self.open()

    def open(self):
        self.engine = Engine(self.path)
        self.identity = getattr(self.engine, 'identity', None) or Identity(self.engine)
        self.engine.identity = self.identity

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def iam(self, operation, **args):
        return self.identity.call('iam', operation, args)

    def role(self, role='app', trust=EC2_TRUST):
        return self.iam('CreateRole', RoleName=role, AssumeRolePolicyDocument=trust)['Role']

    def profile(self, role='app', profile='web'):
        self.role(role)
        self.iam('CreateInstanceProfile', InstanceProfileName=profile)
        self.iam('AddRoleToInstanceProfile', InstanceProfileName=profile, RoleName=role)
        return self.iam('GetInstanceProfile', InstanceProfileName=profile)['InstanceProfile']

    def instance(self):
        profile = self.profile()
        with self.engine.transaction() as state:
            state['instances'] = {'i-0123456789abcdef0': {'InstanceId': 'i-0123456789abcdef0', 'State': {'Name': 'running'}, 'IamInstanceProfile': {'Arn': profile['Arn'], 'Id': profile['InstanceProfileId']}}}
        return 'i-0123456789abcdef0'

    def error(self, code, callable):
        with self.assertRaises(Error) as error:
            callable()
        self.assertEqual(error.exception.code, code)

    def test_profile_membership_and_delete_conflicts(self):
        profile = self.profile()
        self.assertEqual(profile['Roles'][0]['RoleName'], 'app')
        self.error('DeleteConflict', lambda: self.iam('DeleteRole', RoleName='app'))
        self.error('DeleteConflict', lambda: self.iam('DeleteInstanceProfile', InstanceProfileName='web'))
        self.role('second')
        self.error('LimitExceeded', lambda: self.iam('AddRoleToInstanceProfile', InstanceProfileName='web', RoleName='second'))
        self.iam('RemoveRoleFromInstanceProfile', InstanceProfileName='web', RoleName='app')
        self.iam('DeleteInstanceProfile', InstanceProfileName='web')
        self.iam('DeleteRole', RoleName='app')
        self.error('NoSuchEntity', lambda: self.iam('GetRole', RoleName='app'))

    def test_inline_policy_dynamic_statements_and_explicit_subset(self):
        role = self.role()
        self.iam('PutRolePolicy', RoleName='app', PolicyName='queue', PolicyDocument=ALLOW)
        with self.engine.transaction() as state:
            statements = self.identity.statements(state, role['Arn'])
            self.assertEqual(statements[0]['Action'], ['sqs:SendMessage'])
            self.assertEqual(self.identity.statements(state, 'test'), [])
        self.error('DeleteConflict', lambda: self.iam('DeleteRole', RoleName='app'))
        unsupported = {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*', 'Condition': {'Bool': {}}}}
        self.error('UnsupportedOperation', lambda: self.iam('PutRolePolicy', RoleName='app', PolicyName='bad', PolicyDocument=unsupported))
        self.assertEqual(self.iam('ListRolePolicies', RoleName='app')['PolicyNames'], ['queue'])
        self.iam('DeleteRolePolicy', RoleName='app', PolicyName='queue')
        self.assertEqual(self.iam('ListRolePolicies', RoleName='app')['PolicyNames'], [])

    def test_assume_role_requires_trust_and_returns_session_identity(self):
        role = self.role(trust=ROOT_TRUST)
        result = self.identity.call('sts', 'AssumeRole', {'RoleArn': role['Arn'], 'RoleSessionName': 'cli-session'})
        credentials = result['Credentials']
        record = self.identity.lookup_credentials(credentials['AccessKeyId'], credentials['SessionToken'])
        self.assertEqual(record['principal'], role['Arn'])
        self.assertTrue(record['arn'].endswith('assumed-role/app/cli-session'))
        self.engine.request_context.identity = record
        caller = self.identity.call('sts', 'GetCallerIdentity', {}, role['Arn'])
        self.assertEqual(caller['Arn'], result['AssumedRoleUser']['Arn'])
        self.assertEqual(caller['UserId'], result['AssumedRoleUser']['AssumedRoleId'])
        self.error('InvalidClientTokenId', lambda: self.identity.lookup_credentials(credentials['AccessKeyId'], 'wrong'))
        self.iam('UpdateAssumeRolePolicy', RoleName='app', PolicyDocument=EC2_TRUST)
        self.error('InvalidClientTokenId', lambda: self.identity.lookup_credentials(credentials['AccessKeyId'], credentials['SessionToken']))
        self.error('AccessDenied', lambda: self.identity.call('sts', 'AssumeRole', {'RoleArn': role['Arn'], 'RoleSessionName': 'cli-session'}))

    def test_instance_credentials_rotate_and_are_not_in_portable_state(self):
        instance = self.instance()
        first = self.identity.instance_credentials(instance)
        second = self.identity.instance_credentials(instance)
        self.assertEqual(first, second)
        secret = first['Credentials']['SecretAccessKey']
        state = self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]
        self.assertNotIn(secret, state)
        self.assertNotIn(first['Credentials']['Token'], state)
        expiration = self.identity.lookup_credentials(first['Credentials']['AccessKeyId'], first['Credentials']['Token'])['expiration']
        with patch('localcloud.identity.time.time', return_value=expiration - 299):
            rotated = self.identity.instance_credentials(instance)
            self.assertNotEqual(rotated['Credentials']['AccessKeyId'], first['Credentials']['AccessKeyId'])
            self.assertEqual(self.identity.lookup_credentials(first['Credentials']['AccessKeyId'], first['Credentials']['Token'])['secret_access_key'], secret)
        with patch('localcloud.identity.time.time', return_value=expiration + 1):
            self.error('ExpiredToken', lambda: self.identity.lookup_credentials(first['Credentials']['AccessKeyId'], first['Credentials']['Token']))

    def test_broker_capability_persists_but_cannot_cross_instances(self):
        instance = self.instance()
        capability = self.identity.instance_broker(instance)
        self.assertEqual(self.identity.broker_credentials(instance, capability)['RoleName'], 'app')
        self.error('Unauthorized', lambda: self.identity.broker_credentials('i-other', capability))
        self.error('Unauthorized', lambda: self.identity.broker_credentials(instance, 'wrong'))
        self.engine.close()
        self.open()
        self.assertEqual(self.identity.instance_broker(instance), capability)
        self.assertEqual(self.identity.broker_credentials(instance, capability)['RoleName'], 'app')

    def test_disassociation_and_role_removal_revoke_sessions(self):
        instance = self.instance()
        capability = self.identity.instance_broker(instance)
        credentials = self.identity.instance_credentials(instance)['Credentials']
        with self.engine.transaction(): self.identity.revoke_instance(instance, broker=False)
        self.error('InvalidClientTokenId', lambda: self.identity.lookup_credentials(credentials['AccessKeyId'], credentials['Token']))
        self.assertEqual(self.identity.instance_broker(instance), capability)
        credentials = self.identity.instance_credentials(instance)['Credentials']
        self.iam('RemoveRoleFromInstanceProfile', InstanceProfileName='web', RoleName='app')
        self.error('InvalidClientTokenId', lambda: self.identity.lookup_credentials(credentials['AccessKeyId'], credentials['Token']))
        self.error('NotFound', lambda: self.identity.instance_credentials(instance))
        with self.engine.transaction(): self.identity.revoke_instance(instance)
        self.error('Unauthorized', lambda: self.identity.broker_credentials(instance, capability))

    def test_bootstrap_issue_does_not_commit_outer_transaction(self):
        profile = self.profile()
        instance = {'InstanceId': 'i-0123456789abcdef0', 'State': {'Name': 'pending'}, 'IamInstanceProfile': {'Arn': profile['Arn'], 'Id': profile['InstanceProfileId']}}
        with self.assertRaises(RuntimeError):
            with self.engine.transaction() as state:
                state['instances'] = {instance['InstanceId']: instance}
                self.identity.instance_broker(instance['InstanceId'])
                issued = self.identity.instance_credentials_from_state(state, instance)
                self.assertTrue(issued['Credentials']['AccessKeyId'].startswith('ASIA'))
                raise RuntimeError('rollback')
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM identity_sessions').fetchone()[0], 0)
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM identity_brokers').fetchone()[0], 0)
        self.assertNotIn('instances', json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]))

    def test_missing_profile_wrong_trust_and_terminated_instance(self):
        instance = self.instance()
        self.iam('UpdateAssumeRolePolicy', RoleName='app', PolicyDocument=ROOT_TRUST)
        self.error('AccessDenied', lambda: self.identity.instance_credentials(instance))
        self.iam('UpdateAssumeRolePolicy', RoleName='app', PolicyDocument=EC2_TRUST)
        with self.engine.transaction() as state: state['instances'][instance]['State']['Name'] = 'terminated'
        self.error('NotFound', lambda: self.identity.instance_credentials(instance))

    def test_paths_pagination_duplicates_and_bad_parameters(self):
        for value in ('one', 'two', 'three'):
            self.iam('CreateRole', RoleName=value, Path='/application/', AssumeRolePolicyDocument=json.dumps(EC2_TRUST))
        first = self.iam('ListRoles', PathPrefix='/application/', MaxItems=2)
        self.assertTrue(first['IsTruncated'])
        second = self.iam('ListRoles', PathPrefix='/application/', MaxItems=2, Marker=first['Marker'])
        self.assertEqual(len(second['Roles']), 1)
        self.assertFalse(second['IsTruncated'])
        self.error('EntityAlreadyExists', lambda: self.iam('CreateRole', RoleName='one', AssumeRolePolicyDocument=EC2_TRUST))
        self.error('UnsupportedOperation', lambda: self.iam('CreateRole', RoleName='new', AssumeRolePolicyDocument=EC2_TRUST, PermissionsBoundary='anything'))
        self.error('ValidationError', lambda: self.iam('CreateRole', RoleName='bad/name', AssumeRolePolicyDocument=EC2_TRUST))
        self.error('InvalidInput', lambda: self.iam('ListRoles', Marker='invalid'))

    def test_live_role_policy_changes_affect_existing_sessions(self):
        role = self.role(trust=ROOT_TRUST)
        assumed = self.identity.call('sts', 'AssumeRole', {'RoleArn': role['Arn'], 'RoleSessionName': 'policy-test'})
        self.iam('PutRolePolicy', RoleName='app', PolicyName='permissions', PolicyDocument={'Statement': {'Effect': 'Allow', 'Action': 'sqs:*', 'Resource': '*'}})
        self.engine.iam_mode = 'enforce'
        queue = self.engine.sqs('CreateQueue', {'QueueName': 'role-queue'}, role['Arn'])['QueueUrl']
        self.engine.sqs('SendMessage', {'QueueUrl': queue, 'MessageBody': 'allowed'}, role['Arn'])
        self.engine.iam_mode = 'off'
        self.iam('PutRolePolicy', RoleName='app', PolicyName='permissions', PolicyDocument={'Statement': [{'Effect': 'Allow', 'Action': 'sqs:*', 'Resource': '*'}, {'Effect': 'Deny', 'Action': 'sqs:SendMessage', 'Resource': '*'}]})
        self.engine.iam_mode = 'enforce'
        credentials = assumed['Credentials']
        self.assertEqual(self.identity.lookup_credentials(credentials['AccessKeyId'], credentials['SessionToken'])['principal'], role['Arn'])
        self.error('AccessDenied', lambda: self.engine.sqs('SendMessage', {'QueueUrl': queue, 'MessageBody': 'denied'}, role['Arn']))
        self.assertEqual(len(self.engine.overview()['audit']), 1)

    def test_portable_role_validation_rejects_bad_references(self):
        self.profile()
        state = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        self.identity.validate_state(state)
        state['identity']['profiles']['web']['Roles'] = ['missing']
        with self.assertRaises(ValueError): self.identity.validate_state(state)

    def test_trust_explicit_deny_and_account_root_boundary(self):
        trust = {'Statement': [ROOT_TRUST['Statement'], {'Effect': 'Deny', 'Principal': {'AWS': 'arn:aws:iam::000000000000:user/test'}, 'Action': 'sts:AssumeRole'}]}
        self.assertFalse(self.identity.trusted(trust, 'arn:aws:iam::000000000000:user/test'))
        self.assertFalse(self.identity.trusted(ROOT_TRUST, 'arn:aws:iam::123456789012:user/test'))
        self.assertTrue(self.identity.trusted(ROOT_TRUST, 'arn:aws:iam::000000000000:user/other'))
        self.assertFalse(self.identity.trusted(ROOT_TRUST, 'ec2.amazonaws.com', service=True))


if __name__ == '__main__': unittest.main()
