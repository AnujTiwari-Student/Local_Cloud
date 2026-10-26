"""Durable, ownership-bound intents for Kubernetes effects outside SQLite.

The main state transaction supplies its commit marker. Creates/updates happen
before that commit and can be compensated; deletion is deliberately deferred
until it commits. This is independent recovery code, not a distributed commit
protocol or a copy of a product provider.
"""
import copy
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
import weakref

from .engine import Error
from .ec2_storage_journal import process_identity

ANNOTATION = 'localcloud.dev/kube-operation'
_SESSIONS = weakref.WeakValueDictionary()
_SESSION_LOCK = threading.RLock()
_MISSING = object()
_IGNORED_METADATA = {'uid', 'resourceVersion', 'generation', 'creationTimestamp', 'managedFields', 'selfLink'}


def _alive(creator, journal):
    pid = creator.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        with _SESSION_LOCK:
            existing = _SESSIONS.get(creator.get('session'))
        return existing is not None and existing is not journal
    actual, expected = process_identity(pid), creator.get('identity')
    if actual is not None and expected is not None:
        return actual == expected
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _changes(before, desired, path=()):
    """Only revert fields changed by us, preserving unrelated concurrent fields."""
    if isinstance(before, dict) and isinstance(desired, dict):
        result = []
        for key in sorted(set(before) | set(desired)):
            if not path and key == 'status' or path == ('metadata',) and key in _IGNORED_METADATA:
                continue
            result.extend(_changes(before.get(key, _MISSING), desired.get(key, _MISSING), path + (key,)))
        return result
    if before is not _MISSING and desired is not _MISSING and before == desired:
        return []
    return [{'path': list(path), 'old_exists': before is not _MISSING,
             'new_exists': desired is not _MISSING,
             **({'old': copy.deepcopy(before)} if before is not _MISSING else {}),
             **({'new': copy.deepcopy(desired)} if desired is not _MISSING else {})}]


def _lookup(body, path):
    for key in path:
        if not isinstance(body, dict) or key not in body:
            return _MISSING
        body = body[key]
    return body


def _same(value, exists, expected):
    return value is _MISSING if not exists else value is not _MISSING and value == expected


def _restore(current, changes):
    restored = copy.deepcopy(current)
    for change in changes:
        parent = restored
        for key in change['path'][:-1]:
            parent = parent.setdefault(key, {})
        key = change['path'][-1]
        if change['old_exists']:
            parent[key] = copy.deepcopy(change['old'])
        else:
            parent.pop(key, None)
    restored.get('metadata', {}).pop('managedFields', None)
    return restored


