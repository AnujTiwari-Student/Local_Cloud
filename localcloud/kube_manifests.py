"""Kubernetes installation as JSON, directly accepted by kubectl apply -f -."""
from .kube import dns_name


def installation(namespace, image):
    dns_name(namespace)
    def meta(name):
        return {'name': name, 'namespace': namespace}
    string = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    crd = {'apiVersion': 'apiextensions.k8s.io/v1', 'kind': 'CustomResourceDefinition', 'metadata': {'name': 'localcloudinstances.localcloud.dev'}, 'spec': {
        'group': 'localcloud.dev', 'scope': 'Namespaced',
        'names': {'plural': 'localcloudinstances', 'singular': 'localcloudinstance', 'kind': 'LocalCloudInstance', 'shortNames': ['lci']},
        'versions': [{'name': 'v1alpha1', 'served': True, 'storage': True, 'subresources': {'status': {}},
            'additionalPrinterColumns': [{'name': 'Phase', 'type': 'string', 'jsonPath': '.status.phase'}, {'name': 'Endpoint', 'type': 'string', 'jsonPath': '.status.endpoint'}],
            'schema': {'openAPIV3Schema': {'type': 'object', 'properties': {
                'spec': {'type': 'object', 'required': ['image', 'managementTokenSecret'], 'properties': {
                    'image': string, 'managementTokenSecret': {'type': 'string', 'pattern': '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'},
                    'iamMode': {'type': 'string', 'enum': ['off', 'soft', 'enforce'], 'default': 'soft'},
                    'ttlSeconds': {'type': 'integer', 'minimum': 60, 'maximum': 604800},
                    'workloadImages': {'type': 'array', 'maxItems': 20, 'items': string},
                    'ec2': {'type': 'object', 'properties': {
                        'enabled': {'type': 'boolean', 'default': False},
                        'images': {'type': 'object', 'maxProperties': 20, 'additionalProperties': string},
                        'pullPolicy': {'type': 'string', 'enum': ['Always', 'IfNotPresent', 'Never'], 'default': 'IfNotPresent'},
                        'helperImage': string}},
                    'persistence': {'type': 'object', 'properties': {'enabled': {'type': 'boolean', 'default': True}, 'retain': {'type': 'boolean', 'default': True}, 'size': {'type': 'string', 'pattern': '^[1-9][0-9]*(Mi|Gi)$', 'default': '1Gi'}, 'storageClassName': {'type': 'string'}}}}},
                'status': {'type': 'object', 'properties': {'phase': {'type': 'string'}, 'endpoint': {'type': 'string'}, 'message': {'type': 'string'}, 'observedGeneration': {'type': 'integer'}}}}, 'required': ['spec']}}}]}}
    accounts = [{'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': meta(name)} for name in ('localcloud-operator', 'localcloud-executor')]
    roles = [
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': meta('localcloud-operator'), 'rules': [
            {'apiGroups': ['localcloud.dev'], 'resources': ['localcloudinstances'], 'verbs': ['get', 'list', 'delete']},
            {'apiGroups': ['localcloud.dev'], 'resources': ['localcloudinstances/status'], 'verbs': ['get', 'update']},
            {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': ['get', 'create', 'update']},
            {'apiGroups': [''], 'resources': ['services', 'persistentvolumeclaims'], 'verbs': ['get', 'create', 'update']} ]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': meta('localcloud-executor'), 'rules': [
            {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': ['get', 'list', 'create', 'delete']},
            {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': ['get', 'list', 'create', 'update', 'delete']},
            {'apiGroups': ['apps'], 'resources': ['replicasets'], 'verbs': ['get', 'list']},
            {'apiGroups': [''], 'resources': ['services', 'configmaps', 'persistentvolumeclaims'], 'verbs': ['get', 'list', 'create', 'update', 'delete']},
            {'apiGroups': [''], 'resources': ['pods', 'pods/log'], 'verbs': ['get', 'list']} ]}]
    bindings = [{'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': meta(name),
                 'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': namespace}],
                 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name}}
                for name in ('localcloud-operator', 'localcloud-executor')]
    controller = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': meta('localcloud-operator'), 'spec': {
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': 'localcloud-operator'}},
        'template': {'metadata': {'labels': {'app': 'localcloud-operator'}}, 'spec': {
            'serviceAccountName': 'localcloud-operator', 'automountServiceAccountToken': True,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'operator', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                'command': ['python3', '-m', 'localcloud.operator'], 'args': ['--namespace', namespace, 'run'],
                'env': [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}],
                'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '500m', 'memory': '128Mi'}},
                'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}}]}}}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}}, crd,
        *accounts, *roles, *bindings,
        {'apiVersion': 'v1', 'kind': 'ResourceQuota', 'metadata': meta('localcloud-workloads'), 'spec': {'hard': {'count/jobs.batch': '20'}}}, controller]}
