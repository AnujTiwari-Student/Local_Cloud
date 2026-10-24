"""Storage failure compensation and guest pause-state preservation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcloud.engine import Engine, Error


class DockerFixture:
    def __init__(self, runtime):
        self.runtime = runtime
        self.volumes = {}
        self.helpers = {}
        self.events = []
        self.failure = None
        self.cleanup_failure = False

    def __call__(self, *args):
        self.events.append(args)
        if args[:2] == ('volume', 'ls'):
            return '\n'.join(self.volumes)
        if args[:2] == ('container', 'ls'):
            return '\n'.join(self.helpers)
        if args[:2] == ('volume', 'create'):
            labels = dict(args[n + 1].split('=', 1) for n, v in enumerate(args) if v == '--label')
            self.volumes.setdefault(args[-1], labels)
            return args[-1]
        if args[0] in ('volume', 'container') and args[1] == 'inspect':
            resources = self.volumes if args[0] == 'volume' else self.helpers
            if args[-1] not in resources:
                raise Error('RuntimeFailure', 'not found', 503)
            return json.dumps(resources[args[-1]])
        if args[0] == 'run':
            name = args[args.index('--name') + 1]
            self.helpers[name] = dict(args[n + 1].split('=', 1) for n, v in enumerate(args) if v == '--label')
            if self.failure:
                raise self.failure
            del self.helpers[name]
            return ''
        if args[:2] == ('container', 'rm'):
            if self.cleanup_failure:
                raise Error('RuntimeFailure', 'daemon offline', 503)
            del self.helpers[args[-1]]
            return ''
        if args[:2] == ('volume', 'rm'):
            if self.cleanup_failure:
                raise Error('RuntimeFailure', 'daemon offline', 503)
            del self.volumes[args[-1]]
            return ''
        raise AssertionError(args)


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'state.db'))
        self.ec2 = self.engine.ec2
        self.ec2.images = {'ami-test': 'fixture-image'}
        self.runtime = self.ec2.runtime
        self.runtime.helper_image = 'fixture-helper'
        self.docker = DockerFixture(self.runtime)
        self.patch = patch.object(self.ec2, 'docker', side_effect=self.docker)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.engine.close()
        self.tmp.cleanup()

    def test_timed_out_helper_removed_before_destination_and_original_error_retained(self):
        failure = Error('RuntimeUnavailable', 'copy timed out', 503)
        self.docker.failure = failure
        with self.assertRaises(Error) as caught:
            self.runtime.create_volume('snap-copy')
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.docker.helpers)
        self.assertFalse(self.docker.volumes)
        removed = [event[:2] for event in self.docker.events if len(event) > 1 and event[1] == 'rm']
        self.assertEqual(removed, [('container', 'rm'), ('volume', 'rm')])

    def test_preexisting_destination_never_initialized_or_deleted(self):
        for owner in (self.runtime.owner, 'somebody-else'):
            name = self.runtime.volume_name('snap-existing')
            self.docker.volumes[name] = {'localcloud.runtime': owner}
            with self.assertRaises(Error) as caught:
                self.runtime.create_volume('snap-existing')
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.docker.volumes[name], {'localcloud.runtime': owner})
        self.assertFalse(any(event[0] == 'run' or 'rm' in event[:2] for event in self.docker.events))

    def test_raced_foreign_destination_is_not_used_or_removed(self):
        name = self.runtime.volume_name('snap-race')
        original = self.docker
        def raced(*args):
            if args[:2] == ('volume', 'create'):
                original.volumes[name] = {'localcloud.runtime': self.runtime.owner,
                                          'localcloud.storage-operation': 'another-operation'}
            return original(*args)
        with patch.object(self.ec2, 'docker', side_effect=raced):
            with self.assertRaises(Error) as caught:
                self.runtime.create_volume('snap-race')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn(name, original.volumes)
        self.assertFalse(any(event[0] == 'run' or event[:2] == ('volume', 'rm') for event in original.events))

    def test_cleanup_failure_keeps_copy_error_and_does_not_claim_success(self):
        failure = Error('RuntimeUnavailable', 'copy timed out', 503)
        self.docker.failure = failure
        self.docker.cleanup_failure = True
        with self.assertRaises(Error) as caught:
            self.runtime.create_volume('snap-copy')
        self.assertIs(caught.exception, failure)
        self.assertTrue(caught.exception.__notes__)

    def test_source_mismatch_cleans_new_destination_but_preserves_source(self):
        name = self.runtime.volume_name('vol-source')
        self.docker.volumes[name] = {'localcloud.runtime': 'foreign'}
        with self.assertRaises(Error):
            self.runtime.create_volume('snap-copy', 'vol-source')
        self.assertEqual(self.docker.volumes, {name: {'localcloud.runtime': 'foreign'}})

    def test_failed_createvolume_does_not_commit_resource_or_idempotency_record(self):
        self.docker.failure = Error('RuntimeUnavailable', 'copy timed out', 503)
        with self.assertRaises(Error):
            self.ec2.call('CreateVolume', {'AvailabilityZone': 'us-east-1a', 'Size': 1, 'ClientToken': 'retry-me'}, 'test')
        self.assertEqual(self.ec2.call('DescribeVolumes', {}, 'test')['Volumes'], [])
        self.assertFalse(self.docker.volumes)
        self.docker.failure = None
        result = self.ec2.call('CreateVolume', {'AvailabilityZone': 'us-east-1a', 'Size': 1, 'ClientToken': 'retry-me'}, 'test')
        self.assertTrue(result['VolumeId'])

    def snapshot_fixture(self, states, error_on=None):
        events = []
        instances = [{'InstanceId': str(n), 'Backend': 'docker'} for n in range(len(states))]
        def container(instance):
            return 'guest-' + instance['InstanceId']
        def docker(*args):
            events.append(args)
            if args[0] == 'inspect':
                if args[args.index('--format') + 1] == '{{.Id}}':
                    return 'guest-id-' + args[-1]
                return json.dumps(states[int(args[-1].split('-')[-1])])
            if args[0] == error_on:
                raise Error('RuntimeUnavailable', 'fixture ' + error_on, 503)
            return ''
        return instances, events, container, docker

    def test_snapshot_only_resumes_guests_it_paused(self):
        instances, events, container, docker = self.snapshot_fixture([
            {'Running': True, 'Paused': False}, {'Running': True, 'Paused': True}, {'Running': False, 'Paused': False}])
        with patch.object(self.ec2, 'container', side_effect=container), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.runtime, 'create_volume') as copied:
            self.runtime.snapshot_volume('snap-copy', 'vol-source', instances)
        self.assertEqual([e for e in events if e[0] in ('pause', 'unpause')], [('pause', 'guest-0'), ('unpause', 'guest-0')])
        copied.assert_called_once_with('snap-copy', 'vol-source')

    def test_failed_snapshot_copy_always_resumes_guest(self):
        instances, events, container, docker = self.snapshot_fixture([{'Running': True, 'Paused': False}])
        failure = Error('RuntimeUnavailable', 'copy failed', 503)
        with patch.object(self.ec2, 'container', side_effect=container), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.runtime, 'create_volume', side_effect=failure):
            with self.assertRaises(Error) as caught:
                self.runtime.snapshot_volume('snap-copy', 'vol-source', instances)
        self.assertIs(caught.exception, failure)
        self.assertIn(('unpause', 'guest-0'), events)

    def test_resume_failure_removes_copied_snapshot_and_returns_failure(self):
        instances, events, container, docker = self.snapshot_fixture([{'Running': True, 'Paused': False}], error_on='unpause')
        with patch.object(self.ec2, 'container', side_effect=container), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.runtime, 'create_volume'), patch.object(self.runtime, 'delete_volume') as removed:
            with self.assertRaises(Error) as caught:
                self.runtime.snapshot_volume('snap-copy', 'vol-source', instances)
        self.assertIn('unpause', str(caught.exception))
        removed.assert_called_once_with('snap-copy')

    def test_copy_failure_is_not_masked_by_resume_failure(self):
        instances, events, container, docker = self.snapshot_fixture([{'Running': True, 'Paused': False}], error_on='unpause')
        failure = Error('RuntimeUnavailable', 'original copy error', 503)
        with patch.object(self.ec2, 'container', side_effect=container), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.runtime, 'create_volume', side_effect=failure):
            with self.assertRaises(Error) as caught:
                self.runtime.snapshot_volume('snap-copy', 'vol-source', instances)
        self.assertIs(caught.exception, failure)
        self.assertTrue(any('resume' in note for note in failure.__notes__))

    def test_pause_timeout_attempts_resume_without_starting_copy(self):
        instances, events, container, docker = self.snapshot_fixture([{'Running': True, 'Paused': False}], error_on='pause')
        with patch.object(self.ec2, 'container', side_effect=container), patch.object(self.ec2, 'docker', side_effect=docker), patch.object(self.runtime, 'create_volume') as copied:
            with self.assertRaises(Error):
                self.runtime.snapshot_volume('snap-copy', 'vol-source', instances)
        copied.assert_not_called()
        self.assertIn(('unpause', 'guest-0'), events)


if __name__ == '__main__':
    unittest.main()
