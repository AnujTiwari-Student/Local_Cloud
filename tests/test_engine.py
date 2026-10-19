import tempfile
import threading
import unittest
from pathlib import Path
from localcloud.engine import Engine, Error


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.sqlite3')
        self.e = Engine(self.path)

    def tearDown(self):
        self.e.close()
        self.tmp.cleanup()

    def queue(self):
        return self.e.sqs('CreateQueue', {'QueueName': 'jobs'})['QueueUrl']

    def test_persistence_after_restart(self):
        self.queue()
        self.e.s3('PUT', 'assets', '')
        self.e.s3('PUT', 'assets', 'binary', b'\x00\xff')
        self.e.close()
        self.e = Engine(self.path)
        self.assertEqual(self.e.overview()['queues'][0]['name'], 'jobs')
        self.assertEqual(self.e.s3('GET', 'assets', 'binary'), b'\x00\xff')

    def test_snapshot_replaces_data_and_policies(self):
        self.queue()
        self.e.snapshot('before')
        self.e.sqs('CreateQueue', {'QueueName': 'after'})
        self.e.policy('test', {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}})
        self.e.snapshot('before', restore=True)
        self.assertEqual([q['name'] for q in self.e.overview()['queues']], ['jobs'])
        self.assertEqual(self.e.overview()['principals'], [])
        with self.assertRaises(Error):
            self.e.snapshot('before')
        with self.assertRaises(Error):
            self.e.snapshot('../outside')

    def test_visibility_and_acknowledgement(self):
        url = self.queue()
        self.e.sqs('SendMessage', {'QueueUrl': url, 'MessageBody': 'hello'})
        message = self.e.sqs('ReceiveMessage', {'QueueUrl': url})['Messages'][0]
        self.assertEqual(self.e.sqs('ReceiveMessage', {'QueueUrl': url})['Messages'], [])
        self.e.sqs('DeleteMessage', {'QueueUrl': url, 'ReceiptHandle': message['ReceiptHandle']})
        self.assertEqual(self.e.overview()['queues'][0]['messages'], 0)

    def test_redelivery_invalidates_old_receipt(self):
        url = self.queue()
        self.e.sqs('SendMessage', {'QueueUrl': url, 'MessageBody': 'hello'})
        first = self.e.sqs('ReceiveMessage', {'QueueUrl': url})['Messages'][0]
        self.e.sqs('ChangeMessageVisibility', {'QueueUrl': url, 'ReceiptHandle': first['ReceiptHandle'], 'VisibilityTimeout': 0})
        second = self.e.sqs('ReceiveMessage', {'QueueUrl': url})['Messages'][0]
        self.assertNotEqual(first['ReceiptHandle'], second['ReceiptHandle'])
        with self.assertRaises(Error):
            self.e.sqs('DeleteMessage', {'QueueUrl': url, 'ReceiptHandle': first['ReceiptHandle']})

    def test_explicit_deny_overrides_allow_and_implicit_deny(self):
        self.e.iam_mode = 'enforce'
        with self.assertRaises(Error) as denied:
            self.queue()
        self.assertEqual(denied.exception.status, 403)
        self.e.policy('test', {'Statement': [{'Effect': 'Allow', 'Action': 'sqs:*', 'Resource': '*'}, {'Effect': 'Deny', 'Action': 'sqs:DeleteQueue', 'Resource': '*'}]})
        url = self.queue()
        with self.assertRaises(Error):
            self.e.sqs('DeleteQueue', {'QueueUrl': url})
        self.assertEqual(len(self.e.overview()['queues']), 1)

    def test_unsupported_policy_is_rejected(self):
        with self.assertRaises(Error):
            self.e.policy('test', {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*', 'Condition': {}}})
        self.assertEqual(self.e.overview()['principals'], [])

    def test_soft_mode_logs_but_allows(self):
        self.e.iam_mode = 'soft'
        self.queue()
        self.assertEqual(self.e.overview()['audit'][0]['decision'], 'deny')

    def test_concurrent_sends_do_not_lose_updates(self):
        url = self.queue()
        errors = []
        def send(i):
            try:
                self.e.sqs('SendMessage', {'QueueUrl': url, 'MessageBody': str(i)})
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=send, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.e.overview()['queues'][0]['messages'], 30)

    def test_s3_missing_and_nonempty_bucket(self):
        self.e.s3('PUT', 'assets', '')
        self.e.s3('PUT', 'assets', 'a/b', b'data')
        with self.assertRaises(Error):
            self.e.s3('DELETE', 'assets', '')
        self.e.s3('DELETE', 'assets', 'a/b')
        self.e.s3('DELETE', 'assets', '')
        with self.assertRaises(Error):
            self.e.s3('GET', 'assets', 'a/b')

    def test_invalid_request_does_not_mutate_state(self):
        url = self.queue()
        with self.assertRaises(Error):
            self.e.sqs('SendMessage', {'QueueUrl': url, 'MessageBody': 'bad', 'DelaySeconds': -1})
        self.assertEqual(self.e.overview()['queues'][0]['messages'], 0)
        with self.assertRaises(Error):
            self.e.sqs('CreateQueue', {'QueueName': 'fifo', 'Attributes': {'FifoQueue': 'true'}})
