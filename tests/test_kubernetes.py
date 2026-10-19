import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from localcloud.engine import Error
from localcloud.kube import KubeClient, KubeError, KubernetesExecutor
from localcloud.kube_manifests import installation
from localcloud.operator import Operator, desired_resources, instance


class FakeAPI:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def get(self, path):
        if path not in self.objects:
            raise KubeError('NotFound', path, 404)
        return copy.deepcopy(self.objects[path])

    def list(self, path, labels=None):
        items = [copy.deepcopy(v) for k, v in self.objects.items() if k.rsplit('/', 1)[0] == path]
        if labels:
            pairs = [p.split('=', 1) for p in labels.split(',')]
            items = [v for v in items if all(v['metadata'].get('labels', {}).get(k) == val for k, val in pairs)]
        return items

    def request(self, method, path, body=None, text=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        if text:
            return 'workload output\n'
        if method == 'DELETE':
            del self.objects[path]
            return {'status': 'Success'}
        value = copy.deepcopy(body)
        if path.endswith('/status'):
            path = path[:-7]
        if method == 'POST':
            path += '/' + body['metadata']['name']
            if path in self.objects:
                raise KubeError('Conflict', path, 409)
        value['metadata'].setdefault('uid', 'uid-' + value['metadata']['name'])
        value['metadata'].setdefault('generation', 1)
        value['metadata']['resourceVersion'] = str(len(self.calls))
        self.objects[path] = value
        return copy.deepcopy(value)


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.controller = Operator(self.api, 'localcloud')
        self.item = instance('dev', 'localcloud', 'localcloud:dev', 'localcloud-access')
        self.item['metadata'].update(uid='instance-uid', generation=1, creationTimestamp='2026-01-01T00:00:00Z')
        self.path = self.controller.collection + '/dev'
        self.api.objects[self.path] = copy.deepcopy(self.item)

    def test_reconcile_creates_owned_resources_and_is_idempotent(self):
        self.controller.reconcile(self.item)
        self.assertEqual(self.api.get(self.path)['status']['phase'], 'Pending')
        deployment_path = '/apis/apps/v1/namespaces/localcloud/deployments/dev'
        deployed = self.api.get(deployment_path)
        self.assertEqual(deployed['spec']['replicas'], 1)
        self.assertEqual(deployed['metadata']['ownerReferences'][0]['uid'], 'instance-uid')
        self.assertEqual(deployed['spec']['strategy']['type'], 'Recreate')
        current = self.api.get(self.path)
        count = len(self.api.calls)
        self.controller.reconcile(current)
        self.assertEqual(len(self.api.calls), count)
        self.api.objects[deployment_path]['status'] = {'readyReplicas': 1, 'observedGeneration': 1}
        self.controller.reconcile(current)
        self.assertEqual(self.api.get(self.path)['status']['phase'], 'Ready')

    def test_api_defaults_do_not_cause_endless_deployment_updates(self):
        self.controller.reconcile(self.item)
        path = '/apis/apps/v1/namespaces/localcloud/deployments/dev'
        self.api.objects[path]['spec']['revisionHistoryLimit'] = 10
        self.api.objects[path]['spec']['template']['spec']['containers'][0]['terminationMessagePath'] = '/dev/termination-log'
        count = len(self.api.calls)
        self.controller.reconcile(self.api.get(self.path))
        self.assertEqual(len(self.api.calls), count)

    def test_unrelated_resource_is_never_overwritten(self):
        path = '/api/v1/namespaces/localcloud/services/dev'
        unrelated = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'dev'}, 'spec': {'clusterIP': '10.0.0.2'}}
        self.api.objects[path] = copy.deepcopy(unrelated)
        with self.assertRaises(Error):
            self.controller.reconcile(self.item)
        self.assertEqual(self.api.get(path), unrelated)

    def test_retained_pvc_has_no_gc_owner_and_storage_change_is_rejected(self):
        self.controller.reconcile(self.item)
        pvc_path = '/api/v1/namespaces/localcloud/persistentvolumeclaims/dev-data'
        self.assertNotIn('ownerReferences', self.api.get(pvc_path)['metadata'])
        item = self.api.get(self.path)
        item['spec']['persistence']['size'] = '2Gi'
        with self.assertRaises(Error) as error:
            self.controller.reconcile(item)
        self.assertEqual(error.exception.code, 'ImmutableStorage')
        self.assertEqual(self.api.get(pvc_path)['spec']['resources']['requests']['storage'], '1Gi')

    def test_service_allocated_ip_is_preserved(self):
        self.controller.reconcile(self.item)
        path = '/api/v1/namespaces/localcloud/services/dev'
        self.api.objects[path]['spec']['clusterIP'] = '10.1.2.3'
        self.api.objects[path]['spec']['ports'][0]['port'] = 9999
        self.controller.reconcile(self.api.get(self.path))
        self.assertEqual(self.api.get(path)['spec']['clusterIP'], '10.1.2.3')
        self.assertEqual(self.api.get(path)['spec']['ports'][0]['port'], 4566)

    def test_expired_preview_deletes_cr_with_uid_precondition(self):
        self.item['spec']['ttlSeconds'] = 60
        self.controller.now = lambda: dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.timezone.utc)
        self.controller.reconcile(self.item)
        self.assertNotIn(self.path, self.api.objects)
        self.assertEqual(self.api.calls[0][2]['preconditions']['uid'], 'instance-uid')
        self.assertEqual(len(self.api.calls), 1)

    def test_invalid_instances_report_error_and_continue(self):
        self.api.objects[self.path]['spec']['iamMode'] = 'bogus'
        self.controller.run_once()
        self.assertEqual(self.api.get(self.path)['status']['phase'], 'Error')
        with self.assertRaises(Error):
            desired_resources({**self.item, 'spec': {**self.item['spec'], 'ttlSeconds': True}})

    def test_emptydir_instance_disables_executor_without_images(self):
        self.item['spec'].update(workloadImages=[], persistence={'enabled': False})
        rendered = desired_resources(self.item)
        self.assertEqual([r['kind'] for r in rendered], ['Service', 'Deployment'])
        pod = rendered[-1]['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertIn('emptyDir', pod['volumes'][0])
        self.assertNotIn('--executor', pod['containers'][0]['args'])

    def test_manifest_rbac_is_namespaced_and_never_reads_secrets(self):
        manifests = installation('localcloud', 'localcloud:dev')['items']
        self.assertIn('CustomResourceDefinition', [r['kind'] for r in manifests])
        self.assertNotIn('ClusterRole', [r['kind'] for r in manifests])
        for role in (r for r in manifests if r['kind'] == 'Role'):
            self.assertEqual(role['metadata']['namespace'], 'localcloud')
            for rule in role['rules']:
                self.assertNotIn('secrets', rule['resources'])
                self.assertNotIn('*', rule['resources'])
        self.assertTrue(any(r['kind'] == 'ResourceQuota' for r in manifests))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.executor = KubernetesExecutor(self.api, 'localcloud', 'http://dev.localcloud.svc:4566', ['localcloud:dev'], 'dev', 'instance-uid')

    def submit(self):
        return self.executor.submit({'image': 'localcloud:dev', 'command': ['python3', '-c', 'print("hello")'], 'env': {'EXAMPLE': 'value'}})

    def test_job_injects_endpoint_is_bounded_and_has_no_token(self):
        result = self.submit()
        job = self.api.get(self.executor.jobs + '/' + result['name'])
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 300)
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'], 600)
        pod = job['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        container = pod['containers'][0]
        env = {e['name']: e['value'] for e in container['env']}
        self.assertEqual(env['AWS_ENDPOINT_URL'], 'http://dev.localcloud.svc:4566')
        self.assertNotIn('LOCALCLOUD_MANAGEMENT_TOKEN', env)
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(job['metadata']['ownerReferences'][0]['uid'], 'instance-uid')
        self.assertEqual(self.executor.list()[0]['phase'], 'Pending')

    def test_unapproved_images_reserved_env_and_bad_limits_are_rejected(self):
        cases = [{'image': 'unapproved:latest'}, {'env': {'AWS_ENDPOINT_URL': 'http://evil'}}, {'timeoutSeconds': True}, {'ttlSeconds': 1}, {'command': 'sh -c evil'}, {'command': ['\x00']}]
        for case in cases:
            with self.assertRaises(Error):
                self.executor.submit({'image': 'localcloud:dev', 'command': ['true'], **case})
        self.assertEqual(self.api.calls, [])

    def test_foreign_job_cannot_be_read_or_deleted(self):
        result = self.submit()
        path = self.executor.jobs + '/' + result['name']
        self.api.objects[path]['metadata']['labels']['localcloud.dev/instance'] = 'someone-else'
        with self.assertRaises(Error):
            self.executor.status(result['name'])
        with self.assertRaises(Error):
            self.executor.cancel(result['name'])
        self.assertIn(path, self.api.objects)

    def test_status_logs_and_cancellation_use_job_ownership(self):
        result = self.submit()
        name = result['name']
        path = self.executor.jobs + '/' + name
        self.api.objects[path]['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        self.assertEqual(self.executor.status(name)['phase'], 'Complete')
        self.assertTrue(self.executor.logs(name)['pending'])
        self.api.objects['/api/v1/namespaces/localcloud/pods/workload-1'] = {'metadata': {'name': 'workload-1', 'labels': {'job-name': name}, 'ownerReferences': [{'uid': self.api.objects[path]['metadata']['uid']}]}}
        self.assertIn('workload output', self.executor.logs(name)['logs'])
        self.executor.cancel(name)
        self.assertNotIn(path, self.api.objects)
        self.assertEqual(self.api.calls[-1][2]['propagationPolicy'], 'Background')

    def test_spoofed_pod_labels_do_not_grant_log_access(self):
        name = self.submit()['name']
        self.api.objects['/api/v1/namespaces/localcloud/pods/foreign'] = {'metadata': {'name': 'foreign', 'labels': {'job-name': name}, 'ownerReferences': [{'uid': 'other-job'}]}}
        with self.assertRaises(Error):
            self.executor.logs(name)


class ClientTests(unittest.TestCase):
    def test_local_proxy_restriction_and_pagination(self):
        with self.assertRaises(ValueError):
            KubeClient('http://example.com:8001')
        client = KubeClient('http://127.0.0.1:8001')
        client.get = Mock(side_effect=[{'items': [{'id': 1}], 'metadata': {'continue': 'next'}}, {'items': [{'id': 2}]}])
        self.assertEqual(client.list('/api/v1/pods', 'a=b'), [{'id': 1}, {'id': 2}])
        self.assertIn('labelSelector=a%3Db&continue=next', client.get.call_args_list[1].args[0])

    def test_projected_token_is_reread_on_every_request(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            client = KubeClient('http://127.0.0.1:8001', token_file=str(token))
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{}'
            client.opener.open = Mock(return_value=response)
            token.write_text('first')
            client.get('/api/v1/pods')
            self.assertEqual(client.opener.open.call_args.args[0].get_header('Authorization'), 'Bearer first')
            token.write_text('rotated')
            client.get('/api/v1/pods')
            self.assertEqual(client.opener.open.call_args.args[0].get_header('Authorization'), 'Bearer rotated')
