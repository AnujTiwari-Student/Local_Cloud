"""Small Kubernetes REST client; no third-party Python dependencies."""
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .engine import Error


class KubeError(Error):
    pass


def dns_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise Error('InvalidParameterValue', 'Expected a Kubernetes DNS label of at most 63 characters')
    return value


class KubeClient:
    def __init__(self, server=None, token_file=None, ca_file=None, timeout=15):
        self.in_cluster = server is None
        if server is None:
            host = os.environ.get('KUBERNETES_SERVICE_HOST')
            port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')
            if not host:
                raise Error('KubernetesNotConfigured', 'Run in-cluster or pass --api-server for a kubectl proxy', 503)
            if ':' in host:
                host = '[' + host + ']'
            server = f'https://{host}:{port}'
            token_file = token_file or '/var/run/secrets/kubernetes.io/serviceaccount/token'
            ca_file = ca_file or '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid Kubernetes API endpoint')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Plain HTTP Kubernetes API access must use a loopback kubectl proxy')
        self.server = server.rstrip('/')
        self.token_file = token_file
        self.timeout = timeout
        handlers = [urllib.request.ProxyHandler({})]
        if parsed.scheme == 'https':
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        # Redirects must not forward a bearer token to a different endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        handlers.append(NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, body=None, text=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Expected an absolute Kubernetes API path')
        headers = {'Accept': 'application/json'}
        if self.token_file:
            # Read on every request to support projected token rotation.
            headers['Authorization'] = 'Bearer ' + Path(self.token_file).read_text().strip()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.server + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(8 * 1024 * 1024)
                return payload.decode(errors='replace') if text else json.loads(payload or b'{}')
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read(8192)).get('message', error.reason)
            except (ValueError, AttributeError):
                message = str(error.reason)
            raise KubeError('KubernetesAPIError', str(message), error.code) from None
        except (urllib.error.URLError, OSError) as error:
            raise KubeError('KubernetesUnavailable', str(error), 503) from None

    def get(self, path):
        return self.request('GET', path)

    def list(self, path, labels=None):
        suffix = '?labelSelector=' + urllib.parse.quote(labels, safe='') if labels else ''
        result, cursor = [], None
        while True:
            query = suffix
            if cursor:
                query += ('&' if query else '?') + 'continue=' + urllib.parse.quote(cursor, safe='')
            page = self.get(path + query)
            result.extend(page.get('items', []))
            cursor = page.get('metadata', {}).get('continue')
            if not cursor:
                return result


def namespace_path(namespace, group='v1'):
    dns_name(namespace)
    prefix = '/api/v1' if group == 'v1' else '/apis/' + group
    return prefix + '/namespaces/' + namespace


