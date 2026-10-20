#!/usr/bin/env python3
"""Isolated runtime probes for REA process capture; never use a user's state DB.

This checks LocalCloud itself. It does not establish LocalStack compatibility.
Optional --docker-image executes the selected local image on one loopback port.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localcloud.__main__ import Handler
from localcloud.client import Client
from localcloud.engine import Engine, Error
from http.server import ThreadingHTTPServer


@contextlib.contextmanager
def backend(path, images=None):
    engine = Engine(str(path), ec2_images=images)
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    engine.endpoint = f'http://127.0.0.1:{server.server_port}'
    server.engine, server.management_token = engine, token
    server.instance_name, server.executor = 'rea-isolated-audit', None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield engine, Client(engine.endpoint, token=token, timeout=45), token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        engine.close()


def expect_error(status, callback):
    try:
        callback()
    except Error as error:
        assert error.status == status, f'Expected {status}, received {error.status}'
    else:
        raise AssertionError(f'Expected HTTP {status}')


def live_probe(docker_image=None):
    checks = []
    def passed(name):
        checks.append({'check': name, 'passed': True})
    with tempfile.TemporaryDirectory(prefix='localcloud-rea-audit-') as temporary:
        root = Path(temporary)
        path = root / 'state.db'
        bucket = 'rea-assets'
        with backend(path) as (engine, client, token):
            def manage(service, action, **args):
                return client.request('POST', '/_local/' + service, {'Action': action, **args})
            client.request('PUT', '/' + bucket, b'')
            payload = b'rea-binary\x00\xff'
            client.request('PUT', '/' + bucket + '/document', payload)
            assert client.request('GET', '/' + bucket + '/document', raw=True) == payload
            assert client.objects(bucket)['Contents'][0]['Size'] == len(payload)
            expect_error(409, lambda: client.request('DELETE', '/' + bucket))
            passed('s3-live-http-binary-list-and-nonempty-delete-error')
            queue = client.sqs('CreateQueue', {'QueueName': 'rea-jobs'})['QueueUrl']
            client.sqs('SendMessage', {'QueueUrl': queue, 'MessageBody': 'rea-message'})
            first = client.sqs('ReceiveMessage', {'QueueUrl': queue})['Messages'][0]
            assert client.sqs('ReceiveMessage', {'QueueUrl': queue})['Messages'] == []
            client.sqs('ChangeMessageVisibility', {'QueueUrl': queue, 'ReceiptHandle': first['ReceiptHandle'], 'VisibilityTimeout': 0})
            second = client.sqs('ReceiveMessage', {'QueueUrl': queue})['Messages'][0]
            assert second['ReceiptHandle'] != first['ReceiptHandle']
            client.sqs('DeleteMessage', {'QueueUrl': queue, 'ReceiptHandle': second['ReceiptHandle']})
            client.sqs('SetQueueAttributes', {'QueueUrl': queue, 'Attributes': {'VisibilityTimeout': '90'}})
            assert client.sqs('GetQueueAttributes', {'QueueUrl': queue})['Attributes']['VisibilityTimeout'] == '90'
            assert client.sqs('CreateQueue', {'QueueName': 'rea-jobs'})['QueueUrl'] == queue
            assert client.sqs('CreateQueue', {'QueueName': 'rea-jobs', 'Attributes': {}})['QueueUrl'] == queue
            passed('sqs-live-http-delivery-visibility-receipts-ack-settings-and-idempotence')
            item = manage('ec2', 'RunInstances', ImageId='ami-local', Name='rea-mock')['Instances'][0]
            manage('ec2', 'StopInstances', InstanceName='rea-mock')
            manage('ec2', 'ModifyInstanceAttribute', InstanceName='rea-mock', InstanceType='t3.small')
            manage('ec2', 'StartInstances', InstanceIds=[item['InstanceId']])
            passed('ec2-mock-live-http-names-lifecycle-and-type')
            engine.policy('test', {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}})
            engine.policy('reader', {'Statement': {'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::rea-assets/document'}})
            engine.iam_mode = 'enforce'
            reader = Client(engine.endpoint, principal='reader', token=token)
            assert reader.request('GET', '/rea-assets/document', raw=True) == payload
            expect_error(403, lambda: reader.request('PUT', '/rea-assets/forbidden', b'no'))
            passed('iam-live-http-allow-and-deny')
            client.request('POST', '/_local/snapshots', {'name': 'baseline'})
            client.request('PUT', '/temporary-bucket', b'')
            client.request('POST', '/_local/snapshots/restore', {'name': 'baseline'})
            assert 'temporary-bucket' not in [b['name'] for b in client.request('GET', '/_local/resources')['buckets']]
            bundle = client.request('GET', '/_local/state')
            client.request('DELETE', '/rea-assets/document')
            client.request('POST', '/_local/state/import', bundle)
            assert client.request('GET', '/rea-assets/document', raw=True) == payload
            bad = dict(bundle, sha256='0'*64)
            expect_error(400, lambda: client.request('POST', '/_local/state/import', bad))
            passed('snapshot-portable-state-restore-and-checksum-rejection')
            client.request('POST', '/_local/faults', {'name': 'rea-outage', 'service': 's3', 'operation': 'GetObject', 'error': {'code': 'Unavailable', 'status': 503}})
            expect_error(503, lambda: client.request('GET', '/rea-assets/document', raw=True))
            assert client.request('GET', '/_local/resources')['instanceName'] == 'rea-isolated-audit'
            client.request('POST', '/_local/faults/delete', {'name': 'rea-outage'})
            assert client.request('GET', '/rea-assets/document', raw=True) == payload
            passed('fault-injection-management-isolation-and-recovery')
            traces = client.request('GET', '/_local/traces')
            assert any(t.get('request_id') and t['error'] == 'Unavailable' for t in traces)
            policy = client.request('GET', '/_local/policy-stream?principal=reader')
            assert any('s3:GetObject' in st['Action'] for st in policy['Statement'])
            passed('traces-request-identity-and-policy-proposals')
            env = dict(os.environ)
            for name in list(env):
                if name.startswith('LOCALCLOUD_'):
                    env.pop(name)
            config = str(root / 'client.json')
            command = [sys.executable, '-m', 'localcloud.cli', '--config-file', config]
            connect = subprocess.run(command + ['connect', '--endpoint-url', engine.endpoint, '--token-stdin'], input=token, capture_output=True, text=True, env=env, timeout=20, cwd=ROOT)
            assert connect.returncode == 0, connect.stderr
            listing = subprocess.run(command + ['resources'], capture_output=True, text=True, env=env, timeout=20, cwd=ROOT)
            assert listing.returncode == 0, listing.stderr
            assert json.loads(listing.stdout)['instanceName'] == 'rea-isolated-audit'
            passed('cli-verified-profile-and-live-resource-access')
        with backend(path) as (_, client, _):
            assert client.request('GET', '/rea-assets/document', raw=True) == payload
            assert client.request('POST', '/_local/ec2', {'Action': 'DescribeInstances', 'InstanceName': 'rea-mock'})['Reservations'][0]['Instances'][0]['InstanceType'] == 't3.small'
            passed('persistence-after-backend-restart')
        from localcloud.ci import run
        with contextlib.redirect_stdout(io.StringIO()):
            result = run([sys.executable, '-c', 'import os; assert os.environ["LOCALCLOUD_CI"] == "1"; assert os.environ["AWS_ENDPOINT_URL"].startswith("http://127.0.0.1:")'], root / 'ci-artifacts')
        assert result == 0
        assert json.loads((root / 'ci-artifacts' / 'report.json').read_text())['passed']
        passed('ci-live-isolated-server-application-command-and-cleanup')
        if docker_image:
            docker_details = subprocess.run(['docker', 'image', 'inspect', docker_image, '--format', '{{.Id}}'], capture_output=True, text=True, timeout=15)
            assert docker_details.returncode == 0, docker_details.stderr
            digest = docker_details.stdout.strip()
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            with backend(root / 'docker.db', {'ami-rea-web': digest}) as (_, client, _):
                def ec2(action, **args):
                    return client.request('POST', '/_local/ec2', {'Action': action, **args})
                identifier = None
                try:
                    launched = ec2('RunInstances', ImageId='ami-rea-web', Name='rea-web', PortMappings=[{'HostPort': port, 'ContainerPort': 8080}])['Instances'][0]
                    identifier = launched['InstanceId']
                    import urllib.request
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    def available():
                        try:
                            with opener.open(f'http://127.0.0.1:{port}', timeout=2) as response:
                                return b'Hello from a LocalCloud EC2 container!' in response.read()
                        except OSError:
                            return False
                    for _ in range(30):
                        if available():
                            break
                        time.sleep(.2)
                    assert available(), 'Web app did not become reachable'
                    ec2('StopInstances', ContainerId=launched['ContainerId'][:12])
                    assert not available(), 'Stopped web app still responds'
                    ec2('ModifyInstanceAttribute', InstanceName='rea-web', InstanceType='t3.small')
                    ec2('StartInstances', HostPort=port)
                    for _ in range(30):
                        if available():
                            break
                        time.sleep(.2)
                    assert available(), 'Restarted web app did not respond'
                    ec2('TerminateInstances', ContainerName=launched['ContainerName'])
                    assert not available(), 'Terminated web app still responds'
                    removed = subprocess.run(['docker', 'container', 'inspect', 'localcloud-' + identifier], capture_output=True, timeout=15)
                    assert removed.returncode != 0, 'Terminated container was not removed'
                    passed('ec2-real-docker-web-port-stop-resize-start-terminate')
                    checks[-1]['image_id'] = digest
                finally:
                    if identifier:
                        subprocess.run(['docker', 'rm', '-f', 'localcloud-' + identifier], capture_output=True, timeout=15)
    return checks


def contract_probe():
    sys.path.insert(0, str(ROOT / 'tests'))
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    modules = Counter()
    def count(node):
        if isinstance(node, unittest.TestSuite):
            for child in node:
                count(child)
        else:
            modules[node.__class__.__module__] += 1
    count(suite)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    return {'passed': result.wasSuccessful(), 'tests_run': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'modules': dict(sorted(modules.items())),
            **({'details': stream.getvalue()[-6000:]} if not result.wasSuccessful() else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker-image', help='Explicit local web demo image; no image is pulled or built by this script')
    parser.add_argument('--contracts-only', action='store_true')
    args = parser.parse_args()
    source_paths = sorted((ROOT / 'localcloud').glob('*.py')) + [ROOT / name for name in ('localcloud/dashboard.js', 'localcloud/dashboard.html', 'localcloud/ec2-model.json', 'Dockerfile')]
    report = {'target': 'LocalCloud', 'reference_parity': 'not established',
              'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
    try:
        report['contracts'] = contract_probe()
        if not args.contracts_only:
            report['live_checks'] = live_probe(args.docker_image)
        report['passed'] = report['contracts']['passed']
    except Exception as error:
        report.update(passed=False, error=f'{type(error).__name__}: {error}')
    print(json.dumps(report, sort_keys=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
