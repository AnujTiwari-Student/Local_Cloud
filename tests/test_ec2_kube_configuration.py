"""EC2 Kubernetes execution must be explicit and separate from generic Jobs."""
import contextlib
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from localcloud.__main__ import main
from localcloud.engine import Error
from localcloud.kube_manifests import installation
from localcloud.operator import desired_resources, instance


class Ec2KubeConfigurationTests(unittest.TestCase):
    def resource(self):
        item = instance('dev', 'localcloud', 'localcloud:dev', 'access')
        item['metadata']['uid'] = 'instance-uid'
        item['spec']['workloadImages'] = []
        return item

    def test_ec2_without_jobs_uses_executor_identity_and_explicit_image_arguments(self):
        item = self.resource()
        item['spec']['ec2'] = {'enabled': True, 'images': {'ami-web': 'web:dev'}, 'pullPolicy': 'Never', 'helperImage': 'localcloud:dev'}
        deployed = next(r for r in desired_resources(item) if r['kind'] == 'Deployment')
        spec = deployed['spec']['template']['spec']
        self.assertEqual(spec['serviceAccountName'], 'localcloud-executor')
        self.assertTrue(spec['automountServiceAccountToken'])
        args = spec['containers'][0]['args']
        self.assertNotIn('--executor', args)
        self.assertEqual(args[args.index('--ec2-executor') + 1], 'kubernetes')
        self.assertEqual(args[args.index('--ec2-image') + 1], 'ami-web=web:dev')
        self.assertEqual(args[args.index('--ec2-kube-image-pull-policy') + 1], 'Never')

    def test_disabled_ec2_does_not_enable_jobs_or_serviceaccount(self):
        item = self.resource()
        item['spec']['ec2'] = {'enabled': False, 'images': {'ami-web': 'web:dev'}}
        spec = next(r for r in desired_resources(item) if r['kind'] == 'Deployment')['spec']['template']['spec']
        self.assertEqual(spec['serviceAccountName'], 'default')
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertNotIn('--ec2-executor', spec['containers'][0]['args'])

    def test_invalid_ec2_configuration_rejected_before_rendering(self):
        invalid = [{'enabled': True}, {'enabled': 'yes'}, {'enabled': True, 'images': {'not-ami': 'web:dev'}},
                   {'enabled': True, 'images': {'ami-web': '--privileged'}}, {'pullPolicy': 'Sometimes'},
                   {'helperImage': 'bad image'}, {'podTemplate': {}}]
        for value in invalid:
            with self.subTest(value=value):
                item = self.resource();item['spec']['ec2'] = value
                with self.assertRaises(Error):desired_resources(item)

    def test_installation_scopes_ec2_permissions_to_namespace_without_exec_or_secrets(self):
        resources = installation('localcloud', 'localcloud:dev')['items']
        role = next(r for r in resources if r['kind'] == 'Role' and r['metadata']['name'] == 'localcloud-executor')
        self.assertEqual(role['metadata']['namespace'], 'localcloud')
        names = {name for rule in role['rules'] for name in rule['resources']}
        self.assertTrue({'deployments', 'persistentvolumeclaims', 'configmaps', 'services', 'pods/log'} <= names)
        self.assertNotIn('pods/exec', names);self.assertNotIn('secrets', names)
        self.assertFalse(any(r['kind'] == 'ClusterRole' for r in resources))
        crd = next(r for r in resources if r['kind'] == 'CustomResourceDefinition')
        ec2 = crd['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']['properties']['ec2']
        self.assertEqual(ec2['properties']['images']['maxProperties'], 20)

    def test_backend_passes_kubernetes_client_configuration_without_changing_job_executor(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'LOCALCLOUD_MANAGEMENT_TOKEN': 'temporary-test-token'}, clear=True):
            engine = Mock();engine.tools.export_state.return_value = {'version': 1}
            client = Mock()
            with patch('localcloud.__main__.Engine', return_value=engine) as constructor, patch('localcloud.kube.KubeClient', return_value=client):
                main(['--data', str(Path(temporary)/'db'), '--ec2-executor', 'kubernetes', '--ec2-image', 'ami-web=web:dev',
                      '--kube-api-server', 'http://127.0.0.1:8001', '--kube-namespace', 'test-space',
                      '--ec2-kube-image-pull-policy', 'Never', '--export-state', str(Path(temporary)/'export.json')])
            options = constructor.call_args.kwargs['ec2_kubernetes']
            self.assertIs(options['client'], client)
            self.assertEqual(options['namespace'], 'test-space')
            self.assertEqual(options['image_pull_policy'], 'Never')
            engine.close.assert_called_once()

    def test_backend_rejects_missing_auth_images_and_docker_guest_flags(self):
        for flags, authenticated in [(['--ec2-executor', 'kubernetes'], True),
                                     (['--ec2-executor', 'kubernetes', '--ec2-image', 'ami-web=web:dev'], False),
                                     (['--ec2-executor', 'kubernetes', '--ec2-image', 'ami-web=web:dev', '--ec2-guest-mode'], True),
                                     (['--ec2-executor', 'kubernetes', '--ec2-image', 'ami-web=web:dev', '--ec2-imds'], True)]:
            with self.subTest(flags=flags, authenticated=authenticated), patch.dict(os.environ, {'LOCALCLOUD_MANAGEMENT_TOKEN': 'temporary-test-token'} if authenticated else {}, clear=True):
                with patch('localcloud.__main__.Engine') as constructor, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:main(flags)
                    self.assertEqual(error.exception.code, 2)
                    constructor.assert_not_called()


if __name__ == '__main__':unittest.main()
