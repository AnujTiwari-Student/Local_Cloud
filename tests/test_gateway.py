import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
import test_http
from localcloud.engine import Engine, Error
from localcloud.gateway import RequestContext
from localcloud.providers import OperationProvider, ProviderRegistry


class GatewayTests(unittest.TestCase):
    request = test_http.HTTPTests.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'db'))
        self.gateway = self.engine.gateway
        self.server = SimpleNamespace(engine=self.engine, management_token='private')

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def test_context_pipeline_order_and_request_trace_identity(self):
        context = RequestContext('sqs', 'CreateQueue', {'QueueName': 'jobs'}, principal='alice', protocol='aws-json')
        completed = []
        self.gateway.response_handlers.append(lambda ctx: completed.append(('response',ctx.service_response)))
        self.gateway.finalizers.append(lambda ctx: completed.append(('finalizer',ctx.service_exception)))
        result = self.gateway.handle(context)
        self.assertIn('QueueUrl', result)
        self.assertEqual(context.stages, ['validate_context', 'load_provider', 'dispatch_operation'])
        self.assertEqual(completed, [('response', result), ('finalizer',None)])
        trace = self.engine.tools.traces()[0]
        self.assertEqual(trace['request_id'], context.request_id)
        self.assertEqual(trace['principal'], 'alice')
        self.assertEqual(trace['protocol'], 'aws-json')
        self.assertIsNone(self.engine.request_context.gateway_context)

    def test_error_hooks_finalizers_and_no_stale_context(self):
        self.engine.iam_mode = 'enforce'
        completed = []
        self.gateway.exception_handlers.append(lambda ctx, error: completed.append(error.code))
        self.gateway.finalizers.append(lambda ctx: completed.append(ctx.service_exception.code))
        with self.assertRaises(Error):
            self.gateway.invoke('sqs', 'CreateQueue', {'QueueName': 'blocked'})
        self.assertEqual(completed, ['AccessDenied','AccessDenied'])
        self.assertEqual(self.engine.overview()['queues'], [])
        self.assertIsNone(self.engine.request_context.gateway_context)
        self.engine.iam_mode = 'off'
        self.engine.sqs('ListQueues', {})
        self.assertNotIn('request_id', self.engine.tools.traces()[0])

    def test_lazy_registry_concurrent_load_and_factory_failure(self):
        registry = ProviderRegistry()
        created = []
        def factory():
            provider = OperationProvider('custom', {})
            created.append(provider)
            return provider
        registry.register('custom', factory)
        self.assertEqual(registry.describe()['custom']['state'], 'available')
        with ThreadPoolExecutor(max_workers=8) as pool:
            providers = list(pool.map(lambda _: registry.require('custom'), range(32)))
        self.assertEqual(len(created), 1)
        self.assertTrue(all(p is created[0] for p in providers))
        self.assertEqual(registry.describe()['custom']['state'], 'running')
        with self.assertRaises(ValueError):
            registry.register('custom', factory)
        with self.assertRaises(Error):
            registry.require('missing')
        attempted = []
        def failing():
            attempted.append(True)
            raise RuntimeError('provider failed')
        registry.register('broken', failing)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'provider failed'):
                registry.require('broken')
        self.assertEqual(len(attempted), 1)
        self.assertEqual(registry.describe()['broken']['state'], 'error')

    def test_unknown_operation_and_unsupported_namespace_fail_explicitly(self):
        for context in [RequestContext('unknown','Call',{}),RequestContext('s3','Unknown',{}),RequestContext('s3','ListBuckets',{}, region='ap-south-1'),RequestContext('s3','ListBuckets',{}, account_id='123456789012')]:
            with self.assertRaises(Error) as caught:
                self.gateway.handle(context)
            self.assertEqual(caught.exception.status, 501)
        self.assertIsNone(self.engine.request_context.gateway_context)

    def test_context_is_thread_local(self):
        barrier = Barrier(2)
        def observe(context):
            barrier.wait(timeout=5)
            self.assertIs(self.engine.request_context.gateway_context, context)
            return context.principal
        self.gateway.providers.register('custom', lambda: OperationProvider('custom',{'Observe': observe}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda principal: self.gateway.invoke('custom','Observe',{},principal), ['alice','bob']))
        self.assertEqual(results, ['alice','bob'])

    def test_http_aws_and_dashboard_requests_share_the_registry(self):
        self.assertEqual(self.gateway.providers.describe()['sqs']['state'], 'available')
        status, _ = self.request('/', b'{"QueueName":"jobs"}', 'POST', {'X-Amz-Target':'AmazonSQS.CreateQueue'})
        self.assertEqual(status, 200)
        self.assertEqual(self.gateway.providers.describe()['sqs']['state'], 'running')
        status, payload = self.request('/_local/sqs', b'{"Action":"ListQueues"}', 'POST', {'Content-Type':'application/json','Authorization':'Bearer private'})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(payload)['QueueUrls']), 1)
        self.assertEqual(self.gateway.providers.describe()['s3']['state'], 'available')
        self.assertEqual(self.request('/assets', method='PUT')[0], 200)
        self.assertEqual(self.gateway.providers.describe()['s3']['state'], 'running')
        self.assertEqual(self.gateway.providers.describe()['ec2']['state'], 'available')

    def test_request_id_matches_header_xml_and_persisted_trace(self):
        raw = urlencode({'Action':'RunInstances','ImageId':'ami-local'}).encode()
        connection = test_http.Connection(b'POST / HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw)
        from localcloud.__main__ import Handler
        Handler(connection, ('127.0.0.1',1), self.server)
        header, body = bytes(connection.output).split(b'\r\n\r\n',1)
        request_id = next(line.split(b': ',1)[1].decode() for line in header.split(b'\r\n') if line.lower().startswith(b'x-amzn-requestid:'))
        self.assertEqual(ET.fromstring(body).findtext('{*}requestId'), request_id)
        self.assertEqual(self.engine.tools.traces()[0]['request_id'], request_id)
        self.assertEqual(self.engine.tools.traces()[0]['protocol'], 'ec2-query')

    def test_engine_introspection_requires_management_auth_without_loading_services(self):
        self.assertEqual(self.request('/_local/engine')[0], 401)
        status, body = self.request('/_local/engine',headers={'Authorization':'Bearer private'})
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(report['reference']['exactParity'])
        self.assertTrue(all(info['state']=='available' for info in report['services'].values()))
        self.assertEqual(report['requestHandlers'], ['validate_context','load_provider','dispatch_operation'])
