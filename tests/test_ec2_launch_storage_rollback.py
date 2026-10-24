"""Launch rollback must retain the cause and attempt independent cleanup."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from localcloud.engine import Engine, Error


class LaunchStorageRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'state.db'))
        self.ec2 = self.engine.ec2
        self.ec2.images = {'ami-web': 'web:dev'}

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def launch(self, **extra):
        return self.ec2.call('RunInstances', {'ImageId': 'ami-web', **extra}, 'test')

    def test_docker_batch_rejected_before_creating_any_external_storage(self):
        with patch.object(self.ec2, 'docker') as docker, patch.object(self.ec2.runtime, 'create_volume') as volume:
            with self.assertRaises(Error) as caught:
                self.launch(MinCount=2, MaxCount=2, BlockDeviceMappings=[{'DeviceName': '/data', 'Ebs': {'VolumeSize': 1}}])
        self.assertEqual(caught.exception.code, 'InvalidParameterValue')
        docker.assert_not_called()
        volume.assert_not_called()
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_failure_cleanup_attempts_all_volumes_and_preserves_launch_error(self):
        failure = Error('RuntimeUnavailable', 'guest launch timed out', 503)
        def docker(*args):
            if args[0] == 'run':
                raise failure
            return ''
        with patch.object(self.ec2.infrastructure, 'block_devices', return_value=([], ['vol-first', 'vol-second'])), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.ec2, 'container', return_value='owned-guest'), patch.object(self.ec2.runtime, 'delete_volume', side_effect=[Error('RuntimeUnavailable', 'cleanup offline', 503), None]) as deleted:
            with self.assertRaises(Error) as caught:
                self.launch(BlockDeviceMappings=[{'DeviceName': '/data', 'Ebs': {'VolumeSize': 1}}], ClientToken='retry-launch')
        self.assertIs(caught.exception, failure)
        self.assertEqual(deleted.call_args_list, [call('vol-first'), call('vol-second')])
        self.assertTrue(any('vol-first' in note for note in failure.__notes__))
        self.assertEqual(self.engine.overview()['instances'], [])

    def test_helper_cleanup_error_does_not_skip_owned_guest_cleanup(self):
        failure = Error('RuntimeUnavailable', 'guest failed', 503)
        removed = []
        def docker(*args):
            if args[0] == 'run':
                raise failure
            removed.append(args)
            return ''
        with patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.ec2, 'container', return_value='owned-guest'), patch.object(self.ec2.runtime, 'imds_action', side_effect=Error('RuntimeUnavailable', 'helper cleanup failed', 503)):
            with self.assertRaises(Error) as caught:
                self.launch()
        self.assertIs(caught.exception, failure)
        self.assertIn(('rm', '-f', 'owned-guest'), removed)

    def test_ownership_mismatch_never_removes_foreign_guest(self):
        failure = Error('RuntimeFailure', 'port collision', 503)
        with patch.object(self.ec2, 'docker', side_effect=failure) as docker, patch.object(self.ec2, 'container', side_effect=Error('RuntimeFailure', 'foreign owner', 409)):
            with self.assertRaises(Error) as caught:
                self.launch()
        self.assertIs(caught.exception, failure)
        self.assertFalse(any(c.args[0] == 'rm' for c in docker.call_args_list))


if __name__ == '__main__':
    unittest.main()
