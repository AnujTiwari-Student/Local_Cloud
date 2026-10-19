import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from localcloud.engine import Engine, Error


class ToolingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = Engine(str(Path(self.tmp.name) / 'state.db'))

    def tearDown(self):
        self.e.close()
        self.tmp.cleanup()

    @staticmethod
    def checksum(bundle):
        body = json.dumps(bundle['state'], sort_keys=True, separators=(',', ':'), allow_nan=False)
        bundle['sha256'] = hashlib.sha256(body.encode()).hexdigest()
        return bundle

    def test_portable_roundtrip_preserves_binary_messages_and_policies(self):
        self.e.s3('PUT', 'assets', '')
        self.e.s3('PUT', 'assets', 'binary', b'\x00\xff')
        url = self.e.sqs('CreateQueue', {'QueueName': 'jobs'})['QueueUrl']
        self.e.sqs('SendMessage', {'QueueUrl': url, 'MessageBody': 'hello'})
        self.e.policy('alice', {'Statement': {'Effect': 'Allow', 'Action': 'sqs:*', 'Resource': '*'}})
        bundle = json.loads(json.dumps(self.e.tools.export_state()))
        other = Engine(':memory:', endpoint='http://localhost:9999')
        try:
            other.tools.import_state(bundle)
            self.assertEqual(other.s3('GET', 'assets', 'binary'), b'\x00\xff')
            self.assertEqual(other.sqs('GetQueueUrl', {'QueueName': 'jobs'})['QueueUrl'], 'http://localhost:9999/000000000000/jobs')
            self.assertEqual(other.sqs('ReceiveMessage', {'QueueUrl': url})['Messages'][0]['Body'], 'hello')
            self.assertEqual(other.overview()['principals'], ['alice'])
            self.assertEqual(other.tools.rules(), [])
        finally:
            other.close()

    def test_invalid_import_never_changes_active_state(self):
        self.e.s3('PUT', 'assets', '')
        baseline = self.e.tools.export_state()
        invalid = copy.deepcopy(baseline)
        invalid['state']['buckets'] = {}
        with self.assertRaises(Error):
            self.e.tools.import_state(invalid)  # checksum mismatch
        invalid = copy.deepcopy(baseline)
        invalid['state']['buckets']['assets']['bad'] = '%%not-base64'
        with self.assertRaises(Error):
            self.e.tools.import_state(self.checksum(invalid))
        invalid = copy.deepcopy(baseline)
        invalid['state']['policies']['alice'] = [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]
        with self.assertRaises(Error):
            self.e.tools.import_state(self.checksum(invalid))
        self.assertEqual(self.e.tools.export_state(), baseline)

    def test_trace_includes_success_error_and_evaluation_without_payloads(self):
        self.e.s3('PUT', 'assets', '', principal='alice')
        with self.assertRaises(Error):
            self.e.s3('GET', 'assets', 'missing', principal='bob')
        traces = self.e.tools.traces()
        self.assertEqual(traces[0]['error'], 'NoSuchKey')
        self.assertEqual(traces[0]['status'], 404)
        self.assertEqual(traces[1]['permission']['decision'], 'disabled')
        self.assertGreaterEqual(traces[0]['duration_ms'], 0)
        self.assertEqual(len(self.e.tools.traces('alice')), 1)
        self.assertNotIn('body', traces[0])
        self.e.tools.clear_traces()
        self.assertEqual(self.e.tools.traces(), [])

    def test_policy_generation_keeps_action_resource_pairs_separate(self):
        self.e.s3('PUT', 'assets', '')
        self.e.s3('PUT', 'assets', 'a', b'payload')
        self.e.s3('GET', 'assets', 'a')
        self.e.s3('GET', 'assets', 'a')
        policy = self.e.tools.generated_policy('test')
        self.assertEqual(len(policy['Statement']), 3)
        self.assertIn({'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::assets/a'}, policy['Statement'])
        self.assertNotIn('payload', json.dumps(policy))
        self.assertEqual(self.e.tools.generated_policy('absent')['Statement'], [])

    def test_policy_from_denials_does_not_automatically_install_permissions(self):
        self.e.iam_mode = 'enforce'
        with self.assertRaises(Error):
            self.e.sqs('CreateQueue', {'QueueName': 'jobs'}, 'alice')
        self.assertEqual(self.e.tools.generated_policy('alice')['Statement'], [])
        proposed = self.e.tools.generated_policy('alice', 'violations')
        self.assertEqual(proposed['Statement'][0]['Action'], 'sqs:CreateQueue')
        self.assertEqual(self.e.overview()['principals'], [])
        self.assertEqual(self.e.tools.traces()[0]['permission']['decision'], 'deny')

    def test_fault_error_prevents_mutation_and_management_can_remove(self):
        self.e.tools.set_rule({'name': 's3-outage', 'service': 's3', 'operation': 'CreateBucket', 'error': {'code': 'ServiceUnavailable', 'status': 503}})
        with self.assertRaises(Error) as caught:
            self.e.s3('PUT', 'assets', '')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.e.overview()['buckets'], [])
        trace = self.e.tools.traces()[0]
        self.assertEqual(trace['error'], 'ServiceUnavailable')
        self.assertIsNone(trace['permission'])
        self.e.tools.delete_rule('s3-outage')
        self.e.s3('PUT', 'assets', '')
        self.assertEqual(len(self.e.overview()['buckets']), 1)

    def test_probability_scope_and_latency_with_injected_clock(self):
        sleeps = []
        self.e.tools.sleep = sleeps.append
        self.e.tools.random = lambda: 0.5
        self.e.tools.set_rule({'name': 'delay', 'service': 's3', 'operation': 'Create*', 'probability': 0.6, 'latency_ms': 200})
        self.e.sqs('CreateQueue', {'QueueName': 'jobs'})
        self.e.s3('PUT', 'assets', '')
        self.e.s3('GET', 'assets', '')
        self.assertEqual(sleeps, [0.2])
        self.e.tools.set_rule({'name': 'delay', 'service': 's3', 'probability': 0, 'latency_ms': 200})
        self.e.s3('GET', 'assets', '')
        self.assertEqual(sleeps, [0.2])

    def test_rule_validation_and_restart_persistence(self):
        for values in [{'probability': float('nan')}, {'probability': True}, {'latency_ms': -1}, {'latency_ms': 5001}, {'error': {'code': '<xml>'}}, {'service': 'unsupported'}]:
            with self.assertRaises(Error):
                self.e.tools.set_rule({'name': 'invalid', 'service': 's3', 'latency_ms': 10, **values})
        self.e.tools.set_rule({'name': 'delay', 'service': 's3', 'latency_ms': 1})
        self.e.sqs('ListQueues', {})
        self.e.close()
        self.e = Engine(str(Path(self.tmp.name) / 'state.db'))
        self.assertEqual(len(self.e.tools.rules()), 1)
        self.assertEqual(len(self.e.tools.traces()), 1)

    def test_trace_history_is_bounded(self):
        for _ in range(1005):
            self.e.sqs('ListQueues', {})
        self.assertEqual(len(self.e.tools.traces()), 1000)
