"""Durable intent log for owned Docker storage effects outside SQLite state.

Only journaled names are reconciled. This is recovery of file-copy operations,
not a raw-block transaction or an application-consistent snapshot protocol.
"""
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
import weakref

from .engine import Error


_LIVE_SESSIONS = weakref.WeakValueDictionary()
_SESSION_LOCK = threading.RLock()


def process_identity(pid):
    """Linux boot + process start ticks distinguish a live PID from reuse."""
    try:
        stat = Path('/proc/' + str(pid) + '/stat').read_text()
        start_ticks = stat[stat.rfind(')') + 2:].split()[19]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot + ':' + start_ticks
    except (OSError, ValueError, IndexError):
        return None


def creator_alive(job, session):
    creator = job.get('creator', {})
    pid = creator.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        with _SESSION_LOCK:
            other = _LIVE_SESSIONS.get(creator.get('session'))
        return other is not None and other is not session
    expected = creator.get('identity')
    actual = process_identity(pid)
    if expected is not None and actual is not None:
        return expected == actual
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True



class StorageJournal:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.session = uuid.uuid4().hex
        with _SESSION_LOCK:
            _LIVE_SESSIONS[self.session] = self
        path = runtime.ec2.engine.db.execute('PRAGMA database_list').fetchone()[2]
        self.durable = bool(path)
        self.db = sqlite3.connect(path or ':memory:', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS ec2_storage_operations (operation TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL)')
        self.db.commit()
        self.last_recovery = {'pending': 0, 'recovered': 0, 'errors': [], 'durable': self.durable}

    def begin(self, key, *, parent=None, kind='copy'):
        operation = uuid.uuid4().hex
        job = {'operation': operation, 'key': key, 'owner': self.runtime.owner, 'kind': kind, 'parent': parent,
               'helper': 'localcloud-storage-' + self.runtime.owner[:12] + '-' + operation,
               'guests': [], 'creator': {'pid': os.getpid(), 'identity': process_identity(os.getpid()),
                                            'session': self.session}}
        self.save(job)
        return job

    def save(self, job):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO ec2_storage_operations VALUES (?, ?, ?)',
                            (job['operation'], self.runtime.owner, json.dumps(job)))

    def guest(self, job, instance, name):
        entry = {'name': name, 'instance': instance['InstanceId'], 'owner': instance.get('Owner'),
                 'container_id': instance.get('ContainerId'), 'prior_paused': False, 'resumed': False}
        job['guests'].append(entry)
        self.save(job)
        return entry

    def resumed(self, job, entry):
        entry['resumed'] = True
        self.save(job)

    def jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT body FROM ec2_storage_operations WHERE owner=? ORDER BY rowid', (self.runtime.owner,))]

    def present(self, kind, name):
        field = '{{.Names}}' if kind == 'container' else '{{.Name}}'
        args = [kind, 'ls']
        if kind == 'container':
            args.append('-a')
        output = self.runtime.ec2.docker(*args, '--filter', 'name=' + name, '--format', field)
        return name in output.splitlines()

    def matches(self, kind, name, job):
        value = '{{json .Labels}}' if kind == 'volume' else '{{json .Config.Labels}}'
        labels = json.loads(self.runtime.ec2.docker(kind, 'inspect', '--format', value, name))
        return (isinstance(labels, dict) and labels.get('localcloud.runtime') == self.runtime.owner
                and labels.get('localcloud.storage-operation') == job['operation'])

    def recover(self):
        try:
            return self._recover()
        except Exception as error:
            # Recovery diagnostics must not replace a failed API's original
            # error or turn an already committed request into a false failure.
            try:
                pending = len(self.jobs())
            except Exception:
                pending = None
            self.last_recovery = {'pending': pending, 'recovered': 0,
                                  'errors': [{'problems': [str(error)]}], 'durable': self.durable}
            return self.last_recovery

    def _recover(self):
        """Run only after the engine's state transaction has committed/rolled back."""
        engine = self.runtime.ec2.engine
        if engine.db.in_transaction:
            return self.last_recovery
        jobs = self.jobs()
        report = {'pending': len(jobs), 'recovered': 0, 'skipped_active': 0, 'errors': [], 'durable': self.durable}
        if not jobs:
            self.last_recovery = report
            return report
        state = json.loads(engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        resources = state.get('ec2', {})
        referenced = set(resources.get('volumes', {})) | set(resources.get('snapshots', {}))
        # Child copies must settle before a group pause can be released.
        for job in sorted(jobs, key=lambda item: item.get('kind') == 'quiesce'):
            if creator_alive(job, self):
                report['skipped_active'] += 1
                continue
            problems = []
            if job.get('kind') == 'quiesce' and any(child.get('parent') == job['operation'] for child in self.jobs()):
                report['errors'].append({'operation': job['operation'], 'problems': ['Waiting for child storage recovery before resuming batch writers']})
                continue
            # Stop the copy process before resuming writers or deleting its target.
            try:
                if self.present('container', job['helper']):
                    if not self.matches('container', job['helper'], job):
                        raise Error('RuntimeFailure', 'Journal helper ownership mismatch', 409)
                    self.runtime.ec2.docker('container', 'rm', '-f', job['helper'])
            except (Error, ValueError) as error:
                problems.append('helper: ' + str(error))
            for entry in job['guests']:
                if entry.get('prior_paused') or entry.get('resumed'):
                    continue
                try:
                    if not self.present('container', entry['name']):
                        continue
                    labels = json.loads(self.runtime.ec2.docker('container', 'inspect', '--format', '{{json .Config.Labels}}', entry['name']))
                    if not isinstance(labels, dict) or not entry['owner'] or labels.get('localcloud.owner') != entry['owner']:
                        raise Error('RuntimeFailure', 'Journal guest ownership mismatch', 409)
                    if entry.get('container_id'):
                        actual = self.runtime.ec2.docker('container', 'inspect', '--format', '{{.Id}}', entry['name'])
                        if actual != entry['container_id']:
                            raise Error('RuntimeFailure', 'Journal guest identity changed', 409)
                    status = json.loads(self.runtime.ec2.docker('container', 'inspect', '--format', '{{json .State}}', entry['name']))
                    if status.get('Running') and status.get('Paused'):
                        self.runtime.ec2.docker('unpause', entry['name'])
                    self.resumed(job, entry)
                except (Error, ValueError) as error:
                    problems.append('guest: ' + str(error))
            target = self.runtime.volume_name(job['key']) if job.get('kind') != 'quiesce' else None
            if target is not None and job['key'] not in referenced:
                try:
                    if self.present('volume', target):
                        if not self.matches('volume', target, job):
                            raise Error('RuntimeFailure', 'Journal volume ownership mismatch', 409)
                        self.runtime.ec2.docker('volume', 'rm', target)
                except (Error, ValueError) as error:
                    problems.append('volume: ' + str(error))
            if problems:
                report['errors'].append({'operation': job['operation'], 'problems': problems})
            else:
                with self.lock, self.db:
                    self.db.execute('DELETE FROM ec2_storage_operations WHERE operation=? AND owner=?', (job['operation'], self.runtime.owner))
                report['recovered'] += 1
        report['pending'] -= report['recovered']
        self.last_recovery = report
        return report

    def close(self):
        with _SESSION_LOCK:
            _LIVE_SESSIONS.pop(self.session, None)
        self.db.close()
