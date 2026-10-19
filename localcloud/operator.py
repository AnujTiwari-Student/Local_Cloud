"""Namespace-scoped LocalCloudInstance reconciler and preview CLI."""
import argparse
import copy
import datetime as dt
import json
import os
import time
from .engine import Error
from .kube import KubeClient, KubeError, dns_name, namespace_path

API_VERSION = 'localcloud.dev/v1alpha1'
PLURAL = 'localcloudinstances'


def instance(name, namespace, image, secret, ttl=None, persistent=True, workload_images=None):
    dns_name(name)
    if len(name) > 40:
        raise Error('InvalidInstance', 'Instance name must be at most 40 characters')
    spec = {'image': image, 'managementTokenSecret': dns_name(secret), 'iamMode': 'soft',
            'persistence': {'enabled': persistent, 'size': '1Gi', 'retain': True},
            'workloadImages': workload_images or [image]}
    if ttl is not None:
        spec['ttlSeconds'] = ttl
    return {'apiVersion': API_VERSION, 'kind': 'LocalCloudInstance', 'metadata': {'name': name, 'namespace': dns_name(namespace)}, 'spec': spec}


def desired_resources(resource):
    """Render only allowlisted configuration, never pass raw pod templates through."""
    import re
    spec, metadata = resource['spec'], resource['metadata']
    name, namespace = dns_name(metadata['name']), dns_name(metadata['namespace'])
    if len(name) > 40 or not isinstance(spec.get('image'), str) or not spec['image'] or len(spec['image']) > 256:
        raise Error('InvalidInstance', 'Invalid image or instance name')
    secret = dns_name(spec['managementTokenSecret'])
    iam = spec.get('iamMode', 'soft')
    if iam not in ('off', 'soft', 'enforce'):
        raise Error('InvalidInstance', 'Invalid IAM mode')
    persistence = spec.get('persistence', {})
    if not isinstance(persistence, dict) or type(persistence.get('enabled', True)) is not bool or type(persistence.get('retain', True)) is not bool:
        raise Error('InvalidInstance', 'Invalid persistence configuration')
    size = persistence.get('size', '1Gi')
    if not isinstance(size, str) or not re.fullmatch(r'[1-9][0-9]*(Mi|Gi)', size):
        raise Error('InvalidInstance', 'Storage size must be a positive Mi or Gi quantity')
    images = spec.get('workloadImages', [])
    if not isinstance(images, list) or len(images) > 20 or any(not isinstance(i, str) or not i or len(i) > 256 for i in images):
        raise Error('InvalidInstance', 'Invalid workload image allowlist')
    ttl = spec.get('ttlSeconds')
    if ttl is not None and (type(ttl) is not int or not 60 <= ttl <= 604800):
        raise Error('InvalidInstance', 'Instance TTL must be 60 seconds through 7 days')
    labels = {'app.kubernetes.io/managed-by': 'localcloud-operator', 'localcloud.dev/instance': name}
    selector = {'localcloud.dev/instance': name, 'app.kubernetes.io/name': 'localcloud'}
    owner = {'apiVersion': API_VERSION, 'kind': 'LocalCloudInstance', 'name': name, 'uid': metadata['uid'], 'controller': True}
    def meta(child_name, owned=True):
        data = {'name': child_name, 'namespace': namespace, 'labels': copy.deepcopy(labels)}
        if owned:
            data['ownerReferences'] = [owner]
        return data
    endpoint = f'http://{name}.{namespace}.svc:4566'
    args = ['--host', '0.0.0.0', '--data', '/data/state.sqlite3', '--iam', iam,
            '--public-endpoint', endpoint, '--management-token-env', 'LOCALCLOUD_MANAGEMENT_TOKEN']
    if images:
        args += ['--executor', 'kubernetes', '--kube-namespace', namespace, '--instance-name', name, '--instance-uid', metadata['uid']]
        for image in images:
            args += ['--workload-image', image]
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': meta(name), 'spec': {
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': selector},
        'template': {'metadata': {'labels': {**labels, **selector}}, 'spec': {
            'serviceAccountName': 'localcloud-executor' if images else 'default', 'automountServiceAccountToken': bool(images),
            'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True, 'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'localcloud', 'image': spec['image'], 'imagePullPolicy': 'IfNotPresent', 'args': args,
                'env': [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}, {'name': 'LOCALCLOUD_MANAGEMENT_TOKEN', 'valueFrom': {'secretKeyRef': {'name': secret, 'key': 'token'}}}],
                'ports': [{'containerPort': 4566, 'name': 'http'}],
                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                'volumeMounts': [{'name': 'state', 'mountPath': '/data'}, {'name': 'tmp', 'mountPath': '/tmp'}],
                'readinessProbe': {'httpGet': {'path': '/_local/health', 'port': 'http'}, 'initialDelaySeconds': 2, 'periodSeconds': 5},
                'livenessProbe': {'httpGet': {'path': '/_local/health', 'port': 'http'}, 'initialDelaySeconds': 10, 'periodSeconds': 10}}],
            'volumes': [{'name': 'state', **({'persistentVolumeClaim': {'claimName': name + '-data'}} if persistence.get('enabled', True) else {'emptyDir': {}})}, {'name': 'tmp', 'emptyDir': {}}]}}}}
    service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': meta(name), 'spec': {'type': 'ClusterIP', 'selector': selector, 'ports': [{'port': 4566, 'targetPort': 'http', 'name': 'http'}]}}
    resources = []
    if persistence.get('enabled', True):
        pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': meta(name + '-data', not persistence.get('retain', True)), 'spec': {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': size}}}}
        if persistence.get('storageClassName') is not None:
            if not isinstance(persistence['storageClassName'], str):
                raise Error('InvalidInstance', 'Invalid storage class')
            pvc['spec']['storageClassName'] = persistence['storageClassName']
        resources.append(pvc)
    return resources + [service, deployment]


