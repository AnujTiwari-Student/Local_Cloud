"""A batch holds one owned guest pause and journals child copy recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from localcloud.engine import Engine, Error


class GroupDaemon:
    def __init__(self):
        self.events = []
        self.fail_pause = False
        self.containers = {'localcloud-i-group': {'id': 'guest-id', 'labels': {'localcloud.owner': 'guest-owner'}, 'state': {'Running': True, 'Paused': False}}}
        self.volumes = {}

    def __call__(self, *args):
        self.events.append(args)
        if args[0] == 'inspect':
            item = self.containers[args[-1]]
            fmt = args[args.index('--format') + 1]
            return item['id'] if fmt == '{{.Id}}' else json.dumps(item['state'] if fmt == '{{json .State}}' else item['labels'])
        if args[0] in ('container', 'volume'):
            items = self.containers if args[0] == 'container' else self.volumes
            if args[1] == 'ls': return '\n'.join(items)
            if args[1] == 'rm':
                del items[args[-1]]
                return ''
            if args[1] == 'inspect':
                item = items[args[-1]]
                fmt = args[args.index('--format') + 1]
                return item['id'] if fmt == '{{.Id}}' else json.dumps(item['state'] if fmt == '{{json .State}}' else item['labels'])
        if args[0] in ('pause', 'unpause'):
            self.containers[args[-1]]['state']['Paused'] = args[0] == 'pause'
            if args[0] == 'pause' and self.fail_pause:
                raise Error('RuntimeUnavailable', 'pause timed out after effect', 503)
            return ''
        raise AssertionError(args)


class SnapshotGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'state.sqlite3')
        self.daemon = GroupDaemon()
        self.patch = patch('localcloud.ec2.EC2.docker', side_effect=self.daemon)
        self.patch.start()
        self.engine = Engine(self.path)
        self.runtime = self.engine.ec2.runtime
        self.engine.ec2.images = {'ami-fixture': 'fixture-image'}
        self.instance = {'InstanceId': 'i-group', 'Backend': 'docker', 'Owner': 'guest-owner', 'ContainerId': 'guest-id'}

    def tearDown(self):
        self.engine.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_two_copies_hold_one_pause_and_separate_child_journals(self):
        copies = []
        def copy(key, source):
            self.assertTrue(self.daemon.containers['localcloud-i-group']['state']['Paused'])
            copies.append((key, source))
        with patch.object(self.runtime, 'create_volume', side_effect=copy):
            with self.runtime.freeze_snapshot_writers([self.instance]):
                self.runtime.snapshot_volume('snap-first', 'vol-first', [self.instance])
                self.runtime.snapshot_volume('snap-second', 'vol-second', [self.instance])
                self.assertTrue(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertEqual(copies, [('snap-first', 'vol-first'), ('snap-second', 'vol-second')])
        self.assertEqual([e[0] for e in self.daemon.events if e[0] in ('pause', 'unpause')], ['pause', 'unpause'])
        jobs = self.runtime.storage_journal.jobs()
        parent = next(j for j in jobs if j['kind'] == 'quiesce')
        self.assertIsNone(parent['key'])
        self.assertTrue(all(j['parent'] == parent['operation'] for j in jobs if j['kind'] != 'quiesce'))
        self.assertEqual(len({j['operation'] for j in jobs}), 3)

    def test_caller_pause_is_preserved_during_and_after_batch(self):
        self.daemon.containers['localcloud-i-group']['state']['Paused'] = True
        with patch.object(self.runtime, 'create_volume'):
            with self.runtime.freeze_snapshot_writers([self.instance]):
                self.runtime.snapshot_volume('snap-first', 'vol-first', [self.instance])
        self.assertTrue(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertFalse(any(e[0] in ('pause', 'unpause') for e in self.daemon.events))

    def test_copy_failure_resumes_batch_writer_and_preserves_original_error(self):
        failure = Error('RuntimeFailure', 'second copy failed', 503)
        with patch.object(self.runtime, 'create_volume', side_effect=[None, failure]):
            with self.assertRaises(Error) as caught:
                with self.runtime.freeze_snapshot_writers([self.instance]):
                    self.runtime.snapshot_volume('snap-first', 'vol-first', [self.instance])
                    self.runtime.snapshot_volume('snap-second', 'vol-second', [self.instance])
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.daemon.containers['localcloud-i-group']['state']['Paused'])

    def test_pause_timeout_attempts_resume_and_never_enters_copy_body(self):
        self.daemon.fail_pause = True
        with self.assertRaises(Error):
            with self.runtime.freeze_snapshot_writers([self.instance]):
                self.fail('copy body entered after pause failed')
        self.assertFalse(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertEqual([e[0] for e in self.daemon.events if e[0] in ('pause', 'unpause')], ['pause', 'unpause'])

    def test_foreign_guest_is_never_paused(self):
        self.daemon.containers['localcloud-i-group']['labels']['localcloud.owner'] = 'foreign'
        with self.assertRaises(Error):
            with self.runtime.freeze_snapshot_writers([self.instance]): pass
        self.assertFalse(any(e[0] in ('pause', 'unpause') for e in self.daemon.events))

    def interrupted_batch(self, foreign_helper=False):
        journal = self.runtime.storage_journal
        parent = journal.begin(None, kind='quiesce')
        journal.guest(parent, self.instance, 'localcloud-i-group')
        self.daemon.containers['localcloud-i-group']['state']['Paused'] = True
        child = journal.begin('snap-child', parent=parent['operation'])
        labels = {'localcloud.runtime': self.runtime.owner, 'localcloud.storage-operation': child['operation']}
        self.daemon.containers[child['helper']] = {'id': 'helper-id', 'labels': dict(labels), 'state': {'Running': True, 'Paused': False}}
        if foreign_helper: self.daemon.containers[child['helper']]['labels']['localcloud.storage-operation'] = 'foreign'
        self.daemon.volumes[self.runtime.volume_name(child['key'])] = {'labels': dict(labels)}
        return parent, child

    def test_restart_cleans_child_before_resuming_parent_writer(self):
        parent, child = self.interrupted_batch()
        self.engine.close()
        self.daemon.events.clear()
        self.engine = Engine(self.path)
        journal = self.engine.ec2.runtime.storage_journal
        self.assertEqual(journal.jobs(), [])
        self.assertFalse(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertNotIn(child['helper'], self.daemon.containers)
        actions = [e for e in self.daemon.events if e[0] == 'unpause' or e[:2] in (('container', 'rm'), ('volume', 'rm'))]
        self.assertEqual([e[0] for e in actions], ['container', 'volume', 'unpause'])

    def test_pending_foreign_child_keeps_parent_writer_paused_until_recovery(self):
        parent, child = self.interrupted_batch(foreign_helper=True)
        self.engine.close()
        self.engine = Engine(self.path)
        journal = self.engine.ec2.runtime.storage_journal
        self.assertTrue(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertEqual(journal.last_recovery['pending'], 2)
        self.assertIn(child['helper'], self.daemon.containers)
        self.daemon.containers[child['helper']]['labels']['localcloud.storage-operation'] = child['operation']
        journal.recover()
        self.assertFalse(self.daemon.containers['localcloud-i-group']['state']['Paused'])
        self.assertEqual(journal.jobs(), [])


if __name__ == '__main__': unittest.main()
