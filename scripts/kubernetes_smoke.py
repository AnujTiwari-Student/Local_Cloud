"""Live integration checks for a disposable Kubernetes cluster.

Uses the current kubectl context. Run only against a development/test cluster.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from localcloud.ci import HTTPClient, smoke
from localcloud.kube_manifests import installation
from localcloud.operator import instance

CONTEXT = []

def kubectl(*args, body=None, timeout=240):
    return subprocess.run(['kubectl', *CONTEXT, *args], input=json.dumps(body) if body is not None else None,
                          text=True, check=True, timeout=timeout, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', default='localcloud-ci')
    parser.add_argument('--image', default='localcloud:ci')
    parser.add_argument('--artifacts', default='artifacts/kubernetes')
    parser.add_argument('--context', help='Explicit kubectl context for every operation')
    parser.add_argument('--existing-crd', action='store_true', help='Use the installed CRD without modifying it')
    args = parser.parse_args()
    CONTEXT[:] = ['--context', args.context] if args.context else []
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    name = 'ci-' + uuid.uuid4().hex[:10]
    secret = name + '-access'
    token = secrets.token_urlsafe(32)
    forward = log = client = None
    secret_created = instance_created = False
    report = {'passed': False}
    try:
        manifests = installation(args.namespace, args.image)
        if args.existing_crd:
            kubectl('get', 'crd/localcloudinstances.localcloud.dev')
            manifests['items'] = [item for item in manifests['items'] if item['kind'] != 'CustomResourceDefinition']
        kubectl('apply', '-f', '-', body=manifests)
        kubectl('wait', '--for=condition=Established', 'crd/localcloudinstances.localcloud.dev', '--timeout=60s')
        kubectl('rollout', 'status', '-n', args.namespace, 'deployment/localcloud-operator', '--timeout=180s')
        kubectl('create', '-f', '-', body={'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': secret, 'namespace': args.namespace}, 'type': 'Opaque', 'stringData': {'token': token}})
        secret_created = True
        resource = instance(name, args.namespace, args.image, secret, ttl=600, persistent=False)
        kubectl('create', '-f', '-', body=resource)
        instance_created = True
        kubectl('wait', '-n', args.namespace, '--for=jsonpath={.status.phase}=Ready', 'localcloudinstance/' + name, '--timeout=180s')
        log = (artifacts / 'port-forward.log').open('w')
        # Ask kubectl to choose a free loopback port to avoid colliding with other CI jobs.
        forward = subprocess.Popen(['kubectl', *CONTEXT, '-n', args.namespace, 'port-forward', 'service/' + name, ':4566', '--address=127.0.0.1'], stdout=log, stderr=subprocess.STDOUT)
        import re
        for _ in range(120):
            log.flush()
            output = (artifacts / 'port-forward.log').read_text()
            match = re.search(r'Forwarding from 127\.0\.0\.1:(\d+)', output)
            if match:
                client = HTTPClient('http://127.0.0.1:' + match[1], token)
                try:
                    client.request('GET', '/_local/health')
                    break
                except OSError:
                    pass
            if forward.poll() is not None:
                raise RuntimeError('kubectl port-forward exited; see port-forward.log')
            time.sleep(0.5)
        else:
            raise RuntimeError('Port-forward readiness timed out')
        report['checks'] = smoke(client)
        execution = client.request('POST', '/_local/executions', {
            'image': args.image, 'command': ['python3', '-c', "import os,urllib.request; print(urllib.request.urlopen(os.environ['LOCALCLOUD_URL']+'/_local/health').read().decode())"],
            'timeoutSeconds': 120, 'ttlSeconds': 300})
        for _ in range(120):
            status = client.request('GET', '/_local/executions/' + execution['name'])
            if status['phase'] in ('Complete', 'Failed'):
                break
            time.sleep(1)
        else:
            raise RuntimeError('Workload Job did not finish')
        job_logs = client.request('GET', '/_local/executions/' + execution['name'] + '/logs')
        (artifacts / 'workload-logs.json').write_text(json.dumps(job_logs, indent=2))
        if status['phase'] != 'Complete' or 'running' not in job_logs['logs']:
            raise RuntimeError('Pod executor failed or did not reach the instance endpoint')
        report['checks'].append('kubernetes-job-endpoint-roundtrip')
        client.request('DELETE', '/_local/executions/' + execution['name'])
        report['passed'] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Avoid serializing subprocess stdin, which can contain the generated token.
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if client:
            try:
                (artifacts / 'traces.json').write_text(json.dumps(client.request('GET', '/_local/traces'), indent=2))
            except OSError:
                pass
        if forward:
            forward.terminate()
            try:
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.wait()
        if log:
            log.close()
        try:
            diagnostics = kubectl('get', '-n', args.namespace, 'pods,deployments,jobs,localcloudinstances', '-o', 'json', timeout=30)
            (artifacts / 'cluster.json').write_text(diagnostics)
        except (OSError, subprocess.SubprocessError):
            pass
        for kind, resource_name, created in [('localcloudinstance', name, instance_created), ('secret', secret, secret_created)]:
            if created:
                try:
                    kubectl('delete', '-n', args.namespace, kind, resource_name, '--ignore-not-found', '--wait=false', timeout=30)
                except (OSError, subprocess.SubprocessError) as error:
                    report['cleanup_error'] = str(error)
                    report['passed'] = False
        (artifacts / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
