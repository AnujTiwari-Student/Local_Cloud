import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from localcloud.engine import Engine, Error
from localcloud.ec2 import query_request
from localcloud.cli import parser, execute


class EC2ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.db')
        self.engine = Engine(self.path)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return self.engine.ec2.call(operation, args, 'test')

    def launch(self, **args):
        return self.call('RunInstances', ImageId='ami-local', **args)

    def test_transition_responses_and_settled_persistence(self):
        launched = self.launch()
        item = launched['Instances'][0]
        self.assertEqual(item['State'], {'Code': 0, 'Name': 'pending'})
        identifier = item['InstanceId']
        described = self.call('DescribeInstances')['Reservations'][0]
        self.assertEqual(described['ReservationId'], launched['ReservationId'])
        self.assertEqual(described['Instances'][0]['State']['Name'], 'running')
        stopped = self.call('StopInstances', InstanceIds=[identifier])['StoppingInstances'][0]
        self.assertEqual(stopped['CurrentState']['Name'], 'stopping')
        again = self.call('StopInstances', InstanceIds=[identifier])['StoppingInstances'][0]
        self.assertEqual(again['PreviousState']['Name'], 'stopped')
        self.call('StartInstances', InstanceIds=[identifier])
        terminated = self.call('TerminateInstances', InstanceIds=[identifier])['TerminatingInstances'][0]
        self.assertEqual(terminated['CurrentState']['Name'], 'shutting-down')
        self.assertEqual(self.engine.overview()['instances'][0]['State']['Name'], 'terminated')

    def test_named_idempotency_across_restart_and_mismatch(self):
        first = self.launch(Name='web', ClientToken='launch-1')
        self.engine.close()
        self.engine = Engine(self.path)
        repeated = self.launch(Name='web', ClientToken='launch-1', MinCount=1, MaxCount=1, InstanceType='t3.micro')
        self.assertEqual(first['Instances'][0]['InstanceId'], repeated['Instances'][0]['InstanceId'])
        self.assertEqual(first['ReservationId'], repeated['ReservationId'])
        with self.assertRaises(Error) as caught:
            self.launch(Name='web', ClientToken='launch-1', InstanceType='t3.small')
        self.assertEqual(caught.exception.code, 'IdempotentParameterMismatch')
        self.assertEqual(len(self.engine.overview()['instances']), 1)

    def test_tags_filters_and_ambiguous_name(self):
        spec = [{'ResourceType': 'instance', 'Tags': [{'Key': 'Name', 'Value': 'shared'}, {'Key': 'Env', 'Value': 'test'}]}]
        identifier = self.launch(TagSpecifications=spec)['Instances'][0]['InstanceId']
        self.launch(TagSpecifications=spec)
        with self.assertRaises(Error) as caught:
            self.call('DescribeInstances', InstanceName='shared')
        self.assertEqual(caught.exception.code, 'AmbiguousInstance')
        self.call('CreateTags', Resources=[identifier], Tags=[{'Key': 'Env', 'Value': 'dev'}])
        result = self.call('DescribeInstances', Filters=[{'Name': 'tag:Env', 'Values': ['d*']}])
        self.assertEqual([i['InstanceId'] for r in result['Reservations'] for i in r['Instances']], [identifier])
        self.call('DeleteTags', Resources=[identifier], Tags=[{'Key': 'Env', 'Value': 'wrong'}])
        self.assertEqual(len(self.call('DescribeTags', Filters=[{'Name': 'value', 'Values': ['dev']}])['Tags']), 1)
        self.call('DeleteTags', Resources=[identifier], Tags=[{'Key': 'Env'}])
        self.assertEqual(self.call('DescribeTags', Filters=[{'Name': 'value', 'Values': ['dev']}])['Tags'], [])

    def test_tag_batch_atomic_and_iam_denial(self):
        identifier = self.launch()['Instances'][0]['InstanceId']
        with self.assertRaises(Error):
            self.call('CreateTags', Resources=[identifier, 'i-' + '0' * 17], Tags=[{'Key': 'Env', 'Value': 'dev'}])
        self.assertEqual(self.call('DescribeTags')['Tags'], [])
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error):
            self.call('CreateTags', Resources=[identifier], Tags=[{'Key': 'Env', 'Value': 'dev'}])
        self.engine.iam_mode = 'off'
        self.assertEqual(self.call('DescribeTags')['Tags'], [])

    def test_catalog_attribute_and_portable_metadata(self):
        self.assertEqual(self.call('DescribeImages')['Images'][0]['ImageId'], 'ami-local')
        with self.assertRaises(Error) as caught:
            self.call('DescribeImages', ImageIds=['ami-missing'])
        self.assertEqual(caught.exception.code, 'InvalidAMIID.NotFound')
        user_data = base64.b64encode(b'echo hello').decode()
        identifier = self.launch(UserData=user_data, ClientToken='portable')['Instances'][0]['InstanceId']
        self.assertEqual(self.call('DescribeInstanceAttribute', InstanceIds=[identifier], Attribute='userData')['UserData']['Value'], user_data)
        self.assertNotIn('UserData', self.call('DescribeInstances')['Reservations'][0]['Instances'][0])
        bundle = self.engine.tools.export_state()
        self.engine.tools.import_state(bundle)
        self.assertEqual(self.launch(UserData=user_data, ClientToken='portable')['Instances'][0]['InstanceId'], identifier)
        self.engine.ec2.images = {'ami-web': 'web:dev'}
        self.assertEqual(self.call('DescribeImages', ImageIds=['ami-web'])['Images'][0]['Tags'][0]['Value'], 'docker')

    def test_query_nested_lists_and_collision_rejection(self):
        from urllib.parse import urlencode
        action, args = query_request(urlencode({'Action': 'RunInstances', 'ImageId': 'ami-local',
            'TagSpecification.1.ResourceType': 'instance', 'TagSpecification.1.Tag.1.Key': 'Env',
            'TagSpecification.1.Tag.1.Value': 'dev'}).encode())
        self.assertEqual(args['TagSpecifications'][0]['Tags'], [{'Key': 'Env', 'Value': 'dev'}])
        self.call(action, **args)
        for body in [b'Action=DescribeInstances&InstanceId.2=x', b'Action=DescribeInstances&InstanceId=x&InstanceId.1=y',
                     b'Action=DescribeInstances&Filter=x&Filter.1.Name=tag-key']:
            with self.assertRaises(Error):
                query_request(body)

    def test_docker_external_exit_is_started_and_user_data_is_bounded(self):
        self.engine.ec2.images = {'ami-local': 'approved:dev'}
        with patch.object(self.engine.ec2, 'docker', return_value='container') as docker:
            identifier = self.launch(UserData=base64.b64encode(b'echo hello >/tmp/hello').decode())['Instances'][0]['InstanceId']
            self.assertTrue(any(c.args[:2] == ('exec', '--user') for c in docker.call_args_list))
            with patch.object(self.engine.ec2, 'container', return_value='localcloud-' + identifier):
                docker.return_value = 'false'
                self.call('StartInstances', InstanceIds=[identifier])
                self.assertIn(unittest.mock.call('start', 'localcloud-' + identifier), docker.call_args_list)
            with self.assertRaises(Error):
                self.launch(UserData=base64.b64encode(b'x' * 16385).decode())

    def test_cli_tags_filters_images_and_script(self):
        client = Mock()
        execute(parser().parse_args(['ec2', 'create-tags', '--resources', 'i-' + 'a' * 17, '--tags', 'Key=Env,Value=dev']), client)
        self.assertEqual(client.request.call_args.args[2]['Tags'], [{'Key': 'Env', 'Value': 'dev'}])
        execute(parser().parse_args(['ec2', 'describe-images', '--image-ids', 'ami-local']), client)
        self.assertEqual(client.request.call_args.args[2]['ImageIds'], ['ami-local'])
        execute(parser().parse_args(['ec2', 'run-instances', '--image-id', 'ami-local', '--client-token', 'web', '--user-data', 'echo hello']), client)
        self.assertEqual(base64.b64decode(client.request.call_args.args[2]['UserData']), b'echo hello')
