"""Dashboard routes exercise the real handler and engine without socket access."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import test_http
from localcloud.engine import Engine


class DashboardTests(unittest.TestCase):
    request = test_http.HTTPTests.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name) / 'db'))
        self.server = SimpleNamespace(engine=self.engine, management_token='private', instance_name='dev')

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def api(self, service, action, token='private', **args):
        status, payload = self.request('/_local/' + service, json.dumps({'Action': action, **args}).encode(), 'POST', {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        return status, json.loads(payload)

    def test_assets_are_available_before_login_and_metadata_is_protected(self):
        self.assertIn(b'Resource Browser', self.request('/_local')[1])
        self.assertIn(b'function disconnect', self.request('/_local/dashboard.js')[1])
        self.assertIn(b'color-scheme', self.request('/_local/dashboard.css')[1])
        self.assertEqual(self.request('/_local/resources')[0], 401)
        status, payload = self.request('/_local/resources', headers={'Authorization': 'Bearer private'})
        details = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(details['instanceName'], 'dev')
        self.assertEqual(details['ec2Backend'], 'mock')
        self.assertEqual(details['services'], ['ec2', 's3', 'sqs', 'iam', 'sts'])
        self.assertNotIn('private', payload.decode())
        self.assertEqual(self.request('/_local/../engine.py')[0], 401)

    def test_s3_dashboard_binary_crud_and_bucket_not_empty(self):
        self.assertEqual(self.api('s3', 'CreateBucket', Bucket='web-assets')[0], 200)
        encoded = base64.b64encode(b'\x00\xffhello').decode()
        self.assertEqual(self.api('s3', 'PutObject', Bucket='web-assets', Key='folder/a.txt', ContentBase64=encoded)[0], 200)
        status, listing = self.api('s3', 'ListObjects', Bucket='web-assets')
        self.assertEqual(listing['objects'], [{'key': 'folder/a.txt', 'size': 7}])
        self.assertEqual(self.api('s3', 'GetObject', Bucket='web-assets', Key='folder/a.txt')[1]['ContentBase64'], encoded)
        self.assertEqual(self.api('s3', 'DeleteBucket', Bucket='web-assets')[0], 409)
        self.assertEqual(self.api('s3', 'DeleteObject', Bucket='web-assets', Key='folder/a.txt')[0], 200)
        self.assertEqual(self.api('s3', 'DeleteBucket', Bucket='web-assets')[0], 200)
        self.assertEqual(self.api('s3', 'ListBuckets')[1]['buckets'], [])

    def test_dashboard_auth_iam_and_invalid_s3_requests(self):
        for service, action, args in [('s3', 'CreateBucket', {'Bucket': 'assets'}), ('sqs', 'CreateQueue', {'QueueName': 'jobs'}), ('ec2', 'RunInstances', {'ImageId': 'ami-local'})]:
            self.assertEqual(self.api(service, action, token='wrong', **args)[0], 401)
        self.engine.iam_mode = 'enforce'
        self.assertEqual(self.api('s3', 'CreateBucket', Bucket='assets')[0], 403)
        self.assertEqual(self.api('sqs', 'CreateQueue', QueueName='jobs')[0], 403)
        self.engine.iam_mode = 'off'
        for action, args in [('GetObject', {'Bucket': 'assets'}), ('CreateBucket', {'Bucket': 'assets', 'Key': 'wrong'}), ('ListBuckets', {'Unexpected': True}), ('PutObject', {'Bucket': 'assets', 'Key': 'a', 'ContentBase64': '!!!'})]:
            self.assertGreaterEqual(self.api('s3', action, **args)[0], 400)
        self.assertEqual(self.engine.overview()['buckets'], [])

    def test_sqs_dashboard_message_lifecycle_and_inspector(self):
        status, created = self.api('sqs', 'CreateQueue', QueueName='jobs', Attributes={'VisibilityTimeout': '60'})
        self.assertEqual(status, 200)
        url = created['QueueUrl']
        self.assertEqual(self.api('sqs', 'GetQueueUrl', QueueName='jobs')[1]['QueueUrl'], url)
        self.assertEqual(self.api('sqs', 'SendMessage', QueueUrl=url, MessageBody='hello')[0], 200)
        message = self.api('sqs', 'ReceiveMessage', QueueUrl=url)[1]['Messages'][0]
        self.assertEqual(message['Body'], 'hello')
        self.assertEqual(self.api('sqs', 'GetQueueAttributes', QueueUrl=url)[1]['Attributes']['VisibilityTimeout'], '60')
        self.assertEqual(self.api('sqs', 'DeleteMessage', QueueUrl=url, ReceiptHandle=message['ReceiptHandle'])[0], 200)
        self.assertEqual(self.api('sqs', 'PurgeQueue', QueueUrl=url)[0], 200)
        self.assertEqual(self.api('sqs', 'DeleteQueue', QueueUrl=url)[0], 200)
        self.assertEqual(self.engine.overview()['queues'], [])
        self.assertTrue(any(t['operation'] == 'SendMessage' for t in self.engine.tools.traces()))

    def test_management_bridge_requires_json(self):
        for path in ('s3', 'sqs', 'ec2'):
            status, _ = self.request('/_local/' + path, b'{}', 'POST', {'Authorization': 'Bearer private'})
            self.assertEqual(status, 415)
        status, _ = self.request('/_local/s3', b'[]', 'POST', {'Authorization': 'Bearer private', 'Content-Type': 'application/json'})
        self.assertEqual(status, 400)

    def test_queue_settings_update_preserves_inflight_receipt(self):
        url = self.api('sqs', 'CreateQueue', QueueName='jobs', Attributes={'VisibilityTimeout': '60'})[1]['QueueUrl']
        self.api('sqs', 'SendMessage', QueueUrl=url, MessageBody='hello')
        message = self.api('sqs', 'ReceiveMessage', QueueUrl=url)[1]['Messages'][0]
        self.assertEqual(self.api('sqs', 'SetQueueAttributes', QueueUrl=url, Attributes={'VisibilityTimeout': '90'})[0], 200)
        self.assertEqual(self.api('sqs', 'GetQueueAttributes', QueueUrl=url)[1]['Attributes']['VisibilityTimeout'], '90')
        self.assertEqual(self.api('sqs', 'DeleteMessage', QueueUrl=url, ReceiptHandle=message['ReceiptHandle'])[0], 200)
        self.assertEqual(self.api('sqs', 'SetQueueAttributes', QueueUrl=url, Attributes={'Unsupported': 'value'})[0], 501)
        self.assertEqual(self.api('sqs', 'SetQueueAttributes', QueueUrl=url, Attributes={'VisibilityTimeout': '-1'})[0], 400)
        self.assertEqual(self.api('sqs', 'GetQueueAttributes', QueueUrl=url)[1]['Attributes']['VisibilityTimeout'], '90')
