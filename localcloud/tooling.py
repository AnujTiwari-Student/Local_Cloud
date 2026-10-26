"""Independent state portability, request inspection and resilience tooling."""
import base64
import copy
import hashlib
import json
import math
import random
import re
import time
import uuid
from .engine import Error, integer, valid_name


class Tooling:
    def __init__(self, engine):
        self.engine = engine
        self.random = random.random
        self.sleep = time.sleep
        with engine.lock, engine.db:
            engine.db.execute('CREATE TABLE IF NOT EXISTS traces (sequence INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
            engine.db.execute('CREATE TABLE IF NOT EXISTS faults (name TEXT PRIMARY KEY, body TEXT NOT NULL)')

    def traces(self, principal=None):
        with self.engine.lock:
            events = [json.loads(row[0]) for row in self.engine.db.execute('SELECT body FROM traces ORDER BY sequence DESC')]
        return [e for e in events if principal is None or e['principal'] == principal]

    def clear_traces(self):
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('DELETE FROM traces')

    def rules(self):
        with self.engine.lock:
            return [json.loads(row[0]) for row in self.engine.db.execute('SELECT body FROM faults ORDER BY name')]

    def set_rule(self, document):
        if not isinstance(document, dict) or set(document) - {'name', 'service', 'operation', 'probability', 'latency_ms', 'error'}:
            raise Error('InvalidFault', 'Unknown fault rule fields')
        rule = copy.deepcopy(document)
        valid_name(rule.get('name'))
        if rule.get('service') not in ('s3', 'sqs', 'ec2', '*'):
            raise Error('InvalidFault', 'Service must be s3, sqs, ec2 or *')
        operation = rule.get('operation', '*')
        if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z*?]{1,80}', operation):
            raise Error('InvalidFault', 'Invalid operation pattern')
        probability = rule.get('probability', 1)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise Error('InvalidFault', 'Probability must be a number from 0 through 1')
        latency = rule.get('latency_ms', 0)
        if type(latency) is not int or not 0 <= latency <= 5000:
            raise Error('InvalidFault', 'Latency must be an integer from 0 through 5000 ms')
        error = rule.get('error')
        if error is not None:
            if not isinstance(error, dict) or set(error) - {'code', 'status'} or not isinstance(error.get('code'), str) or not re.fullmatch('[A-Za-z][A-Za-z0-9]{0,79}', error['code']):
                raise Error('InvalidFault', 'Error must contain a simple code and optional status')
            if type(error.get('status', 503)) is not int or not 400 <= error.get('status', 503) <= 599:
                raise Error('InvalidFault', 'Error status must be 400 through 599')
        if not latency and error is None:
            raise Error('InvalidFault', 'Specify latency_ms or error')
        rule.update(operation=operation, probability=probability, latency_ms=latency)
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('INSERT OR REPLACE INTO faults VALUES (?,?)', (rule['name'], json.dumps(rule)))
        return rule

    def delete_rule(self, name):
        valid_name(name)
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('DELETE FROM faults WHERE name=?', (name,))

    def before(self, service, operation):
        from .engine import wildcard
        for rule in self.rules():
            if rule['service'] not in (service, '*') or not wildcard(operation, rule['operation']):
                continue
            if self.random() >= rule['probability']:
                continue
            # Never hold the engine lock while sleeping: management remains responsive.
            self.sleep(rule['latency_ms'] / 1000)
            if rule.get('error'):
                raise Error(rule['error']['code'], 'Injected fault: ' + rule['name'], rule['error'].get('status', 503))
            break  # First matching, triggered rule wins; latency does not accumulate.

    def call(self, service, operation, principal, resource, callback):
        engine = self.engine
        started = time.monotonic()
        event = {'id': str(uuid.uuid4()), 'time': time.time(), 'service': service,
                 'operation': operation, 'principal': principal, 'resource': resource,
                 'permission': None, 'status': 200, 'error': None}
        context = getattr(engine.request_context, 'gateway_context', None)
        if context is not None:
            event.update(request_id=context.request_id, protocol=context.protocol, region=context.region, account_id=context.account_id)
        engine.request_context.permission = None
        try:
            self.before(service, operation)
            return callback()
        except Error as error:
            event['status'], event['error'] = error.status, error.code
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            event['status'], event['error'] = 400, 'InvalidRequest'
            raise
        except Exception:
            event['status'], event['error'] = 500, 'InternalError'
            raise
        finally:
            event['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
            event['permission'] = engine.request_context.permission
            with engine.lock, engine.db:
                engine.db.execute('INSERT INTO traces(body) VALUES (?)', (json.dumps(event),))
                engine.db.execute('DELETE FROM traces WHERE sequence NOT IN (SELECT sequence FROM traces ORDER BY sequence DESC LIMIT 1000)')

    def generated_policy(self, principal, source='successful'):
        valid_name(principal)
        if source not in ('successful', 'violations'):
            raise Error('InvalidParameterValue', 'Source must be successful or violations')
        pairs = set()
        for event in self.traces(principal):
            permission = event['permission']
            if not permission:
                continue  # Faults injected before authorization are not permission evidence.
            include = (event['status'] < 400) if source == 'successful' else permission['decision'] == 'deny'
            if include:
                pairs.add((permission['action'], event['resource']))
        return {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': action, 'Resource': resource}
            for action, resource in sorted(pairs)
        ]}

    def export_state(self):
        with self.engine.lock:
            state = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        if state.get('identity') or state.get('ec2_iam_associations'):
            raise Error('UnsupportedOperation', 'IAM roles and profiles are not portable; preserve the backend SQLite database', 409)
        if any(i.get('Backend') in ('docker', 'kubernetes') for i in state.get('instances', {}).values()):
            raise Error('UnsupportedOperation', 'Container EC2 instances cannot be exported as portable state', 409)
        body = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(body.encode()) > 6 * 1024 * 1024:
            raise Error('StateTooLarge', 'Portable state is limited to 6 MiB; local snapshots remain available', 413)
        return {'format': 'localcloud-state', 'version': 1,
                'sha256': hashlib.sha256(body.encode()).hexdigest(), 'state': state}

    def import_state(self, bundle):
        with self.engine.lock:
            current = json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        if current.get('identity') or current.get('ec2_iam_associations'):
            raise Error('UnsupportedOperation', 'State import cannot replace a backend containing IAM roles or profiles', 409)
        if self.engine.ec2.images:
            raise Error('UnsupportedOperation', 'State import is disabled in container EC2 mode', 409)
        # Validate everything before opening the replacement transaction.
        if not isinstance(bundle, dict) or set(bundle) != {'format', 'version', 'sha256', 'state'} or bundle['format'] != 'localcloud-state' or type(bundle['version']) is not int or bundle['version'] != 1:
            raise Error('InvalidState', 'Unsupported state bundle format or version')
        try:
            body = json.dumps(bundle['state'], sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(body.encode()) > 6 * 1024 * 1024:
                raise ValueError('State exceeds the 6 MiB import limit')
            if hashlib.sha256(body.encode()).hexdigest() != bundle['sha256']:
                raise ValueError('State checksum mismatch')
            self.validate_state(bundle['state'])
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, Error) as error:
            raise Error('InvalidState', str(error))
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('UPDATE state SET body=? WHERE id=1', (body,))
        return {'imported': True}

    @staticmethod
    def validate_state(state):
        if not isinstance(state, dict) or set(state) - {'version', 'queues', 'buckets', 'policies', 'audit', 'instances', 'ec2'} or not {'version', 'queues', 'buckets', 'policies', 'audit'} <= set(state) or type(state['version']) is not int or state['version'] != 1:
            raise ValueError('Invalid state schema')
        from .ec2 import validate_instances
        validate_instances(state.get('instances', {}))
        if 'ec2' in state:
            from .ec2_infrastructure import validate_infrastructure
            validate_infrastructure(state['ec2'])
        if any(not isinstance(state[k], dict) for k in ('queues', 'buckets', 'policies')):
            raise ValueError('Resource collections must be objects')
        if not isinstance(state['audit'], list) or len(state['audit']) > 200:
            raise ValueError('Invalid audit list')
        for record in state['audit']:
            if not isinstance(record, dict) or set(record) != {'time', 'principal', 'action', 'resource', 'decision', 'mode'}:
                raise ValueError('Invalid audit record')
            if not isinstance(record['time'], (int, float)) or not math.isfinite(record['time']) or any(not isinstance(record[k], str) for k in ('principal', 'action', 'resource', 'decision', 'mode')):
                raise ValueError('Invalid audit record values')
        for name, queue in state['queues'].items():
            valid_name(name)
            if not isinstance(queue, dict) or set(queue) != {'visibility', 'messages'} or type(queue['visibility']) is not int:
                raise ValueError('Invalid queue schema')
            integer(queue['visibility'], 0, 43200)
            if not isinstance(queue['messages'], list):
                raise ValueError('Invalid message collection')
            ids = set()
            for message in queue['messages']:
                if not isinstance(message, dict) or set(message) != {'id', 'body', 'md5', 'visible', 'receipt'}:
                    raise ValueError('Invalid message schema')
                if any(not isinstance(message[k], str) for k in ('id', 'body', 'md5')) or not message['id'] or message['id'] in ids:
                    raise ValueError('Invalid or duplicate message identifier')
                ids.add(message['id'])
                if not 1 <= len(message['body'].encode()) <= 1048576 or hashlib.md5(message['body'].encode()).hexdigest() != message['md5']:
                    raise ValueError('Invalid message body/digest')
                if type(message['visible']) not in (int, float) or not math.isfinite(message['visible']) or message['visible'] < 0 or (message['receipt'] is not None and not isinstance(message['receipt'], str)):
                    raise ValueError('Invalid message visibility/receipt')
        for name, objects in state['buckets'].items():
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', name) or not isinstance(objects, dict):
                raise ValueError('Invalid bucket')
            for key, value in objects.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError('Invalid object')
                base64.b64decode(value, validate=True)
        # Reuse the policy parser on an isolated in-memory engine, never the live database.
        from .engine import Engine
        validator = Engine(':memory:')
        try:
            for principal, statements in state['policies'].items():
                if not isinstance(statements, list) or any(not isinstance(st, dict) or not isinstance(st.get('Action'), list) or not isinstance(st.get('Resource'), list) for st in statements):
                    raise ValueError('Imported policies must use normalized statement arrays')
                validator.policy(principal, {'Statement': statements})
        finally:
            validator.close()
