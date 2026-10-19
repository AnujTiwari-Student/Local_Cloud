"""Exercise real HTTP parsing and serialization without requiring socket permissions."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from localcloud.__main__ import Handler
from localcloud.engine import Engine


class Connection:
    def __init__(self, raw):
        self.raw = raw
        self.output = bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.output.extend(data)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'state.db'))
        self.server = SimpleNamespace(engine=self.engine)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def request(self, path='/', data=b'', method='GET', headers=None):
        fields = {'Host': '127.0.0.1:4566', 'Content-Length': str(len(data)), **(headers or {})}
        raw = (f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n').encode() + data
        connection = Connection(raw)
        Handler(connection, ('127.0.0.1', 12345), self.server)
        head, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        return int(head.split(b' ')[1]), body

    def test_sqs_json_protocol_and_dashboard(self):
        status, body = self.request('/_local')
        self.assertEqual(status, 200)
        self.assertIn(b'Resource Browser', body)
        status, body = self.request(data=b'{"QueueName":"jobs"}', method='POST', headers={'X-Amz-Target': 'AmazonSQS.CreateQueue', 'Content-Type': 'application/x-amz-json-1.0'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['QueueUrl'], 'http://127.0.0.1:4566/000000000000/jobs')
        status, body = self.request('/_local/resources')
        self.assertEqual(json.loads(body)['queues'][0]['name'], 'jobs')

    def test_s3_binary_and_xml(self):
        self.assertEqual(self.request('/assets', b'', 'PUT')[0], 200)
        self.assertEqual(self.request('/assets/key', b'\x00\xff', 'PUT')[0], 200)
        self.assertEqual(self.request('/assets/key'), (200, b'\x00\xff'))
        self.assertIn(b'<Key>key</Key>', self.request('/assets?list-type=2')[1])
        status, body = self.request('/assets/missing')
        self.assertEqual(status, 404)
        self.assertIn(b'NoSuchKey', body)

    def test_management_content_type_and_snapshot_restore(self):
        self.assertEqual(self.request('/_local/snapshots', b'{"name":"test"}', 'POST')[0], 415)
        headers = {'Content-Type': 'application/json'}
        self.assertEqual(self.request('/_local/snapshots', b'{"name":"test"}', 'POST', headers)[0], 201)
        self.request('/assets', b'', 'PUT')
        self.assertEqual(self.request('/_local/snapshots/restore', b'{"name":"test"}', 'POST', headers)[0], 200)
        self.assertEqual(self.engine.overview()['buckets'], [])

    def test_malformed_json_and_unsupported_service(self):
        for body, target, status in [(b'{', 'AmazonSQS.CreateQueue', 400), (b'{}', 'OtherService.Create', 501)]:
            self.assertEqual(self.request(data=body, method='POST', headers={'X-Amz-Target': target})[0], status)

    def test_enforced_identity_over_http(self):
        self.engine.iam_mode = 'enforce'
        self.assertEqual(self.request('/assets', method='PUT')[0], 403)
        self.assertEqual(self.engine.overview()['audit'][0]['mode'], 'enforce')
        self.engine.policy('alice', {'Statement': {'Effect': 'Allow', 'Action': 's3:*', 'Resource': '*'}})
        self.assertEqual(self.request('/assets', method='PUT', headers={'X-Local-Principal': 'alice'})[0], 200)
        self.assertEqual(self.request('/assets', method='GET')[0], 403)

    def test_portability_trace_and_policy_routes(self):
        self.request('/assets', b'', 'PUT')
        status, body = self.request('/_local/state')
        self.assertEqual(status, 200)
        bundle = json.loads(body)
        self.request('/assets', method='DELETE')
        headers = {'Content-Type': 'application/json'}
        status, _ = self.request('/_local/state/import', json.dumps(bundle).encode(), 'POST', headers)
        self.assertEqual(status, 200)
        status, body = self.request('/_local/policy-stream?principal=test&source=successful')
        self.assertEqual(status, 200)
        self.assertGreater(len(json.loads(body)['Statement']), 0)
        status, body = self.request('/_local/traces?principal=test')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)), 2)
        self.assertEqual(self.request('/_local/traces/clear', b'{}', 'POST', headers)[0], 200)
        self.assertEqual(json.loads(self.request('/_local/traces')[1]), [])
        self.assertEqual(self.request('/_local/policy-stream?source=invalid')[0], 400)

    def test_fault_management_stays_available_during_outage(self):
        headers = {'Content-Type': 'application/json'}
        rule = {'name': 'outage', 'service': '*', 'error': {'code': 'Unavailable', 'status': 503}}
        self.assertEqual(self.request('/_local/faults', json.dumps(rule).encode(), 'POST', headers)[0], 200)
        self.assertEqual(self.request('/assets', method='PUT')[0], 503)
        self.assertEqual(self.request('/_local/health')[0], 200)
        self.assertEqual(len(json.loads(self.request('/_local/faults')[1])), 1)
        self.assertEqual(self.request('/_local/faults/delete', b'{"name":"outage"}', 'POST', headers)[0], 200)
        self.assertEqual(self.request('/assets', method='PUT')[0], 200)


    def test_management_token_protects_data_but_allows_health_and_dashboard(self):
        self.server.management_token = 'example-token'
        self.assertEqual(self.request('/_local')[0], 200)
        self.assertEqual(self.request('/_local/health')[0], 200)
        self.assertEqual(self.request('/_local/resources')[0], 401)
        self.assertEqual(self.request('/_local/resources', headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request('/_local/resources', headers={'Authorization': 'Bearer example-token'})[0], 200)
        self.assertEqual(self.request('/_local/executions')[0], 401)

    def test_executor_routes(self):
        from unittest.mock import Mock
        executor = Mock()
        self.server.executor = executor
        executor.submit.return_value = {'name': 'job-1', 'phase': 'Pending'}
        executor.list.return_value = [{'name': 'job-1'}]
        executor.status.return_value = {'phase': 'Complete'}
        executor.logs.return_value = {'logs': 'hello'}
        executor.cancel.return_value = {'deleted': True}
        self.assertEqual(self.request('/_local/executions', b'{}', 'POST')[0], 415)
        self.assertEqual(self.request('/_local/executions', b'{}', 'POST', {'Content-Type': 'application/json'})[0], 201)
        self.assertEqual(json.loads(self.request('/_local/executions')[1])[0]['name'], 'job-1')
        self.assertEqual(json.loads(self.request('/_local/executions/job-1')[1])['phase'], 'Complete')
        self.assertEqual(json.loads(self.request('/_local/executions/job-1/logs')[1])['logs'], 'hello')
        self.assertEqual(json.loads(self.request('/_local/executions/job-1', method='DELETE')[1])['deleted'], True)
        del self.server.executor
        self.assertEqual(self.request('/_local/executions')[0], 503)

    def test_ci_smoke_exercises_http_handler_without_sockets(self):
        from localcloud.ci import smoke
        test = self
        class Client:
            def request(self, method, path, data=None, target=None):
                headers = {}
                if target:
                    headers['X-Amz-Target'] = 'AmazonSQS.' + target
                if isinstance(data, dict):
                    data = json.dumps(data).encode()
                    headers['Content-Type'] = 'application/json'
                status, body = test.request(path, data or b'', method, headers)
                if status >= 400:
                    raise RuntimeError(body)
                return json.loads(body) if target or path.startswith('/_local') else body
        self.assertEqual(len(smoke(Client())), 5)
        self.assertEqual(self.engine.overview()['buckets'], [])
        self.assertEqual(self.engine.overview()['queues'], [])
