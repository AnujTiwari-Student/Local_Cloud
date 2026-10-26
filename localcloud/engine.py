import base64
import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit


class Error(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name):
        raise Error('InvalidParameterValue', 'Use 1–80 letters, numbers, underscores or hyphens')
    return name


def integer(value, minimum, maximum):
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise Error('InvalidParameterValue', 'Expected an integer')
    if not minimum <= result <= maximum:
        raise Error('InvalidParameterValue', f'Expected {minimum} through {maximum}')
    return result


def wildcard(value, pattern):
    expression = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return re.fullmatch(expression, value) is not None


class Engine:
    def __init__(self, path, endpoint='http://127.0.0.1:4566', iam_mode='off', ec2_images=None, ec2_kubernetes=None):
        if iam_mode not in ('off', 'soft', 'enforce'):
            raise ValueError('Invalid IAM mode')
        self.endpoint, self.iam_mode = endpoint.rstrip('/'), iam_mode
        self.lock = threading.RLock()
        self.request_context = threading.local()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL)')
        self.db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps({'version': 1, 'queues': {}, 'buckets': {}, 'policies': {}, 'audit': []}),))
        self.db.commit()
        from .tooling import Tooling
        self.tools = Tooling(self)
        from .ec2 import EC2
        self.ec2 = EC2(self, ec2_images, kubernetes=ec2_kubernetes)
        from .identity import Identity
        self.identity = Identity(self)
        from .gateway import Gateway
        self.gateway = Gateway(self)
        self.ec2.runtime.storage_journal.recover()
        if self.ec2.kubernetes is not None:
            self.ec2.kubernetes.journal.recover()

    @contextmanager
    def transaction(self):
        with self.lock:
            depth = getattr(self.request_context, 'state_transaction_depth', 0)
            self.request_context.state_transaction_depth = depth + 1
            journal = self.ec2.kubernetes.journal if self.ec2.kubernetes is not None else None
            txid = journal.begin() if journal is not None else None
            try:
                with self.db:
                    state = json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                    try:
                        yield state
                    except Error as error:
                        if error.code == 'AccessDenied':
                            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
                            self.db.commit()
                        raise
                    self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
                    if journal is not None:
                        journal.mark_committed(txid)
            finally:
                self.request_context.state_transaction_depth = depth
                # SQLite has settled before recovery inspects committed resource
                # references or writes through its independent durable journal.
                self.ec2.runtime.storage_journal.recover()
                if journal is not None:
                    journal.end(txid)
                    journal.recover()

    def close(self):
        if self.ec2.kubernetes is not None:
            self.ec2.kubernetes.journal.close()
        self.ec2.runtime.storage_journal.close()
        self.db.close()

    def authorize(self, state, principal, action, resource):
        allowed, denied = False, False
        statements = list(state['policies'].get(principal, []))
        if hasattr(self, 'identity'):
            statements.extend(self.identity.statements(state, principal))
        for statement in statements:
            if any(wildcard(action.lower(), p.lower()) for p in statement['Action']) and any(wildcard(resource, p) for p in statement['Resource']):
                allowed |= statement['Effect'] == 'Allow'
                denied |= statement['Effect'] == 'Deny'
        self.request_context.permission = {'action': action, 'decision': 'disabled' if self.iam_mode == 'off' else ('deny' if denied or not allowed else 'allow'), 'mode': self.iam_mode}
        if self.iam_mode == 'off':
            return
        if denied or not allowed:
            state['audit'].append({'time': time.time(), 'principal': principal, 'action': action, 'resource': resource, 'decision': 'deny', 'mode': self.iam_mode})
            state['audit'] = state['audit'][-200:]
            if self.iam_mode == 'enforce':
                raise Error('AccessDenied', f'{principal} cannot perform {action}', 403)

    def policy(self, principal, document):
        valid_name(principal)
        if not isinstance(document, dict) or set(document) - {'Version', 'Statement'}:
            raise Error('InvalidPolicy', 'Only Version and Statement are supported')
        statements = document.get('Statement', [])
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list):
            raise Error('InvalidPolicy', 'Statement must be a list or object')
        normalized = []
        for st in statements:
            if not isinstance(st, dict) or set(st) - {'Sid', 'Effect', 'Action', 'Resource'} or st.get('Effect') not in ('Allow', 'Deny'):
                raise Error('InvalidPolicy', 'Supported fields: Sid, Effect, Action, Resource; conditions are unsupported')
            st = copy.deepcopy(st)
            for key in ('Action', 'Resource'):
                values = st.get(key)
                if isinstance(values, str):
                    values = [values]
                if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
                    raise Error('InvalidPolicy', f'{key} must contain strings')
                st[key] = values
            normalized.append(st)
        with self.transaction() as state:
            state['policies'][principal] = normalized

    def snapshot(self, name, restore=False):
        valid_name(name)
        if self.ec2.images:
            raise Error('UnsupportedOperation', 'Snapshots are disabled in container EC2 mode', 409)
        with self.lock, self.db:
            current = json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            if current.get('identity') or current.get('ec2_iam_associations'):
                raise Error('UnsupportedOperation', 'IAM roles and instance profiles require the persistent database; portable snapshots are unsupported', 409)
            if restore:
                row = self.db.execute('SELECT body FROM snapshots WHERE name=?', (name,)).fetchone()
                if not row:
                    raise Error('NotFound', 'Snapshot does not exist', 404)
                from .ec2 import validate_instances
                try:
                    self.tools.validate_state(json.loads(row[0]))
                except ValueError as error:
                    raise Error('InvalidState', str(error))
                self.db.execute('UPDATE state SET body=? WHERE id=1', (row[0],))
            else:
                body = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]
                try:
                    self.db.execute('INSERT INTO snapshots VALUES (?, ?, ?)', (name, body, time.time()))
                except sqlite3.IntegrityError:
                    raise Error('Conflict', 'Snapshot name already exists', 409)
        return {'name': name, 'restored': restore}

    def overview(self):
        recovery_errors = {}
        if self.ec2.kubernetes is not None:
            with self.lock:
                records = json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]).get('instances', {})
                ids = [key for key, item in records.items() if item['Backend'] == 'kubernetes' and item['State']['Name'] != 'terminated']
            for key in ids:
                try:
                    with self.transaction() as data:
                        self.ec2.kubernetes.refresh(data['instances'][key])
                except Error as error:
                    recovery_errors[key] = error.code + ': ' + str(error)
        with self.lock:
            s = json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            instances = [self.ec2.public(i) for i in s.get('instances', {}).values()]
            for item in instances:
                if item['InstanceId'] in recovery_errors:
                    item['State'] = {'Name': 'pending', 'Code': 0}
                    item['RuntimeStatusReason'] = recovery_errors[item['InstanceId']]
            return {'endpoint': self.endpoint, 'iamMode': self.iam_mode,
                    'queues': [{'name': n, 'messages': len(q['messages'])} for n, q in s['queues'].items()],
                    'buckets': [{'name': n, 'objects': len(b)} for n, b in s['buckets'].items()],
                    'snapshots': [{'name': n, 'created': t} for n, t in self.db.execute('SELECT name,created FROM snapshots ORDER BY created DESC')],
                    'instances': instances,
                    'principals': list(s['policies']), 'audit': s['audit'],
                    'storageRecovery': copy.deepcopy(self.ec2.runtime.storage_journal.last_recovery),
                    **({'kubernetesRecovery': copy.deepcopy(self.ec2.kubernetes.journal.last_recovery)} if self.ec2.kubernetes is not None else {})}

    def sqs(self, operation, args, principal='test'):
        name = args.get('QueueName', '')
        if 'QueueUrl' in args:
            name = urlsplit(args['QueueUrl']).path.rstrip('/').split('/')[-1]
        resource = f'arn:aws:sqs:us-east-1:000000000000:{name}' if name else '*'
        return self.tools.call('sqs', operation, principal, resource,
                               lambda: self._sqs(operation, args, principal))

    def _sqs(self, operation, args, principal='test'):
        supported = {'CreateQueue', 'ListQueues', 'GetQueueUrl', 'GetQueueAttributes', 'SetQueueAttributes', 'DeleteQueue', 'PurgeQueue', 'SendMessage', 'ReceiveMessage', 'DeleteMessage', 'ChangeMessageVisibility'}
        if operation not in supported:
            raise Error('UnsupportedOperation', operation, 501)
        name = args.get('QueueName', '')
        if 'QueueUrl' in args:
            name = urlsplit(args['QueueUrl']).path.rstrip('/').split('/')[-1]
        resource = f'arn:aws:sqs:us-east-1:000000000000:{name}' if name else '*'
        with self.transaction() as s:
            self.authorize(s, principal, f'sqs:{operation}', resource)
            queues = s['queues']
            queue_url = f'{self.endpoint}/000000000000/{name}'
            if operation == 'ListQueues':
                return {'QueueUrls': [f'{self.endpoint}/000000000000/{n}' for n in queues if n.startswith(args.get('QueueNamePrefix', ''))]}
            if operation == 'CreateQueue':
                valid_name(name)
                if set(args) - {'QueueName', 'Attributes'} or set(args.get('Attributes', {})) - {'VisibilityTimeout'}:
                    raise Error('UnsupportedOperation', 'Only standard queues and VisibilityTimeout are supported', 501)
                visibility = integer(args.get('Attributes', {}).get('VisibilityTimeout', 30), 0, 43200)
                # Upstream SqsProvider.create_queue compares only supplied attributes.
                if name in queues and 'VisibilityTimeout' in args.get('Attributes', {}) and queues[name]['visibility'] != visibility:
                    raise Error('QueueNameExists', 'Queue has different attributes')
                queues.setdefault(name, {'visibility': visibility, 'messages': []})
                return {'QueueUrl': queue_url}
            if name not in queues:
                raise Error('QueueDoesNotExist', 'Queue does not exist', 404)
            q = queues[name]
            if operation == 'GetQueueUrl':
                return {'QueueUrl': queue_url}
            if operation == 'GetQueueAttributes':
                return {'Attributes': {'QueueArn': resource, 'VisibilityTimeout': str(q['visibility']), 'ApproximateNumberOfMessages': str(sum(m['visible'] <= time.time() for m in q['messages']))}}
            if operation == 'SetQueueAttributes':
                attrs = args.get('Attributes')
                if set(args) - {'QueueUrl', 'Attributes'} or not isinstance(attrs, dict) or set(attrs) != {'VisibilityTimeout'}:
                    raise Error('UnsupportedOperation', 'Only VisibilityTimeout can be modified', 501)
                q['visibility'] = integer(attrs['VisibilityTimeout'], 0, 43200)
                return {}
            if operation == 'DeleteQueue':
                del queues[name]
            elif operation == 'PurgeQueue':
                q['messages'] = []
            elif operation == 'SendMessage':
                if set(args) - {'QueueUrl', 'MessageBody', 'DelaySeconds'}:
                    raise Error('UnsupportedOperation', 'Message attributes and FIFO fields are unsupported', 501)
                body = args.get('MessageBody')
                if not isinstance(body, str) or not 1 <= len(body.encode()) <= 1048576:
                    raise Error('InvalidParameterValue', 'Message body must be 1–1048576 bytes')
                delay = integer(args.get('DelaySeconds', 0), 0, 900)
                mid = str(uuid.uuid4())
                digest = hashlib.md5(body.encode()).hexdigest()
                q['messages'].append({'id': mid, 'body': body, 'md5': digest, 'visible': time.time() + delay, 'receipt': None})
                return {'MessageId': mid, 'MD5OfMessageBody': digest}
            elif operation == 'ReceiveMessage':
                if int(args.get('WaitTimeSeconds', 0)) != 0:
                    raise Error('UnsupportedOperation', 'Long polling is unsupported', 501)
                count = integer(args.get('MaxNumberOfMessages', 1), 1, 10)
                visibility = integer(args.get('VisibilityTimeout', q['visibility']), 0, 43200)
                messages = []
                for m in q['messages']:
                    if m['visible'] <= time.time() and len(messages) < count:
                        m['visible'], m['receipt'] = time.time() + visibility, str(uuid.uuid4())
                        messages.append({'MessageId': m['id'], 'Body': m['body'], 'MD5OfBody': m['md5'], 'ReceiptHandle': m['receipt']})
                return {'Messages': messages}
            elif operation in ('DeleteMessage', 'ChangeMessageVisibility'):
                match = next((m for m in q['messages'] if m['receipt'] and m['receipt'] == args.get('ReceiptHandle') and m['visible'] > time.time()), None)
                if not match:
                    raise Error('ReceiptHandleIsInvalid', 'Unknown or expired receipt handle')
                if operation == 'DeleteMessage':
                    q['messages'].remove(match)
                else:
                    match['visible'] = time.time() + integer(args.get('VisibilityTimeout'), 0, 43200)
            return {}

    def s3(self, method, bucket, key, body=b'', principal='test'):
        action = {'PUT': 'PutObject' if key else 'CreateBucket', 'GET': 'GetObject' if key else ('ListBucket' if bucket else 'ListAllMyBuckets'), 'DELETE': 'DeleteObject' if key else 'DeleteBucket'}.get(method, method)
        resource = f'arn:aws:s3:::{bucket}' + (f'/{key}' if key else '') if bucket else '*'
        return self.tools.call('s3', action, principal, resource,
                               lambda: self._s3(method, bucket, key, body, principal))

    def _s3(self, method, bucket, key, body=b'', principal='test'):
        action = {'PUT': 'PutObject' if key else 'CreateBucket', 'GET': 'GetObject' if key else ('ListBucket' if bucket else 'ListAllMyBuckets'), 'DELETE': 'DeleteObject' if key else 'DeleteBucket'}.get(method)
        if not action:
            raise Error('UnsupportedOperation', method, 501)
        resource = f'arn:aws:s3:::{bucket}' + (f'/{key}' if key else '') if bucket else '*'
        with self.transaction() as s:
            self.authorize(s, principal, f's3:{action}', resource)
            buckets = s['buckets']
            if not bucket:
                if method != 'GET':
                    raise Error('InvalidRequest', 'Bucket required')
                return {'buckets': list(buckets)}
            if method == 'PUT' and not key:
                if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
                    raise Error('InvalidBucketName', 'Bucket name must be 3–63 lowercase characters')
                buckets.setdefault(bucket, {})
                return {}
            if bucket not in buckets:
                raise Error('NoSuchBucket', bucket, 404)
            objects = buckets[bucket]
            if not key:
                if method == 'GET':
                    return {'objects': [{'key': k, 'size': len(base64.b64decode(v))} for k, v in objects.items()]}
                if objects:
                    raise Error('BucketNotEmpty', bucket, 409)
                del buckets[bucket]
                return {}
            if method == 'PUT':
                objects[key] = base64.b64encode(body).decode()
                return {'etag': hashlib.md5(body).hexdigest()}
            if method == 'DELETE':
                objects.pop(key, None)
                return {}
            if key not in objects:
                raise Error('NoSuchKey', key, 404)
            return base64.b64decode(objects[key])