class KubernetesJournal:
    def __init__(self, adapter):
        self.adapter = adapter
        self.engine = adapter.ec2.engine
        self.owner, self.namespace = adapter.owner, adapter.namespace
        self.server = str(getattr(adapter.client, 'server', 'in-process-test-client'))
        self.local = threading.local()
        self.lock = threading.RLock()
        self.session = uuid.uuid4().hex
        with _SESSION_LOCK:
            _SESSIONS[self.session] = self
        filename = self.engine.db.execute('PRAGMA database_list').fetchone()[2]
        self.durable = bool(filename)
        self.path = filename + '.ec2-kube-journal.sqlite3' if filename else ':memory:'
        if self.durable:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (operation TEXT PRIMARY KEY, txid TEXT NOT NULL, owner TEXT NOT NULL, body TEXT NOT NULL)')
        self.db.commit()
        self.engine.db.execute('CREATE TABLE IF NOT EXISTS ec2_kube_commits (txid TEXT PRIMARY KEY)')
        self.engine.db.commit()
        self.last_recovery = {'pending': 0, 'recovered': 0, 'errors': [], 'waiting': [], 'skipped_active': 0, 'durable': self.durable}

    @property
    def current(self):
        return getattr(self.local, 'txid', None)

    def begin(self):
        if self.current is None:
            self.local.txid, self.local.depth = uuid.uuid4().hex, 1
        else:
            self.local.depth += 1
        return self.current

    def mark_committed(self, txid):
        # The parent must call this inside the same main SQLite transaction as
        # its resource JSON update. It intentionally does not commit here.
        if getattr(self.local, 'depth', 0) == 1:
            with self.lock:
                exists = self.db.execute('SELECT 1 FROM operations WHERE txid=? AND owner=? LIMIT 1', (txid, self.owner)).fetchone()
            if exists:
                self.engine.db.execute('INSERT OR IGNORE INTO ec2_kube_commits VALUES (?)', (txid,))

    def end(self, txid):
        if self.current != txid:
            return
        self.local.depth -= 1
        if self.local.depth == 0:
            self.local.txid = None

    def jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM operations WHERE owner=? ORDER BY rowid', (self.owner,))]

    def pending_for(self, item=None, uid=None):
        name = item.get('InstanceId') if isinstance(item, dict) else item
        return [job for job in self.jobs() if (uid and job.get('uid') == uid) or
                (name and job.get('labels', {}).get('localcloud.dev/ec2-instance') == name)]

    def _save(self, job):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO operations VALUES (?, ?, ?, ?)',
                            (job['operation'], job['txid'], self.owner, json.dumps(job)))

    def _forget(self, job):
        with self.lock, self.db:
            self.db.execute('DELETE FROM operations WHERE operation=? AND owner=?', (job['operation'], self.owner))

    def _record(self, kind, path, labels, before=None, desired=None):
        job = {'operation': uuid.uuid4().hex, 'txid': self.current, 'owner': self.owner,
               'namespace': self.namespace, 'server': self.server, 'kind': kind,
               'path': path, 'labels': copy.deepcopy(labels),
               'creator': {'pid': os.getpid(), 'identity': process_identity(os.getpid()), 'session': self.session}}
        if before is not None:
            job['uid'] = before['metadata']['uid']
        if desired is not None:
            desired = copy.deepcopy(desired)
            desired.setdefault('metadata', {}).setdefault('annotations', {})[ANNOTATION] = job['operation']
            job['desired'] = desired
        if kind == 'update':
            job['changes'] = _changes(before, desired)
        self._save(job)
        return job

    def create(self, collection, body, labels):
        if self.current is None:
            return self.adapter.client.request('POST', collection, body)
        job = self._record('create', collection + '/' + body['metadata']['name'], labels, desired=body)
        try:
            result = self.adapter.client.request('POST', collection, job['desired'])
        except Error as error:
            if error.status == 409:
                self._forget(job)
            raise
        job['uid'] = result['metadata']['uid']
        self._save(job)
        return result

    def update(self, path, before, desired, labels):
        if self.current is None:
            return self.adapter.client.request('PUT', path, desired)
        job = self._record('update', path, labels, before, desired)
        try:
            result = self.adapter.client.request('PUT', path, job['desired'])
        except Error as error:
            if error.status == 409:
                self._forget(job)
            raise
        return result

    def delete(self, path, owned_object, labels):
        if self.current is None:
            return self.adapter.client.request('DELETE', path, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                        'propagationPolicy': 'Foreground', 'preconditions': {'uid': owned_object['metadata']['uid']}})
        self._record('delete', path, labels, before=owned_object)
        return {'status': 'Success', 'reason': 'DeletionDeferredUntilStateCommit'}

    def _get(self, path):
        try:
            return self.adapter.client.get(path)
        except Error as error:
            if error.status == 404:
                return None
            raise

    def _owned(self, job, obj, nonce=False):
        metadata = obj.get('metadata', {})
        if any(metadata.get('labels', {}).get(k) != v for k, v in job['labels'].items()):
            raise Error('RuntimeFailure', 'Kubernetes journal resource ownership changed', 409)
        if job.get('uid') and metadata.get('uid') != job['uid']:
            raise Error('RuntimeFailure', 'Kubernetes journal resource identity changed', 409)
        if nonce and metadata.get('annotations', {}).get(ANNOTATION) != job['operation']:
            raise Error('RuntimeFailure', 'Kubernetes journal operation identity changed', 409)

    def _delete_owned(self, job, obj):
        self._owned(job, obj, nonce=job['kind'] == 'create')
        if not obj.get('metadata', {}).get('deletionTimestamp'):
            self.adapter.client.request('DELETE', job['path'], {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                        'propagationPolicy': 'Foreground', 'preconditions': {'uid': obj['metadata']['uid']}})
        return self._get(job['path']) is None

    def recover(self):
        try:
            return self._recover()
        except Exception as error:
            try:
                pending = len(self.jobs())
            except Exception:
                pending = None
            self.last_recovery = {'pending': pending, 'recovered': 0, 'errors': [{'problems': [str(error)]}],
                                  'waiting': [], 'skipped_active': 0, 'durable': self.durable}
            return self.last_recovery

    def _recover(self):
        if self.engine.db.in_transaction:
            return self.last_recovery
        jobs = self.jobs()
        commits = {row[0] for row in self.engine.db.execute('SELECT txid FROM ec2_kube_commits')}
        report = {'pending': len(jobs), 'recovered': 0, 'errors': [], 'waiting': [], 'skipped_active': 0, 'durable': self.durable}
        # Undo effects in reverse order. Committed deletions keep request order.
        ordered = [j for j in reversed(jobs) if j['txid'] not in commits] + [j for j in jobs if j['txid'] in commits]
        for job in ordered:
            try:
                if job['namespace'] != self.namespace or job['server'] != self.server:
                    raise Error('RuntimeFailure', 'Kubernetes journal belongs to a different API endpoint or namespace', 409)
                if job['txid'] == self.current or _alive(job.get('creator', {}), self):
                    report['skipped_active'] += 1
                    continue
                committed = job['txid'] in commits
                done = True
                if not committed and job['kind'] == 'delete' or committed and job['kind'] != 'delete':
                    pass
                else:
                    obj = self._get(job['path'])
                    if obj is None:
                        pass
                    elif job['kind'] in ('create', 'delete'):
                        done = self._delete_owned(job, obj)
                    else:
                        self._owned(job, obj)
                        changes = job['changes']
                        matches_new = all(_same(_lookup(obj, c['path']), c['new_exists'], c.get('new')) for c in changes)
                        matches_old = all(_same(_lookup(obj, c['path']), c['old_exists'], c.get('old')) for c in changes)
                        if matches_new:
                            self.adapter.client.request('PUT', job['path'], _restore(obj, changes))
                        elif not matches_old:
                            raise Error('RuntimeFailure', 'Kubernetes journal update has concurrent changes; rollback withheld', 409)
                if done:
                    self._forget(job)
                    report['recovered'] += 1
                else:
                    report['waiting'].append(job['operation'])
            except Exception as error:
                report['errors'].append({'operation': job['operation'], 'path': job['path'], 'problems': [str(error)]})
        report['pending'] -= report['recovered']
        # A marker has no purpose once all its effects have settled. This does
        # not create entries for ordinary read-only API requests.
        remaining = {job['txid'] for job in self.jobs()}
        stale = commits - remaining
        if stale:
            with self.engine.db:
                self.engine.db.executemany('DELETE FROM ec2_kube_commits WHERE txid=?', [(txid,) for txid in stale])
        self.last_recovery = report
        return report

    def close(self):
        with _SESSION_LOCK:
            _SESSIONS.pop(self.session, None)
        self.db.close()
