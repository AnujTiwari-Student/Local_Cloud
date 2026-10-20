import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from localcloud.engine import Engine, Error
from localcloud.ec2_images import ImageLifecycle, validate_images


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.sqlite3')
        self.engine = Engine(self.path)
        self.images = ImageLifecycle(self.engine.ec2.infrastructure)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def action(self, op, **args):
        with self.engine.transaction() as data:
            return self.images.perform(op, args, data, 'test')

    def describe(self, **args):
        with self.engine.transaction() as data:
            return self.images.describe(data, args)['Images']

    def register(self, **args):
        return self.action('RegisterImage', Name='my-app', **args)['ImageId']

    def test_lifecycle_mutations_persist_and_deregistered_cannot_resolve(self):
        key = self.register(Description='original')
        self.action('ModifyImageAttribute', ImageId=key, Description={'Value': 'updated'})
        self.action('ModifyImageAttribute', ImageId=key, LaunchPermission={'Add': [{'Group': 'all'}, {'UserId': '123456789012'}]})
        self.engine.close()
        self.engine = Engine(self.path)
        self.images = ImageLifecycle(self.engine.ec2.infrastructure)
        self.assertTrue(self.describe(ImageIds=[key])[0]['Public'])
        self.assertEqual(self.action('DescribeImageAttribute', ImageId=key, Attribute='description')['Description']['Value'], 'updated')
        self.action('ResetImageAttribute', ImageId=key, Attribute='launchPermission')
        self.assertFalse(self.describe(ImageIds=[key])[0]['Public'])
        self.action('DeregisterImage', ImageId=key)
        self.assertEqual(self.describe(ImageIds=[key]), [])
        for op in ('DeregisterImage', 'DescribeImageAttribute'):
            with self.assertRaises(Error) as caught:
                self.action(op, ImageId=key, Attribute='description')
            self.assertEqual(caught.exception.code, 'InvalidAMIID.Unavailable')

    def test_catalogue_filters_tags_and_permission_batch_atomicity(self):
        key = self.register(TagSpecifications=[{'ResourceType': 'image', 'Tags': [{'Key': 'stage', 'Value': 'production'}]}])
        self.assertEqual([i['ImageId'] for i in self.describe(Filters=[{'Name': 'tag:stage', 'Values': ['prod*']}])], [key])
        self.assertEqual([i['ImageId'] for i in self.describe(Filters=[{'Name': 'name', 'Values': ['my-*']}])], [key])
        self.assertEqual(self.describe(Owners=['123456789012']), [])
        with self.assertRaises(Error):
            self.action('ModifyImageAttribute', ImageId=key, LaunchPermission={'Add': [{'Group': 'all'}, {'UserId': 'bad'}]})
        self.assertFalse(self.describe(ImageIds=[key])[0]['Public'])
        self.assertEqual(self.action('DescribeImageAttribute', ImageId=key, Attribute='launchPermission')['LaunchPermissions'], [])

    def test_create_mock_image_and_reject_invalid_sources(self):
        instance = self.engine.ec2.call('RunInstances', {'ImageId': 'ami-local'}, 'test')['Instances'][0]['InstanceId']
        key = self.action('CreateImage', InstanceId=instance, Name='snapshot', NoReboot=True)['ImageId']
        self.assertEqual(self.describe(ImageIds=[key])[0]['SourceInstanceId'], instance)
        self.engine.ec2.call('TerminateInstances', {'InstanceIds': [instance]}, 'test')
        with self.assertRaises(Error) as caught:
            self.action('CreateImage', InstanceId=instance, Name='late')
        self.assertEqual(caught.exception.code, 'IncorrectInstanceState')

    def test_register_snapshot_mapping_validates_before_persist(self):
        volume = self.engine.ec2.call('CreateVolume', {'AvailabilityZone': 'us-east-1a', 'Size': 2}, 'test')['VolumeId']
        snap = self.engine.ec2.call('CreateSnapshot', {'VolumeId': volume}, 'test')['SnapshotId']
        mapping = [{'DeviceName': '/dev/sda1', 'Ebs': {'SnapshotId': snap, 'VolumeSize': 2, 'DeleteOnTermination': True}}]
        key = self.register(BlockDeviceMappings=mapping)
        self.assertEqual(self.describe(ImageIds=[key])[0]['RootDeviceType'], 'ebs')
        with self.assertRaises(Error):
            self.register(BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {'SnapshotId': 'snap-00000000000000000'}}])
        with self.engine.transaction() as data:
            self.assertEqual(len(self.images.store(data)), 1)
            validate_images(self.images.store(data))

    def test_portable_catalogue_rejects_host_runtime_metadata(self):
        key = self.register()
        with self.engine.transaction() as data:
            images = copy.deepcopy(self.images.store(data))
        for field, value in [('_docker_image', 'arbitrary:tag'), ('RootImage', '/host'), ('_launch_permissions', [{'UserId': 'invalid'}]), ('Public', 'true')]:
            forged = copy.deepcopy(images)
            forged[key][field] = value
            with self.assertRaises(ValueError):
                validate_images(forged)

    def test_docker_capture_resolve_and_owned_cleanup(self):
        ec2 = self.engine.ec2
        ec2.images = {'ami-web': 'approved:web'}
        instance = {'InstanceId': 'i-' + 'a' * 17, 'ImageId': 'ami-web', 'Backend': 'docker', 'State': {'Name': 'stopped'}, 'Owner': 'owner'}
        with self.engine.transaction() as data:
            data.setdefault('instances', {})[instance['InstanceId']] = instance
        ec2.container = Mock(return_value='localcloud-' + instance['InstanceId'])
        ec2.docker = Mock(return_value='false')
        ec2.runtime.owned = Mock(return_value=True)
        key = self.action('CreateImage', InstanceId=instance['InstanceId'], Name='committed')['ImageId']
        self.assertEqual(ec2.docker.call_args_list[1].args[:2], ('commit', '--change'))
        with self.engine.transaction() as data:
            self.images.resolve(data, key)
            self.assertEqual(ec2.images[key], 'localcloud-ec2-ami:' + key)
            with self.assertRaises(ValueError):
                validate_images(self.images.store(data))
        self.action('DeregisterImage', ImageId=key)
        self.assertNotIn(key, ec2.images)
        self.assertIn(('image', 'rm', 'localcloud-ec2-ami:' + key), [x.args for x in ec2.docker.call_args_list])

    def test_docker_capture_requires_stopped_and_volume_free_source(self):
        ec2 = self.engine.ec2
        ec2.images = {'ami-web': 'approved:web'}
        for state, mappings in [('running', []), ('stopped', [{'DeviceName': '/data'}])]:
            instance = {'InstanceId': 'i-' + 'a' * 17, 'ImageId': 'ami-web', 'Backend': 'docker', 'State': {'Name': state}, 'BlockDeviceMappings': mappings}
            with self.engine.transaction() as data:
                data.setdefault('instances', {})[instance['InstanceId']] = instance
            ec2.docker = Mock()
            with self.assertRaises(Error):
                self.action('CreateImage', InstanceId=instance['InstanceId'], Name='invalid')
            ec2.docker.assert_not_called()
        with self.assertRaises(Error):
            self.register()

    def test_runtime_owner_mismatch_never_changes_allowlist(self):
        key = self.register()
        with self.engine.transaction() as data:
            item = self.images.store(data)[key]
            item.update(_docker_image='localcloud-ec2-ami:' + key, _docker_owner='some-other-database')
            with self.assertRaises(Error):
                self.images.resolve(data, key)
            self.assertNotIn(key, self.engine.ec2.images)


if __name__ == '__main__':
    unittest.main()
