import copy
import tempfile
import unittest
from pathlib import Path
from localcloud.engine import Engine, Error
from localcloud.ec2_templates import TemplateManager, validate_templates


class LaunchTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.db')
        self.engine = Engine(self.path)
        self.manager = TemplateManager(self.engine.ec2)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def call(self, operation, **args):
        return self.manager.call(operation, args, 'test')

    def template(self, **args):
        return self.call('CreateLaunchTemplate', LaunchTemplateName='web-template', LaunchTemplateData={'ImageId': 'ami-local', 'InstanceType': 't3.micro', 'UserData': 'ZWNobyBoaQ=='}, **args)['LaunchTemplate']

    def test_persistent_versions_defaults_and_resolution(self):
        template = self.template()
        key = template['LaunchTemplateId']
        v2 = self.call('CreateLaunchTemplateVersion', LaunchTemplateId=key, SourceVersion='1', LaunchTemplateData={'InstanceType': 't3.small'})['LaunchTemplateVersion']
        self.assertEqual(v2['LaunchTemplateData']['ImageId'], 'ami-local')
        self.assertEqual(v2['VersionNumber'], 2)
        self.call('ModifyLaunchTemplate', LaunchTemplateName='web-template', DefaultVersion='$Latest')
        self.engine.close()
        self.engine = Engine(self.path)
        self.manager = TemplateManager(self.engine.ec2)
        with self.engine.transaction() as data:
            args = self.manager.resolve(data, {'LaunchTemplateId': key})
            self.assertEqual(args['InstanceType'], 't3.small')
            self.assertEqual(args['_LaunchTemplate']['Version'], '2')
            validate_templates(data['ec2'])
        versions = self.call('DescribeLaunchTemplateVersions', LaunchTemplateId=key)['LaunchTemplateVersions']
        self.assertTrue(versions[0]['DefaultVersion'])
        self.assertFalse(versions[1]['DefaultVersion'])

    def test_idempotency_conflict_and_duplicate_names(self):
        first = self.template(ClientToken='stable')
        second = self.template(ClientToken='stable')
        self.assertEqual(first, second)
        with self.assertRaises(Error) as caught:
            self.call('CreateLaunchTemplate', LaunchTemplateName='web-template', ClientToken='stable', LaunchTemplateData={'ImageId': 'changed'})
        self.assertEqual(caught.exception.code, 'IdempotentParameterMismatch')
        with self.assertRaises(Error):
            self.template()
        v = dict(LaunchTemplateId=first['LaunchTemplateId'], ClientToken='version', SourceVersion='1', LaunchTemplateData={'InstanceType': 't3.small'})
        self.assertEqual(self.call('CreateLaunchTemplateVersion', **v), self.call('CreateLaunchTemplateVersion', **v))
        self.assertNotIn('_client_token', self.call('DescribeLaunchTemplateVersions', LaunchTemplateId=first['LaunchTemplateId'])['LaunchTemplateVersions'][0])

    def test_delete_default_error_and_non_reused_version_numbers(self):
        key = self.template()['LaunchTemplateId']
        self.call('CreateLaunchTemplateVersion', LaunchTemplateId=key, LaunchTemplateData={'ImageId': 'ami-two'})
        result = self.call('DeleteLaunchTemplateVersions', LaunchTemplateId=key, Versions=['1', '2', '999'])
        self.assertEqual(len(result['SuccessfullyDeletedLaunchTemplateVersions']), 1)
        self.assertEqual(len(result['UnsuccessfullyDeletedLaunchTemplateVersions']), 2)
        with self.engine.transaction() as data:
            self.assertEqual(self.manager.resolve(data, {'LaunchTemplateId': key, 'Version': '$Latest'})['_LaunchTemplate']['Version'], '1')
        result = self.call('CreateLaunchTemplateVersion', LaunchTemplateId=key, SourceVersion='1', LaunchTemplateData={'KeyName': 'new'})
        self.assertEqual(result['LaunchTemplateVersion']['VersionNumber'], 3)
        self.call('DeleteLaunchTemplate', LaunchTemplateId=key)
        self.assertEqual(self.call('DescribeLaunchTemplates', LaunchTemplateIds=[key]), {'LaunchTemplates': []})
        with self.assertRaises(Error):
            self.call('DescribeLaunchTemplateVersions', LaunchTemplateId=key)

    def test_primary_network_defaults_and_unsupported_fields(self):
        key = self.call('CreateLaunchTemplate', LaunchTemplateName='primary-network', LaunchTemplateData={'ImageId': 'ami-local', 'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': 'subnet-placeholder', 'Groups': ['sg-placeholder']}]})['LaunchTemplate']['LaunchTemplateId']
        with self.engine.transaction() as data:
            resolved = self.manager.resolve(data, {'LaunchTemplateId': key, 'Version': '$Default'})
        self.assertEqual(resolved['SubnetId'], 'subnet-placeholder')
        self.assertEqual(resolved['SecurityGroupIds'], ['sg-placeholder'])
        self.assertNotIn('NetworkInterfaces', resolved)
        for settings in [{'ImageId': 'ami-local', 'IamInstanceProfile': {'Name': 'admin'}}, {'NetworkInterfaces': [{'DeviceIndex': 1}]}, {'UserData': 'not base64'}, {'BlockDeviceMappings': [{'DeviceName': '/etc', 'Ebs': {'VolumeSize': 1}}]}]:
            with self.subTest(settings=settings), self.assertRaises(Error):
                self.call('CreateLaunchTemplate', LaunchTemplateName='bad-template', LaunchTemplateData=settings)

    def test_tag_filters_and_pagination_query_binding(self):
        for i in range(6):
            self.call('CreateLaunchTemplate', LaunchTemplateName='template-' + str(i), LaunchTemplateData={'ImageId': 'ami-local'}, TagSpecifications=[{'ResourceType': 'launch-template', 'Tags': [{'Key': 'env', 'Value': 'dev'}]}])
        page = self.call('DescribeLaunchTemplates', MaxResults=5, Filters=[{'Name': 'tag:env', 'Values': ['dev']}])
        self.assertEqual(len(page['LaunchTemplates']), 5)
        self.assertEqual(len(self.call('DescribeLaunchTemplates', MaxResults=5, NextToken=page['NextToken'], Filters=[{'Name': 'tag:env', 'Values': ['dev']}])['LaunchTemplates']), 1)
        with self.assertRaises(Error):
            self.call('DescribeLaunchTemplates', MaxResults=5, NextToken=page['NextToken'])

    def test_portable_validation_rejects_runtime_and_invalid_defaults(self):
        key = self.template()['LaunchTemplateId']
        with self.engine.transaction() as data:
            resources = copy.deepcopy(data['ec2'])
        validate_templates(resources)
        for mutate in [lambda t: t.update(DefaultVersionNumber=99), lambda t: t['Versions'][0]['LaunchTemplateData'].update(RootImage='host/image'), lambda t: t.update(Versions=[]), lambda t: t['Versions'][0].update(HostPath='/')]:
            bad = copy.deepcopy(resources)
            mutate(bad['launch_templates'][key])
            with self.assertRaises(ValueError):
                validate_templates(bad)

    def test_instance_export_data_and_authorization(self):
        item = self.engine.ec2.call('RunInstances', {'ImageId': 'ami-local'}, 'test')['Instances'][0]
        result = self.call('GetLaunchTemplateData', InstanceId=item['InstanceId'])['LaunchTemplateData']
        self.assertEqual(result['ImageId'], 'ami-local')
        self.assertNotIn('State', result.get('MetadataOptions', {}))
        key = self.call('CreateLaunchTemplate', LaunchTemplateName='from-instance', LaunchTemplateData=result)['LaunchTemplate']['LaunchTemplateId']
        self.assertTrue(key.startswith('lt-'))
        self.engine.iam_mode = 'enforce'
        with self.assertRaises(Error) as caught:
            self.call('DescribeLaunchTemplates')
        self.assertEqual(caught.exception.code, 'AccessDenied')
