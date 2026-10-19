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

    def test_publish_validation_and_mock_rejection(self):
        from localcloud.ec2 import parse_publish, port_mappings
        for value in ['0.0.0.0:8080:8080', '80:8080', '8080:65536', 'abc:8080', '8080:8080/udp']:
            with self.assertRaises((Error, ValueError)):
                parse_publish(value)
        for value in [None, {}, [{'HostPort': True, 'ContainerPort': 8080}], [{'HostPort': 8080, 'ContainerPort': 8080, 'HostIp': '0.0.0.0'}], [{'HostPort': 8080, 'ContainerPort': 8080}]*2]:
            with self.assertRaises(Error):
                port_mappings(value)
        with self.assertRaisesRegex(Error, 'Docker EC2'):
            self.call('RunInstances', ImageId='ami-local', PortMappings=[{'HostPort': 8088, 'ContainerPort': 8080}])
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_publish_localhost_persistence_and_port_reuse(self):
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        ports = [{'HostPort': 8088, 'ContainerPort': 8080}]
        with patch.object(self.engine.ec2, 'docker', return_value='container') as docker:
            result = self.call('RunInstances', ImageId='ami-web', PortMappings=ports)['Instances'][0]
            argv = docker.call_args.args
            self.assertEqual(argv[argv.index('--publish') + 1], '127.0.0.1:8088:8080/tcp')
            self.assertEqual(argv[argv.index('--network') + 1], 'bridge')
            self.assertEqual(result['Endpoints'], ['http://127.0.0.1:8088'])
            identifier = result['InstanceId']
            with self.assertRaisesRegex(Error, 'already assigned'):
                self.call('RunInstances', ImageId='ami-web', PortMappings=ports)
            self.engine.close()
            self.engine = Engine(self.path, ec2_images={'ami-web': 'web:dev'})
            self.assertEqual(self.engine.overview()['instances'][0]['PortMappings'], ports)
            with patch.object(self.engine.ec2, 'docker', return_value=''), patch.object(self.engine.ec2, 'container', return_value='localcloud-' + identifier):
                self.call('StopInstances', InstanceIds=[identifier])
                self.call('StartInstances', InstanceIds=[identifier])
                self.call('TerminateInstances', InstanceIds=[identifier])
                self.call('RunInstances', ImageId='ami-web', PortMappings=ports)

    def test_publish_cli_payload_and_launch_failure(self):
        args = parser().parse_args(['ec2', 'run-instances', '--image-id', 'ami-web', '--publish', '8088:8080', '--publish', '9090:9090'])
        client = unittest.mock.Mock()
        execute(args, client)
        self.assertEqual(client.request.call_args.args[2]['PortMappings'], [{'HostPort': 8088, 'ContainerPort': 8080}, {'HostPort': 9090, 'ContainerPort': 9090}])
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        with patch.object(self.engine.ec2, 'docker', side_effect=Error('RuntimeFailure', 'port busy', 503)):
            with self.assertRaises(Error):
                self.call('RunInstances', ImageId='ami-web', PortMappings=[{'HostPort': 8088, 'ContainerPort': 8080}])
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_failed_publish_cleans_up_only_owned_container(self):
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        label = None
        def docker_operation(*args):
            nonlocal label
            if args[0] == 'run':
                label = args[args.index('--label') + 1].split('=', 1)[1]
                raise Error('RuntimeFailure', 'port conflict', 503)
            if args[0] == 'inspect':
                return json.dumps({'localcloud.owner': label})
            return ''
        with patch.object(self.engine.ec2, 'docker', side_effect=docker_operation) as docker:
            with self.assertRaises(Error):
                self.call('RunInstances', ImageId='ami-web', PortMappings=[{'HostPort': 8088, 'ContainerPort': 8080}])
            self.assertEqual(docker.call_args.args[:2], ('rm', '-f'))
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_named_lifecycle_restart_and_rename(self):
        launched = self.call('RunInstances', ImageId='ami-local', Name='web')['Instances'][0]
        identifier = launched['InstanceId']
        self.engine.close()
        self.engine = Engine(self.path)
        self.call('StopInstances', InstanceName='web')
        self.assertEqual(self.call('DescribeInstances', InstanceName='web')['Reservations'][0]['Instances'][0]['State']['Name'], 'stopped')
        self.call('StartInstances', InstanceName='web')
        self.call('NameInstance', InstanceName='web', Name='renamed')
        with self.assertRaises(Error):
            self.call('StopInstances', InstanceName='web')
        self.assertEqual(self.call('DescribeInstances', InstanceName='renamed')['Reservations'][0]['Instances'][0]['InstanceId'], identifier)
        self.engine.tools.import_state(self.engine.tools.export_state())
        self.call('TerminateInstances', InstanceName='renamed')
        with self.assertRaises(Error):
            self.call('StartInstances', InstanceName='renamed')
        self.assertEqual(self.call('DescribeInstances', InstanceName='renamed')['Reservations'][0]['Instances'][0]['Name'], 'renamed')

    def test_name_validation_uniqueness_and_existing_instance_naming(self):
        identifier = self.launch()
        self.call('NameInstance', InstanceIds=[identifier], Name='existing')
        for args in [{'ImageId': 'ami-local', 'Name': 'existing'}, {'ImageId': 'ami-local', 'Name': 'bad name'}, {'ImageId': 'ami-local', 'Name': 'two', 'MaxCount': 2}]:
            with self.assertRaises(Error):
                self.call('RunInstances', **args)
        other = self.launch()
        with self.assertRaises(Error):
            self.call('NameInstance', InstanceIds=[other], Name='existing')
        with self.assertRaises(Error):
            self.call('StopInstances', InstanceIds=[identifier], InstanceName='existing')
        with self.assertRaises(Error):
            self.call('StopInstances', InstanceName='missing')
        self.assertEqual(self.call('DescribeInstances', InstanceIds=[identifier])['Reservations'][0]['Instances'][0]['State']['Name'], 'running')

    def test_container_and_port_selectors_and_ambiguity(self):
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        with patch.object(self.engine.ec2, 'docker', return_value='a'*64), patch.object(self.engine.ec2, 'container', return_value='owned'):
            item = self.call('RunInstances', ImageId='ami-web', Name='web', PortMappings=[{'HostPort': 8088, 'ContainerPort': 8080}])['Instances'][0]
            self.assertEqual(item['ContainerId'], 'a'*64)
            self.assertEqual(item['ContainerName'], 'localcloud-' + item['InstanceId'])
            self.call('StopInstances', ContainerId='a'*12)
            self.call('StartInstances', ContainerName=item['ContainerName'])
            self.call('StopInstances', HostPort=8088)
            self.call('StartInstances', InstanceName='web')
            self.call('RunInstances', ImageId='ami-web')  # Same mocked container ID must be rejected as ambiguous.
            with self.assertRaisesRegex(Error, 'Multiple'):
                self.call('StopInstances', ContainerId='a'*12)
            with self.assertRaises(Error):
                self.call('StopInstances', ContainerName='unmanaged-container')
            self.call('TerminateInstances', InstanceName='web')
            with self.assertRaises(Error):
                self.call('StopInstances', HostPort=8088)

    def test_selector_iam_cannot_bypass_instance_policy(self):
        identifier = self.call('RunInstances', ImageId='ami-local', Name='protected')['Instances'][0]['InstanceId']
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error):
            self.call('StopInstances', InstanceName='protected')
        self.engine.policy('test', {'Statement': {'Effect': 'Allow', 'Action': 'ec2:StopInstances', 'Resource': 'arn:aws:ec2:us-east-1:000000000000:instance/' + identifier}})
        self.call('StopInstances', InstanceName='protected')
        self.assertEqual(self.engine.tools.traces()[0]['resource'], 'arn:aws:ec2:us-east-1:000000000000:instance/' + identifier)

    def test_identifier_cli_mapping(self):
        for option, value, key in [('--name', 'web', 'InstanceName'), ('--container-id', 'a'*12, 'ContainerId'), ('--container-name', 'localcloud-i-123', 'ContainerName'), ('--host-port', '8088', 'HostPort')]:
            client = unittest.mock.Mock()
            args = parser().parse_args(['ec2', 'stop-instances', option, value])
            execute(args, client)
            self.assertEqual(client.request.call_args.args[2][key], int(value) if key == 'HostPort' else value)
        client = unittest.mock.Mock()
        execute(parser().parse_args(['ec2', 'name-instance', '--host-port', '8088', '--new-name', 'web']), client)
        self.assertEqual(client.request.call_args.args[2], {'Action': 'NameInstance', 'HostPort': 8088, 'Name': 'web'})
        client = unittest.mock.Mock()
        execute(parser().parse_args(['ec2', 'run-instances', '--image-id', 'ami-local', '--name', 'web']), client)
        self.assertEqual(client.request.call_args.args[2]['Name'], 'web')

    def test_legacy_container_id_backfill_and_authenticated_name_route(self):
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        with patch.object(self.engine.ec2, 'docker', return_value='b'*64):
            identifier = self.call('RunInstances', ImageId='ami-web')['Instances'][0]['InstanceId']
        with self.engine.transaction() as data:
            del data['instances'][identifier]['ContainerId']
        def docker_operation(*args):
            if args[0] == 'inspect' and args[-2] == '{{json .Config.Labels}}':
                with self.engine.transaction() as data:
                    return json.dumps({'localcloud.owner': data['instances'][identifier]['Owner']})
            if args[0] == 'inspect' and args[-2] == '{{.Id}}':
                return 'b'*64
            if args[0] == 'inspect' and args[-2] == '{{.State.Running}}':
                return 'true'
            return ''
        with patch.object(self.engine.ec2, 'docker', side_effect=docker_operation):
            item = self.call('DescribeInstances', ContainerId='b'*12)['Reservations'][0]['Instances'][0]
            self.assertEqual(item['ContainerId'], 'b'*64)
            payload = json.dumps({'Action': 'NameInstance', 'ContainerId': 'b'*64, 'Name': 'old-web'}).encode()
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer private'}
            status, body = self.request('/_local/ec2', payload, 'POST', headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['Instance']['Name'], 'old-web')

    def test_modify_instance_type_requires_stopped_and_authorized_instance(self):
        identifier = self.call('RunInstances', ImageId='ami-local', Name='resize')['Instances'][0]['InstanceId']
        with self.assertRaisesRegex(Error, 'Stop the instance'):
            self.call('ModifyInstanceAttribute', InstanceName='resize', InstanceType='t3.small')
        self.call('StopInstances', InstanceName='resize')
        changed = self.call('ModifyInstanceAttribute', InstanceName='resize', InstanceType='t3.small')
        self.assertEqual(changed['Instance']['InstanceType'], 't3.small')
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error):
            self.call('ModifyInstanceAttribute', InstanceIds=[identifier], InstanceType='t3.medium')
        self.engine.iam_mode = 'off'
        self.assertEqual(self.call('DescribeInstances', InstanceName='resize')['Reservations'][0]['Instances'][0]['InstanceType'], 't3.small')
        self.call('TerminateInstances', InstanceName='resize')
        with self.assertRaises(Error):
            self.call('ModifyInstanceAttribute', InstanceName='resize', InstanceType='t3.medium')

    def test_docker_resize_updates_limits_and_preserves_state_on_failure(self):
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        with patch.object(self.engine.ec2, 'docker', return_value='c'*64), patch.object(self.engine.ec2, 'container', return_value='owned'):
            self.call('RunInstances', ImageId='ami-web', Name='resize')
            self.call('StopInstances', InstanceName='resize')
        with patch.object(self.engine.ec2, 'container', return_value='owned'), patch.object(self.engine.ec2, 'docker', return_value='false') as docker:
            self.call('ModifyInstanceAttribute', InstanceName='resize', InstanceType='t3.medium')
            self.assertEqual(docker.call_args.args, ('update', '--memory', '512m', '--memory-swap', '1024m', 'owned'))
        def fail_update(*args):
            if args[0] == 'update':
                raise Error('RuntimeFailure', 'failed', 503)
            return 'false'
        with patch.object(self.engine.ec2, 'container', return_value='owned'), patch.object(self.engine.ec2, 'docker', side_effect=fail_update):
            with self.assertRaises(Error):
                self.call('ModifyInstanceAttribute', InstanceName='resize', InstanceType='t3.small')
        self.assertEqual(self.engine.overview()['instances'][0]['InstanceType'], 't3.medium')
