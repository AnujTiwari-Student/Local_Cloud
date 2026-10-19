"""Isolated live HTTP CI runner with diagnostics and deterministic cleanup."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from .__main__ import Handler
from .engine import Engine, Error


class HTTPClient:
    def __init__(self, endpoint, token):
        self.endpoint, self.token = endpoint.rstrip('/'), token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, data=None, target=None):
        headers = {'X-Local-Principal': 'ci-smoke'}
        if path.startswith('/_local/'):
            headers['Authorization'] = 'Bearer ' + self.token
        if target:
            headers['X-Amz-Target'] = 'AmazonSQS.' + target
            headers['Content-Type'] = 'application/x-amz-json-1.0'
        elif isinstance(data, dict):
            headers['Content-Type'] = 'application/json'
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        request = urllib.request.Request(self.endpoint + path, data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=10) as response:
            payload = response.read()
            return json.loads(payload) if 'json' in response.headers.get('Content-Type', '') else payload


def smoke(client):
    """Verify real S3 and SQS HTTP behavior; only delete resources we created."""
    name = 'ci-' + uuid.uuid4().hex[:12]
    queue = None
    bucket_created = False
    try:
        health = client.request('GET', '/_local/health')
        if health.get('status') != 'running':
            raise RuntimeError('Backend health check failed')
        client.request('PUT', '/' + name, b'')
        bucket_created = True
        payload = b'localcloud-ci\x00\xff'
        client.request('PUT', '/' + name + '/payload', payload)
        if client.request('GET', '/' + name + '/payload') != payload:
            raise RuntimeError('S3 binary roundtrip failed')
        queue = client.request('POST', '/', {'QueueName': name}, 'CreateQueue')['QueueUrl']
        client.request('POST', '/', {'QueueUrl': queue, 'MessageBody': 'ci-message'}, 'SendMessage')
        received = client.request('POST', '/', {'QueueUrl': queue}, 'ReceiveMessage')['Messages']
        if len(received) != 1 or received[0]['Body'] != 'ci-message':
            raise RuntimeError('SQS delivery failed')
        hidden = client.request('POST', '/', {'QueueUrl': queue}, 'ReceiveMessage')['Messages']
        if hidden:
            raise RuntimeError('SQS visibility failed')
        client.request('POST', '/', {'QueueUrl': queue, 'ReceiptHandle': received[0]['ReceiptHandle']}, 'DeleteMessage')
        if client.request('POST', '/', {'QueueUrl': queue}, 'ReceiveMessage')['Messages']:
            raise RuntimeError('SQS acknowledgement failed')
        return ['health', 's3-binary-roundtrip', 'sqs-delivery', 'sqs-visibility', 'sqs-acknowledgement']
    finally:
        if queue:
            client.request('POST', '/', {'QueueUrl': queue}, 'DeleteQueue')
        if bucket_created:
            client.request('DELETE', '/' + name + '/payload')
            client.request('DELETE', '/' + name)


def run(command, artifacts, fixture=None, iam='soft', timeout=300, export_state=False):
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'checks': [], 'command_exit_code': None, 'error': None}
    started = time.monotonic()
    server = thread = engine = None
    token = os.environ.get('LOCALCLOUD_CI_TOKEN') or secrets.token_urlsafe(32)
    result = 1
    with tempfile.TemporaryDirectory(prefix='localcloud-ci-') as directory:
        try:
            engine = Engine(str(Path(directory) / 'state.sqlite3'), iam_mode=iam)
            if fixture:
                engine.tools.import_state(json.loads(Path(fixture).read_text()))
            engine.policy('ci-smoke', {'Statement': {'Effect': 'Allow', 'Action': ['s3:*', 'sqs:*'], 'Resource': '*'}})
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.engine, server.management_token, server.executor = engine, token, None
            endpoint = 'http://127.0.0.1:' + str(server.server_port)
            engine.endpoint = endpoint
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            report['checks'] = smoke(HTTPClient(endpoint, token))
            if command:
                env = os.environ.copy()
                env.update(LOCALCLOUD_URL=endpoint, AWS_ENDPOINT_URL=endpoint,
                           LOCALCLOUD_MANAGEMENT_TOKEN=token, AWS_DEFAULT_REGION='us-east-1',
                           AWS_ACCESS_KEY_ID='test', AWS_SECRET_ACCESS_KEY='test')
                process = subprocess.run(command, env=env, timeout=timeout, check=False)
                report['command_exit_code'] = process.returncode
                result = process.returncode if process.returncode >= 0 else 1
            else:
                result = 0
            report['passed'] = result == 0
        except (OSError, Error, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            report['error'] = str(error)
            result = 1
        finally:
            if server is not None:
                if thread is not None:
                    server.shutdown()
                    thread.join(timeout=10)
                server.server_close()
            try:
                if engine is not None:
                    (artifacts / 'traces.json').write_text(json.dumps(engine.tools.traces(), indent=2) + '\n')
                    if export_state:
                        (artifacts / 'state.json').write_text(json.dumps(engine.tools.export_state(), indent=2) + '\n')
            except (OSError, Error) as error:
                report['artifact_error'] = str(error)
                report['passed'], result = False, 1
            finally:
                if engine is not None:
                    engine.close()
                report['duration_seconds'] = round(time.monotonic() - started, 3)
                (artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run isolated LocalCloud HTTP smoke checks and an optional application test command')
    parser.add_argument('--artifacts', default='artifacts/localcloud')
    parser.add_argument('--fixture', help='Validated LocalCloud JSON state bundle to preload')
    parser.add_argument('--iam', choices=['off', 'soft', 'enforce'], default='soft')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--export-state', action='store_true', help='Include resource contents in diagnostics; may contain sensitive test data')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Optional command after --')
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error('Timeout must be positive')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    raise SystemExit(run(command, args.artifacts, args.fixture, args.iam, args.timeout, args.export_state))


if __name__ == '__main__':
    main()