def contains(observed, desired):
    """Compare owned fields while tolerating Kubernetes API defaulting."""
    if isinstance(desired, dict):
        return isinstance(observed, dict) and all(k in observed and contains(observed[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        return isinstance(observed, list) and len(observed) == len(desired) and all(contains(a, b) for a, b in zip(observed, desired))
    return observed == desired


def storage_mib(value):
    if value.endswith('Gi'):
        return int(value[:-2]) * 1024
    if value.endswith('Mi'):
        return int(value[:-2])
    return value


class Operator:
    def __init__(self, client, namespace, now=None):
        self.client, self.namespace = client, dns_name(namespace)
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.collection = namespace_path(namespace, API_VERSION) + '/' + PLURAL

    def child_collection(self, resource):
        group = 'apps/v1' if resource['kind'] == 'Deployment' else 'v1'
        plural = {'Deployment': 'deployments', 'Service': 'services', 'PersistentVolumeClaim': 'persistentvolumeclaims'}[resource['kind']]
        return namespace_path(self.namespace, group) + '/' + plural

    def ensure(self, desired):
        path = self.child_collection(desired)
        try:
            existing = self.client.get(path + '/' + desired['metadata']['name'])
        except KubeError as error:
            if error.status != 404:
                raise
            try:
                return self.client.request('POST', path, desired)
            except KubeError as conflict:
                if conflict.status != 409:
                    raise
                return self.ensure(desired)
        labels = existing['metadata'].get('labels', {})
        for key in ('app.kubernetes.io/managed-by', 'localcloud.dev/instance'):
            if labels.get(key) != desired['metadata']['labels'][key]:
                raise Error('ResourceConflict', 'Refusing to adopt an unrelated ' + desired['kind'], 409)
        desired_owners = desired['metadata'].get('ownerReferences', [])
        existing_owners = existing['metadata'].get('ownerReferences', [])
        if desired_owners and not any(r.get('uid') == desired_owners[0]['uid'] for r in existing_owners):
            raise Error('ResourceConflict', 'Resource belongs to another instance generation', 409)
        # PVC size/storage class/retention are immutable in this controller.
        if desired['kind'] == 'PersistentVolumeClaim':
            current_spec = existing['spec']
            if storage_mib(current_spec.get('resources', {}).get('requests', {}).get('storage', '')) != storage_mib(desired['spec']['resources']['requests']['storage']) or ('storageClassName' in desired['spec'] and current_spec.get('storageClassName') != desired['spec']['storageClassName']) or bool(existing_owners) != bool(desired_owners):
                raise Error('ImmutableStorage', 'PVC size, storage class and retention cannot be changed by this operator', 409)
            return existing
        if contains(existing['spec'], desired['spec']) and contains(existing['metadata'], desired['metadata']):
            return existing
        updated = copy.deepcopy(existing)
        updated['metadata'].setdefault('labels', {}).update(desired['metadata']['labels'])
        updated['metadata']['ownerReferences'] = desired['metadata'].get('ownerReferences', [])
        # Preserve API-assigned service networking, but own the deployment spec.
        if desired['kind'] == 'Service':
            updated['spec'].update(desired['spec'])
        else:
            updated['spec'] = desired['spec']
        if updated['spec'] == existing['spec'] and updated['metadata'] == existing['metadata']:
            return existing
        return self.client.request('PUT', path + '/' + desired['metadata']['name'], updated)

    def status(self, resource, value):
        if resource.get('status') == value:
            return
        update = copy.deepcopy(resource)
        update['status'] = value
        self.client.request('PUT', self.collection + '/' + resource['metadata']['name'] + '/status', update)

    def reconcile(self, resource):
        if resource['metadata'].get('deletionTimestamp'):
            return
        desired = desired_resources(resource)
        ttl = resource['spec'].get('ttlSeconds')
        if ttl is not None:
            created = dt.datetime.fromisoformat(resource['metadata']['creationTimestamp'].replace('Z', '+00:00'))
            if self.now() >= created + dt.timedelta(seconds=ttl):
                self.client.request('DELETE', self.collection + '/' + resource['metadata']['name'], {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': resource['metadata']['uid']}})
                return
        deployment = None
        for child in desired:
            observed = self.ensure(child)
            if child['kind'] == 'Deployment':
                deployment = observed
        state = deployment.get('status', {})
        ready = state.get('readyReplicas', 0) >= 1 and state.get('observedGeneration', 0) >= deployment['metadata'].get('generation', 1)
        self.status(resource, {'phase': 'Ready' if ready else 'Pending', 'observedGeneration': resource['metadata'].get('generation', 1), 'endpoint': f"http://{resource['metadata']['name']}.{self.namespace}.svc:4566"})

    def run_once(self):
        for resource in self.client.list(self.collection):
            try:
                self.reconcile(resource)
            except (Error, ValueError, KeyError, TypeError) as error:
                self.status(resource, {'phase': 'Error', 'observedGeneration': resource['metadata'].get('generation', 1), 'message': str(error)[:500]})


def main(argv=None):
    parser = argparse.ArgumentParser(description='LocalCloud Kubernetes operator and preview manager')
    parser.add_argument('--api-server', help='Loopback kubectl proxy URL; omit when running in-cluster')
    parser.add_argument('--namespace', default=os.environ.get('POD_NAMESPACE', 'localcloud'))
    sub = parser.add_subparsers(dest='action', required=True)
    controller = sub.add_parser('run')
    controller.add_argument('--once', action='store_true')
    controller.add_argument('--interval', type=float, default=5)
    manifests = sub.add_parser('manifests')
    manifests.add_argument('--image', required=True)
    sample = sub.add_parser('example')
    sample.add_argument('--name', default='dev')
    sample.add_argument('--image', required=True)
    sample.add_argument('--secret', default='localcloud-access')
    preview = sub.add_parser('preview')
    previews = preview.add_subparsers(dest='preview_action', required=True)
    create = previews.add_parser('create')
    create.add_argument('--name', required=True)
    create.add_argument('--image', required=True)
    create.add_argument('--secret', default='localcloud-access')
    create.add_argument('--ttl', type=int, default=3600)
    previews.add_parser('list')
    delete = previews.add_parser('delete')
    delete.add_argument('--name', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'manifests':
            from .kube_manifests import installation
            print(json.dumps(installation(args.namespace, args.image), indent=2))
            return
        if args.action == 'example':
            print(json.dumps(instance(args.name, args.namespace, args.image, args.secret), indent=2))
            return
        client = KubeClient(args.api_server)
        operator = Operator(client, args.namespace)
        if args.action == 'preview':
            if args.preview_action == 'create':
                item = instance(args.name, args.namespace, args.image, args.secret, ttl=args.ttl, persistent=False)
                item['metadata']['labels'] = {'localcloud.dev/preview': 'true'}
                # Validate before posting, including the TTL; server assigns UID later.
                desired_resources({**item, 'metadata': {**item['metadata'], 'uid': 'validation'}})
                result = client.request('POST', operator.collection, item)
            elif args.preview_action == 'list':
                result = client.list(operator.collection, 'localcloud.dev/preview=true')
            else:
                dns_name(args.name)
                current = client.get(operator.collection + '/' + args.name)
                if current['metadata'].get('labels', {}).get('localcloud.dev/preview') != 'true':
                    raise Error('NotPreview', 'Refusing to delete a non-preview instance')
                result = client.request('DELETE', operator.collection + '/' + args.name, {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': current['metadata']['uid']}})
            print(json.dumps(result, indent=2))
            return
        if args.interval < 1:
            parser.error('Interval must be at least 1 second')
        while True:
            try:
                operator.run_once()
            except Error as error:
                if args.once:
                    raise
                print('Reconciliation error: ' + str(error), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except (Error, ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
