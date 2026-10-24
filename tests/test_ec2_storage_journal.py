"""File-backed storage intents survive rollback/restart without broad cleanup."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcloud.engine import Engine, Error


class JournalDaemon:
    def __init__(self):
        self.volumes = {}
        self.containers = {}
        self.events = []
        self.offline = False

    def __call__(self, *args):
        self.events.append(args)
        if self.offline:
            raise Error('RuntimeUnavailable', 'daemon unavailable', 503)
        if args[0] in ('container', 'volume'):
            kind = args[0]
            items = self.containers if kind == 'container' else self.volumes
            if args[1] == 'ls':
                return '\n'.join(items)
            if args[1] == 'inspect':
                value = items[args[-1]]
                fmt = args[args.index('--format') + 1]
                if fmt == '{{.Id}}':
                    return value['id']
                return json.dumps(value['state'] if fmt == '{{json .State}}' else value['labels'])
            if args[1] == 'rm':
                del items[args[-1]]
                return ''
        if args[0] == 'unpause':
            self.containers[args[-1]]['state']['Paused'] = False
            return ''
        raise AssertionError(args)


class StorageJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.db')
        self.daemon = JournalDaemon()
        self.patch = patch('localcloud.ec2.EC2.docker', side_effect=self.daemon)
        self.patch.start()
        self.engine = Engine(self.path)

    def tearDown(self):
        self.engine.close()
        self.patch.stop()
        self.tmp.cleanup()

    def intent(self, key='snap-interrupted', guest=True):
        runtime = self.engine.ec2.runtime
        job = runtime.storage_journal.begin(key)
        labels = {'localcloud.runtime': runtime.owner, 'localcloud.storage-operation': job['operation']}
        self.daemon.volumes[runtime.volume_name(key)] = {'labels': dict(labels)}
        self.daemon.containers[job['helper']] = {'labels': dict(labels), 'id': 'helper-id', 'state': {'Running': True, 'Paused': False}}
        if guest:
            instance = {'InstanceId': 'i-journal', 'Owner': 'guest-owner', 'ContainerId': 'guest-id'}
            runtime.storage_journal.guest(job, instance, 'localcloud-i-journal')
            self.daemon.containers['localcloud-i-journal'] = {'labels': {'localcloud.owner': 'guest-owner'}, 'id': 'guest-id', 'state': {'Running': True, 'Paused': True}}
        return job

    def restart(self):
        self.engine.close()
        self.engine = Engine(self.path)
        return self.engine.ec2.runtime.storage_journal

    def test_restart_removes_only_journaled_partial_copy_and_resumes_owned_guest(self):
        job = self.intent()
        self.daemon.volumes['unrelated-user-volume'] = {'labels': {'localcloud.runtime': 'foreign'}}
        journal = self.restart()
        self.assertEqual(journal.last_recovery['pending'], 0)
        self.assertNotIn(job['helper'], self.daemon.containers)
        self.assertFalse(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        self.assertEqual(list(self.daemon.volumes), ['unrelated-user-volume'])
        self.assertEqual(journal.jobs(), [])

    def test_committed_resource_kept_after_crash_before_journal_finalization(self):
        job = self.intent(guest=False)
        # Simulate the main state commit succeeding immediately before a crash.
        data = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        data['ec2'] = {'volumes': {}, 'snapshots': {job['key']: {'SnapshotId': job['key']}}}
        self.engine.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(data),))
        self.engine.db.commit()
        journal = self.restart()
        self.assertIn(self.engine.ec2.runtime.volume_name(job['key']), self.daemon.volumes)
        self.assertNotIn(job['helper'], self.daemon.containers)
        self.assertEqual(journal.jobs(), [])

    def test_offline_daemon_retains_journal_until_later_recovery(self):
        job = self.intent()
        self.daemon.offline = True
        journal = self.restart()
        self.assertEqual(journal.last_recovery['pending'], 1)
        self.assertTrue(journal.last_recovery['errors'])
        self.assertTrue(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        self.daemon.offline = False
        self.assertEqual(journal.recover()['pending'], 0)
        self.assertNotIn(job['helper'], self.daemon.containers)

    def test_replaced_guest_identity_is_never_unpaused(self):
        self.intent()
        self.daemon.containers['localcloud-i-journal']['id'] = 'replacement-id'
        journal = self.restart()
        self.assertTrue(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        self.assertEqual(journal.last_recovery['pending'], 1)
        self.assertFalse(any(event[0] == 'unpause' for event in self.daemon.events))

    def test_preexisting_pause_is_preserved(self):
        job = self.intent()
        job['guests'][0]['prior_paused'] = True
        self.engine.ec2.runtime.storage_journal.save(job)
        journal = self.restart()
        self.assertTrue(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        self.assertEqual(journal.jobs(), [])

    def test_foreign_destination_is_preserved_and_unresolved_job_retained(self):
        job = self.intent(guest=False)
        target = self.engine.ec2.runtime.volume_name(job['key'])
        self.daemon.volumes[target]['labels']['localcloud.storage-operation'] = 'foreign-operation'
        journal = self.restart()
        self.assertIn(target, self.daemon.volumes)
        self.assertEqual(journal.last_recovery['pending'], 1)

    def test_rollback_hook_reconciles_using_committed_state(self):
        failure = Error('RuntimeFailure', 'request failed', 503)
        with self.assertRaises(Error) as caught:
            with self.engine.transaction() as state:
                job = self.intent()
                state['ec2'] = {'snapshots': {job['key']: {}}, 'volumes': {}}
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.engine.ec2.runtime.storage_journal.jobs(), [])
        self.assertFalse(self.daemon.volumes)
        self.assertFalse(self.daemon.containers['localcloud-i-journal']['state']['Paused'])

    def test_journal_rows_visible_independently_before_state_commit(self):
        with self.engine.transaction():
            job = self.engine.ec2.runtime.storage_journal.begin('snap-intent')
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(self.path)) as observer:
                row = observer.execute('SELECT body FROM ec2_storage_operations WHERE operation=?', (job['operation'],)).fetchone()
            self.assertEqual(json.loads(row[0])['key'], 'snap-intent')

    def test_already_resumed_guest_later_paused_by_user_is_not_resumed_again(self):
        job = self.intent()
        self.engine.ec2.runtime.storage_journal.resumed(job, job['guests'][0])
        journal = self.restart()
        self.assertEqual(journal.jobs(), [])
        self.assertTrue(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        self.assertFalse(any(event[0] == 'unpause' for event in self.daemon.events))

    def test_second_live_engine_does_not_recover_first_engines_copy(self):
        job = self.intent()
        other = Engine(self.path)
        try:
            report = other.ec2.runtime.storage_journal.last_recovery
            self.assertEqual(report['skipped_active'], 1)
            self.assertEqual(report['pending'], 1)
            self.assertIn(job['helper'], self.daemon.containers)
            self.assertTrue(self.daemon.containers['localcloud-i-journal']['state']['Paused'])
        finally:
            other.close()

    def test_live_foreign_process_is_not_recovered_and_pid_reuse_is_distinguished(self):
        job = self.intent()
        job['creator'] = {'pid': 987654321, 'identity': 'boot:original', 'session': 'foreign'}
        journal = self.engine.ec2.runtime.storage_journal
        journal.save(job)
        with patch('localcloud.ec2_storage_journal.process_identity', return_value='boot:original'):
            self.assertEqual(journal.recover()['skipped_active'], 1)
        self.assertIn(job['helper'], self.daemon.containers)
        with patch('localcloud.ec2_storage_journal.process_identity', return_value='boot:reused'):
            self.assertEqual(journal.recover()['pending'], 0)
        self.assertNotIn(job['helper'], self.daemon.containers)

    def test_recovery_diagnostics_cannot_mask_original_api_error(self):
        failure = Error('RuntimeFailure', 'original request failed', 503)
        with patch.object(self.engine.ec2.runtime.storage_journal, 'jobs', side_effect=RuntimeError('journal unavailable')):
            with self.assertRaises(Error) as caught:
                with self.engine.transaction():
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertIsNone(self.engine.ec2.runtime.storage_journal.last_recovery['pending'])

    def test_in_memory_engine_reports_nondurable_journal(self):
        memory = Engine(':memory:')
        try:
            self.assertFalse(memory.ec2.runtime.storage_journal.durable)
        finally:
            memory.close()


if __name__ == '__main__':
    unittest.main()
