"""Instance-profile lifecycle and dynamic IMDS credential boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from localcloud.engine import Engine, Error
from localcloud.ec2 import query_request, query_response
from localcloud.ec2_metadata import Metadata, broker_provider
from localcloud.ec2_profiles import broker_environment, transport_origin

TRUST = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'ec2.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'state.db'))
        for name in ('worker', 'other'):
            self.engine.identity.call('iam', 'CreateRole', {'RoleName': name, 'AssumeRolePolicyDocument': TRUST})
            self.engine.identity.call('iam', 'CreateInstanceProfile', {'InstanceProfileName': name})
            self.engine.identity.call('iam', 'AddRoleToInstanceProfile', {'InstanceProfileName': name, 'RoleName': name})

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def ec2(self, operation, **args):
        return self.engine.ec2.call(operation, args, 'test')

    def launch(self, **args):
        return self.ec2('RunInstances', ImageId='ami-local', **args)['Instances'][0]

    def test_launch_profile_identity_and_client_token_conflict(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'}, ClientToken='replay')
        self.assertTrue(item['IamInstanceProfile']['Id'].startswith('AIPA'))
        self.assertEqual(self.launch(IamInstanceProfile={'Name': 'worker'}, ClientToken='replay')['InstanceId'], item['InstanceId'])
        with self.assertRaises(Error) as error:
            self.launch(IamInstanceProfile={'Name': 'other'}, ClientToken='replay')
        self.assertEqual(error.exception.code, 'IdempotentParameterMismatch')
        self.assertEqual(self.engine.identity.instance_credentials(item['InstanceId'])['RoleName'], 'worker')

    def test_replace_and_detach_change_existing_metadata_provider(self):
        item = self.launch()
        instance = item['InstanceId']
        association = self.ec2('AssociateIamInstanceProfile', InstanceId=instance, IamInstanceProfile={'Name': 'worker'})['IamInstanceProfileAssociation']
        self.assertEqual(association['State'], 'associating')
        metadata = Metadata(item, lambda: self.engine.identity.instance_credentials(instance))
        path = '/latest/meta-data/iam/security-credentials/'
        self.assertEqual(metadata.request('GET', path, {}), (200, 'worker'))
        first = json.loads(metadata.request('GET', path + 'worker', {})[1])
        broker = self.engine.identity.instance_broker(instance)
        replacement = self.ec2('ReplaceIamInstanceProfileAssociation', AssociationId=association['AssociationId'], IamInstanceProfile={'Name': 'other'})['IamInstanceProfileAssociation']
        self.assertEqual(replacement['AssociationId'], association['AssociationId'])
        self.assertEqual(metadata.request('GET', path, {}), (200, 'other'))
        self.assertEqual(metadata.request('GET', path + 'worker', {})[0], 404)
        with self.assertRaises(Error):
            self.engine.identity.lookup_credentials(first['AccessKeyId'], first['Token'])
        self.assertEqual(self.engine.identity.instance_broker(instance), broker)
        self.ec2('DisassociateIamInstanceProfile', AssociationId=association['AssociationId'])
        self.assertEqual(metadata.request('GET', path, {})[0], 404)
        self.assertEqual(self.ec2('DescribeIamInstanceProfileAssociations', AssociationIds=[association['AssociationId']])['IamInstanceProfileAssociations'], [])

    def test_pass_role_is_required_even_with_ec2_launch_permission(self):
        self.engine.policy('developer', {'Statement': [{'Effect': 'Allow', 'Action': 'ec2:RunInstances', 'Resource': '*'}]})
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error) as error:
            self.engine.ec2.call('RunInstances', {'ImageId': 'ami-local', 'IamInstanceProfile': {'Name': 'worker'}}, 'developer')
        self.assertEqual(error.exception.code, 'AccessDenied')
        self.assertIn('iam:PassRole', error.exception.message)

    def test_duplicate_invalid_profile_and_association_errors(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'})
        with self.assertRaises(Error) as error:
            self.ec2('AssociateIamInstanceProfile', InstanceId=item['InstanceId'], IamInstanceProfile={'Name': 'other'})
        self.assertEqual(error.exception.code, 'IncorrectState')
        with self.assertRaises(Error) as error:
            self.launch(IamInstanceProfile={'Name': 'missing'})
        self.assertEqual(error.exception.code, 'InvalidParameterValue')
        with self.assertRaises(Error) as error:
            self.ec2('DisassociateIamInstanceProfile', AssociationId='iip-assoc-' + '0' * 17)
        self.assertEqual(error.exception.code, 'InvalidAssociationID.NotFound')

    def test_termination_revokes_credentials_and_instance_capability(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'})
        instance = item['InstanceId']
        capability = self.engine.identity.instance_broker(instance)
        old = self.engine.identity.broker_credentials(instance, capability)['Credentials']
        self.ec2('TerminateInstances', InstanceIds=[instance])
        with self.assertRaises(Error):
            self.engine.identity.broker_credentials(instance, capability)
        with self.assertRaises(Error):
            self.engine.identity.lookup_credentials(old['AccessKeyId'], old['Token'])

    def test_template_profile_reaches_launch_and_query_shapes_work(self):
        template = self.ec2('CreateLaunchTemplate', LaunchTemplateName='profile', LaunchTemplateData={'ImageId': 'ami-local', 'IamInstanceProfile': {'Name': 'worker'}})['LaunchTemplate']
        item = self.ec2('RunInstances', LaunchTemplate={'LaunchTemplateId': template['LaunchTemplateId']})['Instances'][0]
        self.assertIn('IamInstanceProfile', item)
        operation, args = query_request(('Action=AssociateIamInstanceProfile&InstanceId=' + item['InstanceId'] + '&IamInstanceProfile.Name=worker').encode())
        self.assertEqual(args, {'InstanceId': item['InstanceId'], 'IamInstanceProfile': {'Name': 'worker'}})
        self.assertIn('iamInstanceProfileAssociationSet', query_response('DescribeIamInstanceProfileAssociations', self.ec2('DescribeIamInstanceProfileAssociations')))

    def test_imds_token_and_endpoint_checks_run_before_credential_issuance(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'}, MetadataOptions={'HttpTokens': 'required'})
        calls = []
        def credentials():
            calls.append(True)
            return self.engine.identity.instance_credentials(item['InstanceId'])
        metadata = Metadata(item, credentials)
        path = '/latest/meta-data/iam/security-credentials/'
        self.assertEqual(metadata.request('GET', path, {})[0], 401)
        self.assertEqual(calls, [])
        _, token = metadata.request('PUT', '/latest/api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '60'})
        self.assertEqual(metadata.request('GET', path, {'X-aws-ec2-metadata-token': token}), (200, 'worker'))
        self.assertEqual(json.loads(metadata.request('GET', '/latest/meta-data/iam/info', {'X-aws-ec2-metadata-token': token})[1])['Code'], 'Success')
        item['MetadataOptions']['HttpEndpoint'] = 'disabled'
        count = len(calls)
        self.assertEqual(metadata.request('GET', path, {'X-aws-ec2-metadata-token': token})[0], 404)
        self.assertEqual(len(calls), count)

    def test_broker_origin_and_no_management_secret_in_payload(self):
        for endpoint in ('http://127.0.0.1:4566', 'http://localhost:4566', 'http://0.0.0.0:4566'):
            self.engine.endpoint = endpoint
            with self.assertRaises(Error): transport_origin(self.engine)
        self.engine.endpoint = 'http://localcloud.localcloud.svc:4566'
        item = self.launch(IamInstanceProfile={'Name': 'worker'})
        env = broker_environment(self.engine.ec2, item)
        self.assertEqual(set(env), {'LOCALCLOUD_IMDS_BROKER_URL', 'LOCALCLOUD_IMDS_BROKER_TOKEN'})
        self.assertNotIn(env['LOCALCLOUD_IMDS_BROKER_TOKEN'], json.dumps(self.engine.overview()))
        self.assertNotIn('LOCALCLOUD_MANAGEMENT_TOKEN', json.dumps(env))

    def test_broker_never_falls_back_after_explicit_profile_detach(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'})
        initial = self.engine.identity.instance_credentials(item['InstanceId'])
        environment = {'LOCALCLOUD_IMDS_BROKER_URL': 'http://backend.test/credentials', 'LOCALCLOUD_IMDS_BROKER_TOKEN': 'private-capability', 'LOCALCLOUD_IMDS_INITIAL_CREDENTIALS': json.dumps(initial)}
        provider = broker_provider(environment)
        detached = HTTPError(environment['LOCALCLOUD_IMDS_BROKER_URL'], 404, 'Detached', {}, None)
        with patch('urllib.request.OpenerDirector.open', side_effect=detached):
            with self.assertRaises(Error) as error: provider()
        self.assertEqual(error.exception.status, 404)
        detached.close()
        with patch('urllib.request.OpenerDirector.open', side_effect=URLError('not committed yet')):
            self.assertEqual(provider(), initial)
        with patch('urllib.request.OpenerDirector.open', side_effect=URLError('offline')), patch('localcloud.ec2_metadata.time.monotonic', return_value=10**12):
            with self.assertRaises(Error) as error: provider()
        self.assertEqual(error.exception.status, 503)

    def test_cli_profile_commands_build_aws_shaped_arguments(self):
        from localcloud.cli import parser
        from localcloud.ec2_cli import payload
        args = parser().parse_args(['ec2', 'associate-iam-instance-profile', '--name', 'web', '--iam-instance-profile', '{"Name":"worker"}'])
        self.assertEqual(payload(args, json.loads), {'Action': 'AssociateIamInstanceProfile', 'InstanceName': 'web', 'IamInstanceProfile': {'Name': 'worker'}})
        args = parser().parse_args(['ec2', 'replace-iam-instance-profile-association', '--association-id', 'iip-assoc-test', '--iam-instance-profile', '{"Name":"other"}'])
        self.assertEqual(payload(args, json.loads)['AssociationId'], 'iip-assoc-test')
        args = parser().parse_args(['ec2', 'describe-iam-instance-profile-associations', '--filters', '[{"Name":"state","Values":["associated"]}]'])
        self.assertEqual(payload(args, json.loads)['Filters'][0]['Values'], ['associated'])

    def test_existing_docker_guest_with_network_none_cannot_claim_role_delivery(self):
        item = self.launch()
        self.engine.ec2.images = {'ami-local': 'guest:dev'}
        self.engine.endpoint = 'http://192.0.2.1:4566'
        self.engine.ec2.runtime.helper_image = 'helper:dev'
        self.engine.ec2.runtime.imds = True
        with self.engine.transaction() as state:
            internal = state['instances'][item['InstanceId']]
            internal['Backend'] = 'docker'
            with patch.object(self.engine.ec2, 'container', return_value='owned-guest'), patch.object(self.engine.ec2, 'docker', return_value='none'):
                with self.assertRaises(Error) as error:
                    self.engine.ec2.profiles.require_runtime(internal)
            self.assertEqual(error.exception.status, 501)

    def test_bootstrap_busy_response_uses_only_unexpired_short_fallback(self):
        item = self.launch(IamInstanceProfile={'Name': 'worker'})
        initial = self.engine.identity.instance_credentials(item['InstanceId'])
        environment = {'LOCALCLOUD_IMDS_BROKER_URL': 'http://backend.test/credentials', 'LOCALCLOUD_IMDS_BROKER_TOKEN': 'capability', 'LOCALCLOUD_IMDS_INITIAL_CREDENTIALS': json.dumps(initial)}
        provider = broker_provider(environment)
        unavailable = HTTPError(environment['LOCALCLOUD_IMDS_BROKER_URL'], 503, 'Starting', {}, None)
        with patch('urllib.request.OpenerDirector.open', side_effect=unavailable):
            self.assertEqual(provider(), initial)
        initial['Credentials']['Expiration'] = '2000-01-01T00:00:00Z'
        environment['LOCALCLOUD_IMDS_INITIAL_CREDENTIALS'] = json.dumps(initial)
        provider = broker_provider(environment)
        unavailable = HTTPError(environment['LOCALCLOUD_IMDS_BROKER_URL'], 503, 'Starting', {}, None)
        with patch('urllib.request.OpenerDirector.open', side_effect=unavailable):
            with self.assertRaises(Error) as error: provider()
        self.assertEqual(error.exception.status, 503)
