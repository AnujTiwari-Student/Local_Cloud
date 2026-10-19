import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
import test_http
from localcloud.engine import Engine, Error
from localcloud.cli import parser, execute


class EC2Tests(unittest.TestCase):
    request = test_http.HTTPTests.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'db')
        self.engine = Engine(self.path)
        self.server = SimpleNamespace(engine=self.engine, management_token='private')

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return self.engine.ec2.call(operation, args, 'test')

    def launch(self):
        return self.call('RunInstances', ImageId='ami-local')['Instances'][0]['InstanceId']

    def test_lifecycle_persistence_and_termination(self):
        identifier = self.launch()
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.call('StopInstances', InstanceIds=[identifier])['StoppingInstances'][0]['CurrentState']['Name'], 'stopped')
        self.assertEqual(self.call('StartInstances', InstanceIds=[identifier])['StartingInstances'][0]['PreviousState']['Name'], 'stopped')
        self.call('TerminateInstances', InstanceIds=[identifier])
        self.call('TerminateInstances', InstanceIds=[identifier])
        with self.assertRaisesRegex(Error, 'Terminated'):
            self.call('StartInstances', InstanceIds=[identifier])

    def test_validation_and_atomic_missing_id(self):
        identifier = self.launch()
        for operation, args in [('RunInstances', {'ImageId': 'bad'}), ('RunInstances', {'ImageId': 'ami-ok', 'MaxCount': 20}),
                                ('StopInstances', {}), ('DescribeInstances', {'Filters': []}),
                                ('StopInstances', {'InstanceIds': [identifier, 'i-' + '0'*17]})]:
            with self.assertRaises(Error):
                self.call(operation, **args)
        self.assertEqual(self.call('DescribeInstances')['Reservations'][0]['Instances'][0]['State']['Name'], 'running')

    def test_iam_checks_every_instance(self):
        ids = [self.launch(), self.launch()]
        self.engine.iam_mode = 'enforce'
        self.engine.policy('test', {'Statement': {'Effect': 'Allow', 'Action': 'ec2:*', 'Resource': 'arn:aws:ec2:us-east-1:000000000000:instance/' + ids[0]}})
        with self.assertRaises(Error):
            self.call('StopInstances', InstanceIds=ids)
        self.assertEqual(self.engine.overview()['instances'][0]['State']['Name'], 'running')

    def test_snapshots_and_portable_state(self):
        identifier = self.launch()
        self.engine.snapshot('before')
        bundle = self.engine.tools.export_state()
        self.call('TerminateInstances', InstanceIds=[identifier])
        self.engine.snapshot('before', restore=True)
        self.assertEqual(self.engine.overview()['instances'][0]['State']['Name'], 'running')
        self.engine.tools.import_state(bundle)
        corrupt = json.loads(json.dumps(bundle))
        corrupt['state']['instances'][identifier]['Backend'] = 'docker'
        import hashlib
        corrupt['sha256'] = hashlib.sha256(json.dumps(corrupt['state'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.assertRaises(Error):
            self.engine.tools.import_state(corrupt)

    def test_query_protocol_and_cli_management_token(self):
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        status, body = self.request(data=urlencode({'Action': 'RunInstances', 'ImageId': 'ami-local', 'Version': '2016-11-15'}).encode(), method='POST', headers=headers)
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        identifier = root.findtext('.//{*}instanceId')
        self.assertTrue(identifier.startswith('i-'))
        status, body = self.request(data=urlencode({'Action': 'DescribeInstances', 'InstanceId.1': identifier}).encode(), method='POST', headers=headers)
        self.assertEqual(ET.fromstring(body).findtext('.//{*}instanceState/{*}name'), 'running')
        payload = json.dumps({'Action': 'StopInstances', 'InstanceIds': [identifier]}).encode()
        self.assertEqual(self.request('/_local/ec2', payload, 'POST', {'Content-Type': 'application/json'})[0], 401)
        self.assertEqual(self.request('/_local/ec2', payload, 'POST', {'Content-Type': 'application/json', 'Authorization': 'Bearer private'})[0], 200)
        args = parser().parse_args(['ec2', 'run-instances', '--image-id', 'ami-local', '--count', '2'])
        client = unittest.mock.Mock()
        execute(args, client)
        self.assertEqual(client.request.call_args.args[2]['MaxCount'], 2)

    def test_faults_and_traces(self):
        self.engine.tools.set_rule({'name': 'outage', 'service': 'ec2', 'operation': 'RunInstances', 'error': {'code': 'Unavailable', 'status': 503}})
        with self.assertRaises(Error):
            self.launch()
        self.assertEqual(self.engine.tools.traces()[0]['service'], 'ec2')
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_docker_allowlist_ownership_and_repeated_operations(self):
        self.engine.ec2.images = {'ami-local': 'approved:dev'}
        with patch.object(self.engine.ec2, 'docker', return_value='container') as docker:
            with self.assertRaises(Error):
                self.call('RunInstances', ImageId='ami-unlisted')
            identifier = self.launch()
            argv = docker.call_args.args
            self.assertIn('--network', argv)
            self.assertEqual(argv[-1], 'approved:dev')
            item = self.engine.overview()['instances'][0]
            with patch.object(self.engine.ec2, 'container', return_value='localcloud-' + identifier):
                docker.return_value = 'false'
                described = self.call('DescribeInstances')['Reservations'][0]['Instances'][0]
                self.assertEqual(described['State']['Name'], 'stopped')
                docker.return_value = 'true'
                described = self.call('DescribeInstances')['Reservations'][0]['Instances'][0]
                self.assertEqual(described['State']['Name'], 'running')
            with patch.object(self.engine.ec2, 'container', return_value='localcloud-' + identifier):
                self.call('StopInstances', InstanceIds=[identifier])
                self.call('StartInstances', InstanceIds=[identifier])
                self.call('TerminateInstances', InstanceIds=[identifier])
                count = docker.call_count
                self.call('TerminateInstances', InstanceIds=[identifier])
                self.assertEqual(docker.call_count, count)
            with self.assertRaises(Error):
                self.engine.tools.import_state({})
        with patch.object(self.engine.ec2, 'docker', return_value='{"localcloud.owner":"wrong"}'):
            with self.assertRaisesRegex(Error, 'ownership'):
                self.engine.ec2.container({**item, 'Owner': 'expected'})

    def test_runtime_failure_does_not_commit_instance(self):
        self.engine.ec2.images = {'ami-local': 'approved:dev'}
        with patch.object(self.engine.ec2, 'docker', side_effect=Error('RuntimeFailure', 'failed', 503)):
            with self.assertRaises(Error):
                self.launch()
        self.assertEqual(self.engine.overview()['instances'], [])
        self.assertEqual(self.engine.tools.traces()[0]['status'], 503)