class KubernetesExecutor:
    def __init__(self, client, namespace, endpoint, allowed_images, instance='localcloud', instance_uid=None):
        self.client, self.namespace = client, dns_name(namespace)
        self.endpoint, self.images = endpoint.rstrip('/'), set(allowed_images)
        self.instance, self.instance_uid = dns_name(instance), instance_uid
        if not self.images:
            raise ValueError('Kubernetes execution requires at least one allowed image')
        self.jobs = namespace_path(namespace, 'batch/v1') + '/jobs'
        self.selector = f'app.kubernetes.io/managed-by=localcloud-executor,localcloud.dev/instance={instance}'

    @staticmethod
    def describe(job):
        status = job.get('status', {})
        conditions = {c['type']: c.get('status') for c in status.get('conditions', [])}
        phase = 'Complete' if conditions.get('Complete') == 'True' else ('Failed' if conditions.get('Failed') == 'True' else ('Running' if status.get('active') else 'Pending'))
        return {'name': job['metadata']['name'], 'phase': phase, 'status': status}

    def submit(self, data):
        import uuid
        if not isinstance(data, dict) or set(data) - {'image', 'command', 'env', 'timeoutSeconds', 'ttlSeconds'}:
            raise Error('InvalidExecution', 'Unknown execution fields')
        image, command = data.get('image'), data.get('command')
        if image not in self.images:
            raise Error('InvalidExecution', 'Image is not in the executor allowlist', 403)
        if not isinstance(command, list) or not command or len(command) > 100 or any(not isinstance(v, str) or '\x00' in v or len(v) > 8192 for v in command):
            raise Error('InvalidExecution', 'command must be a nonempty array of strings')
        env = data.get('env', {})
        if not isinstance(env, dict) or len(env) > 100 or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or not isinstance(v, str) or '\x00' in v or len(v) > 8192 for k, v in env.items()):
            raise Error('InvalidExecution', 'env must map variable names to short strings')
        if any(k.startswith(('LOCALCLOUD_', 'AWS_')) for k in env):
            raise Error('InvalidExecution', 'LOCALCLOUD_ and AWS_ variables are reserved')
        timeout, ttl = data.get('timeoutSeconds', 300), data.get('ttlSeconds', 600)
        if type(timeout) is not int or not 1 <= timeout <= 3600 or type(ttl) is not int or not 30 <= ttl <= 86400:
            raise Error('InvalidExecution', 'Timeout must be 1–3600 seconds and TTL 30–86400 seconds')
        name = 'lc-run-' + uuid.uuid4().hex[:16]
        labels = {'app.kubernetes.io/managed-by': 'localcloud-executor', 'localcloud.dev/instance': self.instance}
        metadata = {'name': name, 'namespace': self.namespace, 'labels': labels}
        if self.instance_uid:
            metadata['ownerReferences'] = [{'apiVersion': 'localcloud.dev/v1alpha1', 'kind': 'LocalCloudInstance', 'name': self.instance, 'uid': self.instance_uid}]
        variables = {'AWS_ENDPOINT_URL': self.endpoint, 'LOCALCLOUD_URL': self.endpoint,
                     'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test', **env}
        job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': metadata, 'spec': {
            'backoffLimit': 0, 'activeDeadlineSeconds': timeout, 'ttlSecondsAfterFinished': ttl,
            'template': {'metadata': {'labels': labels}, 'spec': {
                'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'containers': [{'name': 'workload', 'image': image, 'command': command,
                    'env': [{'name': k, 'value': v} for k, v in variables.items()],
                    'resources': {'requests': {'cpu': '100m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}},
                    'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                    'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'}]}],
                'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}}}}
        return self.describe(self.client.request('POST', self.jobs, job))

    def list(self):
        return [self.describe(j) for j in self.client.list(self.jobs, self.selector)]

    def owned_job(self, name):
        dns_name(name)
        job = self.client.get(self.jobs + '/' + name)
        labels = job.get('metadata', {}).get('labels', {})
        if labels.get('app.kubernetes.io/managed-by') != 'localcloud-executor' or labels.get('localcloud.dev/instance') != self.instance:
            raise Error('AccessDenied', 'Job is not owned by this executor instance', 403)
        if self.instance_uid and not any(r.get('uid') == self.instance_uid for r in job.get('metadata', {}).get('ownerReferences', [])):
            raise Error('AccessDenied', 'Job belongs to a different instance generation', 403)
        return job

    def status(self, name):
        return self.describe(self.owned_job(name))

    def logs(self, name):
        self.owned_job(name)
        pods = self.client.list(namespace_path(self.namespace) + '/pods', 'job-name=' + name)
        if not pods:
            return {'name': name, 'logs': '', 'pending': True}
        pod = pods[0]
        # Verify job ownership, rather than trusting only a pod label.
        job_uid = self.owned_job(name)['metadata']['uid']
        if not any(r.get('uid') == job_uid for r in pod['metadata'].get('ownerReferences', [])):
            raise Error('AccessDenied', 'Pod does not belong to execution', 403)
        path = namespace_path(self.namespace) + '/pods/' + dns_name(pod['metadata']['name']) + '/log?container=workload&tailLines=200&limitBytes=65536'
        return {'name': name, 'logs': self.client.request('GET', path, text=True), 'pending': False}

    def cancel(self, name):
        job = self.owned_job(name)
        self.client.request('DELETE', self.jobs + '/' + name, {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Background', 'preconditions': {'uid': job['metadata']['uid']}})
        return {'name': name, 'deleted': True}
