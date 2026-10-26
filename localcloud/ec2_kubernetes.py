"""Owned Kubernetes runtime for the bounded EC2 provider.

This reconstructs observed Deployment/Pod behavior independently. It provides
real lifecycle and filesystem PVC mounts, not VMs, EKS or raw EBS devices.
"""
import copy
import base64
import hashlib
import json
import uuid
from urllib.parse import quote

from .engine import Error
from .kube import KubeError, dns_name, namespace_path

MANAGED = 'localcloud-ec2'


def state(name):
    return {'Name': name, 'Code': {'pending': 0, 'running': 16, 'shutting-down': 32, 'stopping': 64, 'stopped': 80, 'terminated': 48}[name]}


class EC2Kubernetes:
    def __init__(self, ec2, client, namespace, helper_image=None, image_pull_policy='IfNotPresent'):
        if image_pull_policy not in ('IfNotPresent', 'Always', 'Never'):
            raise ValueError('Unsupported EC2 Kubernetes image pull policy')
        self.ec2, self.client = ec2, client
        self.namespace, self.helper_image = dns_name(namespace), helper_image
        self.image_pull_policy = image_pull_policy
        self.base = namespace_path(self.namespace)
        self.apps = namespace_path(self.namespace, 'apps/v1')
        self.owner = ec2.runtime.owner
        ec2.engine.db.execute('CREATE TABLE IF NOT EXISTS ec2_kubernetes_pvcs (path TEXT PRIMARY KEY, uid TEXT NOT NULL, owner TEXT NOT NULL)')
        ec2.engine.db.commit()
        from .ec2_kube_journal import KubernetesJournal
        self.journal = KubernetesJournal(self)

    def name(self, item):
        return dns_name('lc-ec2-' + self.owner[:8] + '-' + item['InstanceId'])

    def labels(self, item=None, key=None):
        labels = {'app.kubernetes.io/managed-by': MANAGED, 'localcloud.dev/runtime': self.owner}
        if item:
            labels.update({'localcloud.dev/ec2-instance': item['InstanceId'], 'localcloud.dev/ec2-owner': item['Owner']})
        if key:
            labels['localcloud.dev/storage'] = key
        return labels

    def metadata(self, name, item=None, key=None):
        return {'name': name, 'namespace': self.namespace, 'labels': self.labels(item, key)}

    def owned(self, path, item=None, key=None, uid=None, absent=False):
        try:
            obj = self.client.get(path)
        except KubeError as error:
            if absent and error.status == 404:
                return None
            raise
        labels = obj.get('metadata', {}).get('labels', {})
        if any(labels.get(k) != v for k, v in self.labels(item, key).items()):
            raise Error('RuntimeFailure', 'Kubernetes resource ownership mismatch', 409)
        if uid and obj['metadata'].get('uid') != uid:
            raise Error('RuntimeFailure', 'Kubernetes resource identity changed', 409)
        return obj

    def delete(self, path, item=None, key=None, uid=None):
        obj = self.owned(path, item, key, uid, absent=True)
        if obj is not None:
            self.journal.delete(path, obj, self.labels(item, key))

    def create(self, collection, body, item=None, key=None):
        # POST conflict never turns into an update of an existing object.
        try:
            return self.journal.create(collection, body, self.labels(item, key))
        except KubeError as error:
            if error.status == 409:
                raise Error('RuntimeFailure', 'Kubernetes resource name already exists; no existing resource was changed', 409) from error
            raise

    def update(self, path, desired, item):
        before = self.owned(path, item, uid=desired['metadata'].get('uid'))
        return self.journal.update(path, before, desired, self.labels(item))

    def volume_name(self, key):
        return dns_name('lc-ebs-' + self.owner[:8] + '-' + key)

    def create_volume(self, key, source=None, size=1):
        if source is not None:
            raise Error('UnsupportedOperation', 'Kubernetes volume snapshot/copy requires a separate storage adapter', 501)
        if type(size) is not int or not 1 <= size <= 1000:
            raise Error('InvalidParameterValue', 'PVC size must be 1 through 1000 GiB')
        obj = self.create(self.base + '/persistentvolumeclaims', {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                    'metadata': self.metadata(self.volume_name(key), key=key), 'spec': {'accessModes': ['ReadWriteOnce'],
                    'resources': {'requests': {'storage': str(size) + 'Gi'}}}}, key=key)
        self.ec2.engine.db.execute('INSERT INTO ec2_kubernetes_pvcs VALUES (?, ?, ?)', (self.base + '/persistentvolumeclaims/' + self.volume_name(key), obj['metadata']['uid'], self.owner))

    def _volume_uid(self, key):
        path = self.base + '/persistentvolumeclaims/' + self.volume_name(key)
        row = self.ec2.engine.db.execute('SELECT uid FROM ec2_kubernetes_pvcs WHERE path=? AND owner=?', (path, self.owner)).fetchone()
        if not row:
            raise Error('RuntimeFailure', 'PVC identity is not recorded for this runtime', 409)
        return row[0]

    def delete_volume(self, key):
        path = self.base + '/persistentvolumeclaims/' + self.volume_name(key)
        self.delete(path, key=key, uid=self._volume_uid(key))
        self.ec2.engine.db.execute('DELETE FROM ec2_kubernetes_pvcs WHERE path=? AND owner=?', (path, self.owner))

    def _mounts(self, mappings):
        mounts, volumes = [{'name': 'tmp', 'mountPath': '/tmp'}], [{'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]
        for index, mapping in enumerate(mappings):
            key = mapping['Ebs']['VolumeId']
            self.owned(self.base + '/persistentvolumeclaims/' + self.volume_name(key), key=key, uid=self._volume_uid(key))
            target = mapping['DeviceName']
            if target.startswith('/dev/'):
                target = '/mnt/ebs/' + target[5:]
            name = 'ebs-' + str(index)
            volumes.append({'name': name, 'persistentVolumeClaim': {'claimName': self.volume_name(key)}})
            mounts.append({'name': name, 'mountPath': target})
        return mounts, volumes

    @staticmethod
    def _container_security():
        return {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}

    def _metadata_payload(self, item):
        return {k: copy.deepcopy(item.get(k)) for k in ('InstanceId', 'ImageId', 'InstanceType', 'PrivateIpAddress',
                'PrivateDnsName', 'PublicIpAddress', 'Placement', 'UserData', 'MetadataOptions', 'ReservationId', 'AmiLaunchIndex')}

    def _config(self, item, script):
        return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': self.metadata(self.name(item), item),
                'data': {'user-data': script, 'metadata.json': json.dumps(self._metadata_payload(item))}}

    def _pod(self, item, script, mappings=None):
        image = self.ec2.images.get(item['ImageId'])
        if not image:
            raise Error('InvalidAMIID.NotFound', 'Image is not in the configured Kubernetes allowlist')
        ports = item.get('PortMappings', [])
        if any(p.get('HostPort', 0) for p in ports):
            raise Error('UnsupportedOperation', 'Kubernetes uses ClusterIP service ports; use HostPort 0 then kubectl port-forward', 501)
        mounts, volumes = self._mounts(mappings if mappings is not None else item.get('BlockDeviceMappings', []))
        kind = item['InstanceType']
        if kind not in ('t3.micro', 't3.small', 't3.medium'):
            raise Error('InvalidParameterValue', 'Unsupported Kubernetes instance type')
        container = {'name': 'guest', 'image': image, 'imagePullPolicy': self.image_pull_policy,
                     'resources': {'requests': {'cpu': '100m', 'memory': '64Mi'},
                                   'limits': {'cpu': '1', 'memory': {'t3.micro': '128Mi', 't3.small': '256Mi', 't3.medium': '512Mi'}[kind]}},
                     'securityContext': self._container_security(), 'volumeMounts': mounts}
        if ports:
            container['ports'] = [{'name': 'tcp-' + str(p['ContainerPort']), 'containerPort': p['ContainerPort']} for p in ports]
            container['readinessProbe'] = {'tcpSocket': {'port': ports[0]['ContainerPort']}, 'initialDelaySeconds': 1, 'periodSeconds': 2}
        pod = {'automountServiceAccountToken': False, 'terminationGracePeriodSeconds': 5,
               'securityContext': {'runAsNonRoot': True, 'runAsUser': 65534, 'runAsGroup': 65534,
                                   'fsGroup': 65534, 'seccompProfile': {'type': 'RuntimeDefault'}},
               'containers': [container], 'volumes': volumes}
        if script:
            pod['volumes'].append({'name': 'bootstrap', 'configMap': {'name': self.name(item)}})
            pod['initContainers'] = [{'name': 'user-data', 'image': image, 'imagePullPolicy': self.image_pull_policy,
                    'command': ['/bin/sh', '-c', 'set -eu; /bin/sh /localcloud-init/user-data'],
                    'securityContext': self._container_security(), 'resources': copy.deepcopy(container['resources']),
                    'volumeMounts': [*copy.deepcopy(mounts), {'name': 'bootstrap', 'mountPath': '/localcloud-init', 'readOnly': True}]}]
        if self.helper_image:
            endpoint_env = [{'name': 'AWS_EC2_METADATA_SERVICE_ENDPOINT', 'value': 'http://127.0.0.1:1338'},
                            {'name': 'LOCALCLOUD_METADATA_URL', 'value': 'http://127.0.0.1:1338'}]
            container['env'] = copy.deepcopy(endpoint_env)
            for init in pod.get('initContainers', []):
                init['env'] = copy.deepcopy(endpoint_env)
            probe = {'exec': {'command': ['python3', '-c', "import socket;s=socket.create_connection(('127.0.0.1',1338),1);s.close()"]},
                     'periodSeconds': 1, 'timeoutSeconds': 2}
            # A native sidecar starts before later init containers. Its startup
            # probe gates user-data without making IMDS HTTP authorization or
            # HttpEndpoint=disabled prevent the Pod itself from starting.
            metadata = {'name': 'metadata', 'image': self.helper_image, 'imagePullPolicy': self.image_pull_policy,
                    'restartPolicy': 'Always',
                    'command': ['python3', '-m', 'localcloud.ec2_kubernetes', '--metadata'],
                    'env': [{'name': 'LOCALCLOUD_IMDS_DATA', 'valueFrom': {'configMapKeyRef': {'name': self.name(item), 'key': 'metadata.json'}}}],
                    'securityContext': self._container_security(), 'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '100m', 'memory': '64Mi'}},
                    'startupProbe': {**copy.deepcopy(probe), 'failureThreshold': 60},
                    'readinessProbe': copy.deepcopy(probe)}
            from .ec2_profiles import broker_environment
            metadata['env'].extend({'name': key, 'value': value} for key, value in broker_environment(self.ec2, item).items())
            pod['initContainers'] = [metadata, *pod.get('initContainers', [])]
        return pod

    def launch(self, item, decoded_userdata):
        if item.get('KeyName') or item.get('GuestMode'):
            raise Error('UnsupportedOperation', 'Kubernetes EC2 does not provide guest-root mode or SSH key injection', 501)
        try:
            script = decoded_userdata.decode('utf-8')
        except UnicodeDecodeError:
            raise Error('InvalidParameterValue', 'Kubernetes UserData must be UTF-8') from None
        name = self.name(item)
        item['KubernetesDesiredState'] = 'running'
        deployment = self.desired_deployment(item, script)
        created = []
        try:
            cm = self.create(self.base + '/configmaps', self._config(item, script), item)
            created.append((self.base + '/configmaps/' + name, cm['metadata']['uid']))
            item['KubernetesConfigUid'] = cm['metadata']['uid']
            dep = self.create(self.apps + '/deployments', deployment, item)
            created.append((self.apps + '/deployments/' + name, dep['metadata']['uid']))
            item['KubernetesUid'] = dep['metadata']['uid']
            item['KubernetesName'] = name
            item['KubernetesNamespace'] = self.namespace
            ports = item.get('PortMappings', [])
            if ports:
                service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': self.metadata(name, item),
                           'spec': {'type': 'ClusterIP', 'selector': self.labels(item),
                                    'ports': [{'name': 'tcp-' + str(p['ContainerPort']), 'port': p['ContainerPort'], 'targetPort': p['ContainerPort']} for p in ports]}}
                svc = self.create(self.base + '/services', service, item)
                created.append((self.base + '/services/' + name, svc['metadata']['uid']))
                item['KubernetesServiceUid'] = svc['metadata']['uid']
            item['State'] = state('pending')
            item['Endpoints'] = self.endpoints(item)
        except Exception as failure:
            for path, uid in reversed(created):
                try:
                    self.delete(path, item, uid=uid)
                except Error as error:
                    failure.add_note('Kubernetes launch cleanup failed: ' + str(error))
            raise

    def endpoints(self, item):
        name = self.name(item)
        return [f'http://{name}.{self.namespace}.svc:{p["ContainerPort"]}' for p in item.get('PortMappings', [])]

    def desired_deployment(self, item, script):
        config = self._config(item, script)['data']
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': self.metadata(self.name(item), item),
                'spec': {'replicas': 0 if item.get('KubernetesDesiredState') == 'stopped' else 1,
                         'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': self.labels(item)},
                         'template': {'metadata': {'labels': self.labels(item),
                                      'annotations': {'localcloud.dev/config-sha': digest}}, 'spec': self._pod(item, script)}}}

    @staticmethod
    def includes(actual, desired):
        if isinstance(desired, dict):
            return isinstance(actual, dict) and all(k in actual and EC2Kubernetes.includes(actual[k], v) for k, v in desired.items())
        if isinstance(desired, list):
            return isinstance(actual, list) and len(actual) == len(desired) and all(EC2Kubernetes.includes(a, b) for a, b in zip(actual, desired))
        return actual == desired

    def reconcile(self, item):
        """Repair only absent owned runtime objects, preserving recorded PVC data."""
        desired_state = item.setdefault('KubernetesDesiredState', 'stopped' if item['State']['Name'] in ('stopped', 'stopping') else 'running')
        if desired_state == 'terminated':
            remaining = False
            for group, collection, field in ((self.apps, 'deployments', 'KubernetesUid'),
                                             (self.base, 'services', 'KubernetesServiceUid'),
                                             (self.base, 'configmaps', 'KubernetesConfigUid')):
                if self.owned(group + '/' + collection + '/' + self.name(item), item, uid=item.get(field), absent=True) is not None:
                    remaining = True
            for mapping in item.get('BlockDeviceMappings', []):
                if mapping['Ebs'].get('DeleteOnTermination'):
                    key = mapping['Ebs']['VolumeId']
                    if self.owned(self.base + '/persistentvolumeclaims/' + self.volume_name(key), key=key, absent=True) is not None:
                        remaining = True
            item['State'] = state('shutting-down' if remaining or self.journal.pending_for(item) else 'terminated')
            return None
        if self.journal.pending_for(item):
            item['State'] = state('pending')
            item['RuntimeStatusReason'] = 'Kubernetes recovery is pending; inspect kubernetesRecovery'
            return None
        for mapping in item.get('BlockDeviceMappings', []):
            key = mapping['Ebs']['VolumeId']
            pvc = self.owned(self.base + '/persistentvolumeclaims/' + self.volume_name(key), key=key, uid=self._volume_uid(key), absent=True)
            if pvc is None or pvc['metadata'].get('deletionTimestamp'):
                item['State'] = state('pending')
                item['RuntimeStatusReason'] = 'Persistent volume is missing or deleting; empty replacement is not created'
                return None
        script = base64.b64decode(item.get('UserData', '')).decode('utf-8')
        name = self.name(item)
        cm_path = self.base + '/configmaps/' + name
        cm = self.owned(cm_path, item, uid=item.get('KubernetesConfigUid'), absent=True)
        desired_cm = self._config(item, script)
        if cm is None:
            cm = self.create(self.base + '/configmaps', desired_cm, item)
            item['KubernetesConfigUid'] = cm['metadata']['uid']
        elif cm.get('data') != desired_cm['data']:
            cm['data'] = desired_cm['data']
            self.update(cm_path, cm, item)
        deployment = self._deployment(item, absent=True)
        desired = self.desired_deployment(item, script)
        if deployment is None:
            deployment = self.create(self.apps + '/deployments', desired, item)
            item['KubernetesUid'] = deployment['metadata']['uid']
            item.pop('PodName', None); item.pop('PodUid', None)
        elif not self.includes(deployment['spec'], desired['spec']):
            deployment['spec']['replicas'] = desired['spec']['replicas']
            deployment['spec']['template'] = desired['spec']['template']
            deployment = self.update(self.apps + '/deployments/' + name, deployment, item)
        if item.get('PortMappings'):
            path = self.base + '/services/' + name
            service = self.owned(path, item, uid=item.get('KubernetesServiceUid'), absent=True)
            desired_ports = [{'name': 'tcp-' + str(p['ContainerPort']), 'port': p['ContainerPort'], 'targetPort': p['ContainerPort']} for p in item['PortMappings']]
            if service is None:
                service = self.create(self.base + '/services', {'apiVersion': 'v1', 'kind': 'Service',
                        'metadata': self.metadata(name, item), 'spec': {'type': 'ClusterIP', 'selector': self.labels(item), 'ports': desired_ports}}, item)
                item['KubernetesServiceUid'] = service['metadata']['uid']
            elif not self.includes(service['spec'], {'selector': self.labels(item), 'ports': desired_ports}):
                service['spec']['selector'] = self.labels(item); service['spec']['ports'] = desired_ports
                self.update(path, service, item)
        return deployment

    def _deployment(self, item, absent=False):
        if item.get('KubernetesNamespace', self.namespace) != self.namespace:
            raise Error('RuntimeFailure', 'Instance belongs to a different Kubernetes namespace', 409)
        return self.owned(self.apps + '/deployments/' + self.name(item), item, uid=item.get('KubernetesUid'), absent=absent)

    def _pods(self, item, deployment):
        selector = ','.join(k + '=' + v for k, v in self.labels(item).items())
        pods = self.client.list(self.base + '/pods', selector)
        owned = []
        for pod in pods:
            if any(pod.get('metadata', {}).get('labels', {}).get(k) != v for k, v in self.labels(item).items()):
                continue
            for reference in pod.get('metadata', {}).get('ownerReferences', []):
                if reference.get('kind') != 'ReplicaSet':
                    continue
                try:
                    rs = self.owned(self.apps + '/replicasets/' + dns_name(reference['name']), item, uid=reference.get('uid'))
                except KubeError as error:
                    if error.status == 404:
                        continue
                    raise
                if any(r.get('uid') == deployment['metadata']['uid'] and r.get('kind') == 'Deployment' for r in rs['metadata'].get('ownerReferences', [])):
                    owned.append(pod)
                    break
        return owned

    def refresh(self, item):
        if item['State']['Name'] == 'terminated':
            return item
        item.pop('RuntimeStatusReason', None)
        deployment = self.reconcile(item)
        if item.get('KubernetesDesiredState') == 'terminated' or item.get('RuntimeStatusReason'):
            return item
        if deployment is None:
            item['State'] = state('pending')
            item['RuntimeStatusReason'] = 'Kubernetes Deployment is missing'
            item.pop('PodName', None); item.pop('PodUid', None)
            return item
        pods = self._pods(item, deployment)
        if deployment['spec'].get('replicas', 1) == 0:
            item['State'] = state('stopping' if pods else 'stopped')
        else:
            generation = deployment['metadata'].get('generation', 1)
            observed = deployment.get('status', {}).get('observedGeneration', 0) >= generation
            ready = [p for p in pods if not p['metadata'].get('deletionTimestamp') and p.get('status', {}).get('phase') == 'Running'
                     and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in p.get('status', {}).get('conditions', []))]
            item['State'] = state('running' if observed and ready else 'pending')
            selected = ready[0] if ready else next(iter(pods), None)
            if selected:
                item['PodName'] = selected['metadata']['name']; item['PodUid'] = selected['metadata']['uid']
                for condition in selected.get('status', {}).get('conditions', []):
                    if condition.get('status') == 'False' and condition.get('reason'):
                        item['RuntimeStatusReason'] = condition['reason'] + ': ' + condition.get('message', '')[:1024]
                for status in selected.get('status', {}).get('initContainerStatuses', []) + selected.get('status', {}).get('containerStatuses', []):
                    waiting = status.get('state', {}).get('waiting', {})
                    terminated = status.get('state', {}).get('terminated', {})
                    if waiting.get('reason') or terminated.get('exitCode', 0):
                        item['RuntimeStatusReason'] = waiting.get('reason') or 'Container exit code ' + str(terminated['exitCode'])
            else:
                item.pop('PodName', None); item.pop('PodUid', None)
                for condition in deployment.get('status', {}).get('conditions', []):
                    if condition.get('status') == 'True' and condition.get('type') == 'ReplicaFailure':
                        item['RuntimeStatusReason'] = condition.get('reason', 'ReplicaFailure') + ': ' + condition.get('message', '')[:1024]
        item['Endpoints'] = self.endpoints(item)
        return item

    def lifecycle(self, item, action):
        if action == 'terminate':
            item['KubernetesDesiredState'] = 'terminated'
            dep = self._deployment(item, absent=True)
            if dep is not None:
                self.delete(self.apps + '/deployments/' + self.name(item), item, uid=item.get('KubernetesUid'))
            for collection in ('services', 'configmaps'):
                uid = item.get('KubernetesServiceUid' if collection == 'services' else 'KubernetesConfigUid')
                self.delete(self.base + '/' + collection + '/' + self.name(item), item, uid=uid)
            item['State'] = state('shutting-down')
            return
        deployment = self._deployment(item)
        if action in ('start', 'stop'):
            item['KubernetesDesiredState'] = 'running' if action == 'start' else 'stopped'
            deployment['spec']['replicas'] = 1 if action == 'start' else 0
        elif action == 'reboot':
            deployment['spec']['template']['metadata'].setdefault('annotations', {})['localcloud.dev/reboot'] = uuid.uuid4().hex
        else:
            raise Error('UnsupportedOperation', 'Unknown Kubernetes lifecycle action', 501)
        self.update(self.apps + '/deployments/' + self.name(item), deployment, item)
        item['State'] = state('pending' if action != 'stop' else 'stopping')

    def console(self, item):
        deployment = self._deployment(item, absent=True)
        if deployment is None:
            return ''
        outputs = []
        for pod in self._pods(item, deployment):
            for name in ('user-data', 'guest'):
                if name == 'user-data' and not any(c.get('name') == name for c in pod.get('spec', {}).get('initContainers', [])):
                    continue
                path = self.base + '/pods/' + dns_name(pod['metadata']['name']) + '/log?container=' + quote(name) + '&tailLines=200&limitBytes=65536'
                try:
                    outputs.append(self.client.request('GET', path, text=True))
                except KubeError as error:
                    if error.status not in (400, 404): raise
        return '\n'.join(outputs)[-65536:]

    def check_reconfigure(self, item):
        deployment = self._deployment(item)
        if item['State']['Name'] != 'stopped' or deployment['spec'].get('replicas', 1) != 0 or self._pods(item, deployment):
            raise Error('IncorrectInstanceState', 'Stop the Kubernetes instance and wait for its Pods to terminate before changing volumes or type')

    def reconfigure(self, item, mappings):
        self.check_reconfigure(item)
        deployment = self._deployment(item)
        mounts, volumes = self._mounts(mappings)
        template = deployment['spec']['template']['spec']
        config = [v for v in template['volumes'] if v['name'] == 'bootstrap']
        template['volumes'] = [*volumes, *config]
        template['containers'][0]['volumeMounts'] = mounts
        for init in template.get('initContainers', []):
            if init['name'] == 'user-data':
                init['volumeMounts'] = [*copy.deepcopy(mounts), {'name': 'bootstrap', 'mountPath': '/localcloud-init', 'readOnly': True}]
        self.update(self.apps + '/deployments/' + self.name(item), deployment, item)

    def modify_type(self, item, kind):
        self.check_reconfigure(item)
        deployment = self._deployment(item)
        if kind not in ('t3.micro', 't3.small', 't3.medium'):
            raise Error('InvalidParameterValue', 'Unsupported Kubernetes instance type')
        deployment['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'] = {'t3.micro': '128Mi', 't3.small': '256Mi', 't3.medium': '512Mi'}[kind]
        for init in deployment['spec']['template']['spec'].get('initContainers', []):
            if init['name'] == 'user-data':
                init['resources']['limits']['memory'] = {'t3.micro': '128Mi', 't3.small': '256Mi', 't3.medium': '512Mi'}[kind]
        cm_path = self.base + '/configmaps/' + self.name(item)
        cm = self.owned(cm_path, item, uid=item.get('KubernetesConfigUid'))
        previous = copy.deepcopy(cm)
        payload = json.loads(cm['data']['metadata.json'])
        payload['InstanceType'] = kind
        cm['data']['metadata.json'] = json.dumps(payload)
        deployment['spec']['template']['metadata'].setdefault('annotations', {})['localcloud.dev/config-sha'] = hashlib.sha256(json.dumps(cm['data'], sort_keys=True).encode()).hexdigest()
        updated = self.update(cm_path, cm, item)
        try:
            self.update(self.apps + '/deployments/' + self.name(item), deployment, item)
        except Error as failure:
            try:
                previous['metadata']['resourceVersion'] = updated['metadata']['resourceVersion']
                self.update(cm_path, previous, item)
            except Error as cleanup_error:
                failure.add_note('Kubernetes metadata rollback failed: ' + str(cleanup_error))
            raise

    def update_metadata(self, item, options):
        if not self.helper_image:
            raise Error('UnsupportedOperation', 'Configure an EC2 Kubernetes helper image for instance metadata', 501)
        path = self.base + '/configmaps/' + self.name(item)
        cm = self.owned(path, item, uid=item.get('KubernetesConfigUid'))
        previous = copy.deepcopy(cm)
        payload = self._metadata_payload(item); payload['MetadataOptions'] = copy.deepcopy(options)
        cm['data']['metadata.json'] = json.dumps(payload)
        deployment = self._deployment(item)
        deployment['spec']['template']['metadata'].setdefault('annotations', {})['localcloud.dev/config-sha'] = hashlib.sha256(json.dumps(cm['data'], sort_keys=True).encode()).hexdigest()
        updated = self.update(path, cm, item)
        deployment['spec']['template']['metadata'].setdefault('annotations', {})['localcloud.dev/metadata'] = uuid.uuid4().hex
        try:
            self.update(self.apps + '/deployments/' + self.name(item), deployment, item)
        except Error as failure:
            try:
                previous['metadata']['resourceVersion'] = updated['metadata']['resourceVersion']
                self.update(path, previous, item)
            except Error as cleanup_error:
                failure.add_note('Kubernetes metadata rollback failed: ' + str(cleanup_error))
            raise


def metadata_server():
    """Unprivileged Pod-local IMDS endpoint; no host or link-local rewriting."""
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    from .ec2_metadata import Metadata, broker_provider
    metadata = Metadata(json.loads(os.environ['LOCALCLOUD_IMDS_DATA']), broker_provider())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def dispatch(self):
            status, body = metadata.request(self.command, urlsplit(self.path).path, self.headers)
            if isinstance(body, str): body = body.encode()
            self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        do_GET = do_PUT = do_POST = do_DELETE = dispatch
    ThreadingHTTPServer(('127.0.0.1', 1338), Handler).serve_forever()


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--metadata']:
        metadata_server()
