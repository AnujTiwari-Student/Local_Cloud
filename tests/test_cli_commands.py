"""CLI-to-handler contract tests without a live TCP socket."""
import argparse
import base64
import contextlib
import email.parser
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock, patch
from localcloud.__main__ import Handler
from localcloud.cli import main
from localcloud.client import Profiles, Client
from localcloud.engine import Engine


class Connection:
    def __init__(self, raw):
        self.raw, self.output = raw, bytearray()
    def makefile(self, *args):
        return io.BytesIO(self.raw)
    def sendall(self, data):
        self.output.extend(data)


class HandlerOpener:
    def __init__(self, server):
        self.server, self.requests = server, []
    def open(self, request, timeout=15):
        self.requests.append(request)
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        body = request.data or b''
        headers = {**dict(request.header_items()), 'Content-Length': str(len(body))}
        raw = (request.get_method() + ' ' + path + ' HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in headers.items()) + '\r\n').encode() + body
        connection = Connection(raw)
        Handler(connection, ('127.0.0.1', 12345), self.server)
        head, payload = bytes(connection.output).split(b'\r\n\r\n', 1)
        status = int(head.split()[1])
        parsed_headers = email.parser.BytesParser().parsebytes(head.split(b'\r\n', 1)[1])
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, 'fixture response', parsed_headers, io.BytesIO(payload))
        response = io.BytesIO(payload)
        response.headers = parsed_headers
        return response


class CommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'LOCALCLOUD_CONFIG': str(self.root / 'config.json')}, clear=True)
        self.env.start()
        self.engine = Engine(str(self.root / 'state.db'))
        self.server = SimpleNamespace(engine=self.engine, management_token=None, executor=None)
        self.transport = HandlerOpener(self.server)
        self.patch = patch('localcloud.client.urllib.request.build_opener', return_value=self.transport)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.engine.close()
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *args, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch('sys.stdin', io.StringIO(stdin)):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def success(self, *args, stdin=''):
        code, out, err = self.cli(*args, stdin=stdin)
        self.assertEqual(code, 0, err)
        return out

    def test_s3_upload_download_copy_list_prefix_and_delete(self):
        source = self.root / 'source.bin'
        source.write_bytes(b'\x00\xffbinary')
        self.success('s3', 'mb', 's3://assets')
        self.success('s3', 'cp', str(source), 's3://assets/folder/a ?#☃')
        self.assertIn('folder/a ?#☃', self.success('s3', 'ls', 's3://assets/folder/'))
        output = self.root / 'download.bin'
        self.success('s3', 'cp', 's3://assets/folder/a ?#☃', str(output))
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.success('s3', 'cp', 's3://assets/folder/a ?#☃', 's3://assets/copy')
        self.assertEqual(self.engine.s3('GET', 'assets', 'copy'), source.read_bytes())
        self.success('s3', 'rm', 's3://assets/folder/a ?#☃')
        self.success('s3', 'rm', 's3://assets/copy')
        self.success('s3', 'rb', 's3://assets')
        self.assertEqual(self.engine.overview()['buckets'], [])

    def test_s3_destination_infers_filename_and_json_output(self):
        source = self.root / 'photo.bin'
        source.write_bytes(b'photo')
        self.success('s3', 'mb', 's3://assets')
        self.success('s3', 'cp', str(source), 's3://assets/')
        result = json.loads(self.success('s3', 'ls', '--output', 'json'))
        self.assertEqual(result['Buckets'], [{'Name': 'assets'}])
        directory = self.root / 'downloads'
        directory.mkdir()
        self.success('s3', 'cp', 's3://assets/photo.bin', str(directory))
        self.assertEqual((directory / 'photo.bin').read_bytes(), b'photo')

    def test_s3api_every_implemented_operation(self):
        self.success('s3api', 'create-bucket', '--bucket', 'assets')
        source = self.root / 'body'
        source.write_bytes(b'body')
        put = json.loads(self.success('s3api', 'put-object', '--bucket', 'assets', '--key', 'key', '--body', str(source)))
        self.assertIn('ETag', put)
        result = json.loads(self.success('s3api', 'list-objects-v2', '--bucket', 'assets'))
        self.assertEqual(result['Contents'], [{'Key': 'key', 'Size': 4}])
        destination = self.root / 'received'
        self.success('s3api', 'get-object', '--bucket', 'assets', '--key', 'key', str(destination))
        self.assertEqual(destination.read_bytes(), b'body')
        self.success('s3api', 'list-buckets')
        self.success('s3api', 'delete-object', '--bucket', 'assets', '--key', 'key')
        self.success('s3api', 'delete-bucket', '--bucket', 'assets')

    def test_empty_object_key_never_deletes_a_bucket(self):
        self.success('s3', 'mb', 's3://assets')
        code, _, error = self.cli('s3api', 'delete-object', '--bucket', 'assets', '--key', '')
        self.assertEqual(code, 1)
        self.assertIn('key cannot be empty', error)
        self.assertEqual(len(self.engine.overview()['buckets']), 1)

    def test_sqs_all_operations_and_text_queue_url(self):
        self.success('sqs', 'create-queue', '--queue-name', 'jobs', '--attributes', 'VisibilityTimeout=300')
        queue_url = self.success('sqs', 'get-queue-url', '--queue-name', 'jobs', '--output', 'text').strip()
        self.assertTrue(queue_url.endswith('/000000000000/jobs'))
        self.success('sqs', 'list-queues', '--queue-name-prefix', 'job')
        result = json.loads(self.success('sqs', 'get-queue-attributes', '--queue-name', 'jobs'))
        self.assertEqual(result['Attributes']['VisibilityTimeout'], '300')
        message_file = self.root / 'message.txt'
        message_file.write_text('hello from file')
        self.success('sqs', 'send-message', '--queue-name', 'jobs', '--message-body', 'file://' + str(message_file))
        response = json.loads(self.success('sqs', 'receive-message', '--queue-url', queue_url, '--max-number-of-messages', '1'))
        receipt = response['Messages'][0]['ReceiptHandle']
        self.assertEqual(response['Messages'][0]['Body'], 'hello from file')
        self.success('sqs', 'change-message-visibility', '--queue-name', 'jobs', '--receipt-handle', receipt, '--visibility-timeout', '0')
        response = json.loads(self.success('sqs', 'receive-message', '--queue-name', 'jobs'))
        self.success('sqs', 'delete-message', '--queue-name', 'jobs', '--receipt-handle', response['Messages'][0]['ReceiptHandle'])
        self.success('sqs', 'purge-queue', '--queue-name', 'jobs')
        self.success('sqs', 'delete-queue', '--queue-name', 'jobs')
        self.assertEqual(self.engine.overview()['queues'], [])

    def test_snapshot_and_portable_state_commands(self):
        self.success('s3', 'mb', 's3://assets')
        self.success('snapshots', 'save', 'baseline')
        self.assertEqual(json.loads(self.success('snapshots', 'list'))[0]['name'], 'baseline')
        self.success('s3', 'rb', 's3://assets')
        self.success('snapshots', 'restore', 'baseline')
        self.assertEqual(len(self.engine.overview()['buckets']), 1)
        filename = self.root / 'bundle.json'
        self.success('state', 'export', str(filename))
        self.success('s3', 'rb', 's3://assets')
        self.success('state', 'import', str(filename))
        self.assertEqual(len(self.engine.overview()['buckets']), 1)
        self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)

    def test_iam_principal_trace_and_policy_file(self):
        document = self.root / 'policy.json'
        document.write_text(json.dumps({'Statement': {'Effect': 'Allow', 'Action': 's3:*', 'Resource': '*'}}))
        self.success('iam', 'put-policy', '--user-name', 'alice', '--policy-document', 'file://' + str(document))
        self.assertEqual(json.loads(self.success('iam', 'list-principals')), ['alice'])
        self.engine.iam_mode = 'enforce'
        self.success('s3', 'mb', 's3://assets', '--principal', 'alice')
        generated = json.loads(self.success('iam', 'generate-policy', '--user-name', 'alice'))
        self.assertEqual(generated['Statement'][0]['Action'], 's3:CreateBucket')
        traces = json.loads(self.success('traces', 'list', '--user-name', 'alice'))
        self.assertEqual(traces[0]['principal'], 'alice')
        self.success('traces', 'clear')
        self.assertEqual(self.engine.tools.traces(), [])

    def test_fault_commands_and_friendly_server_error(self):
        self.success('faults', 'add', '--name', 'outage', '--service', 's3', '--operation', 'CreateBucket', '--error-code', 'ServiceUnavailable')
        self.assertEqual(json.loads(self.success('faults', 'list'))[0]['name'], 'outage')
        code, _, error = self.cli('s3', 'mb', 's3://assets')
        self.assertEqual(code, 1)
        self.assertIn('ServiceUnavailable', error)
        self.assertIn('HTTP 503', error)
        self.assertNotIn('Traceback', error)
        self.success('faults', 'remove', 'outage')
        self.success('s3', 'mb', 's3://assets')

    def test_job_command_preserves_argv_env_and_calls_real_routes(self):
        executor = Mock()
        executor.images = {'localcloud:dev'}
        executor.submit.return_value = {'name': 'lc-run-example', 'phase': 'Pending'}
        executor.list.return_value = []
        executor.status.return_value = {'name': 'lc-run-example', 'phase': 'Complete'}
        executor.logs.return_value = {'logs': 'hello\n', 'pending': False}
        executor.cancel.return_value = {'deleted': True}
        self.server.executor = executor
        self.success('jobs', 'run', '--image', 'localcloud:dev', '--env', 'GREETING=a=b', '--command', 'python3', '-c', 'print("hello")')
        body = executor.submit.call_args.args[0]
        self.assertEqual(body['command'], ['python3', '-c', 'print("hello")'])
        self.assertEqual(body['env'], {'GREETING': 'a=b'})
        self.success('jobs', 'list')
        self.success('jobs', 'status', 'lc-run-example')
        self.assertEqual(self.success('jobs', 'logs', 'lc-run-example'), 'hello\n')
        self.success('jobs', 'delete', 'lc-run-example')

    def test_login_stores_private_token_and_never_prints_it(self):
        self.server.management_token = 'secret-value'
        self.success('auth', 'login', '--token-stdin', stdin='secret-value\n')
        configuration = Path(os.environ['LOCALCLOUD_CONFIG'])
        self.assertEqual(stat.S_IMODE(configuration.stat().st_mode), 0o600)
        self.assertEqual(Profiles().get('default')['token'], 'secret-value')
        output = self.success('configure', '--list')
        self.assertNotIn('secret-value', output)
        self.assertIn('token_configured', output)
        self.success('resources')
        self.success('auth', 'status')
        self.success('auth', 'logout')
        self.assertNotIn('token', Profiles().get('default'))
        code, _, error = self.cli('resources')
        self.assertEqual(code, 1)
        self.assertIn('auth login', error)

    def test_failed_login_does_not_overwrite_existing_token(self):
        Profiles().update('default', {'token': 'working'})
        self.server.management_token = 'working'
        code, out, err = self.cli('auth', 'login', '--token-stdin', stdin='wrong-token')
        self.assertEqual(code, 1)
        self.assertNotIn('wrong-token', out + err)
        self.assertEqual(Profiles().get('default')['token'], 'working')

    def test_kubernetes_login_does_not_expose_secret(self):
        self.server.management_token = 'from-kubernetes'
        payload = {'data': {'token': base64.b64encode(b'from-kubernetes').decode()}}
        with patch('localcloud.client.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(payload))) as kubectl:
            output = self.success('auth', 'login', '--kubernetes-secret', 'localcloud-access', '--namespace', 'localcloud')
        self.assertNotIn('from-kubernetes', output)
        self.assertIn('localcloud-access', kubectl.call_args.args[0])
        self.success('resources')

    def test_global_flags_before_or_after_command_and_environment_precedence(self):
        self.success('--profile', 'dev', 'configure', '--endpoint-url', 'http://127.0.0.1:9999')
        self.success('--profile', 'dev', 'status')
        self.assertEqual(self.transport.requests[-1].full_url, 'http://127.0.0.1:9999/_local/health')
        with patch.dict(os.environ, {'LOCALCLOUD_URL': 'http://127.0.0.1:8888'}):
            self.success('status', '--profile', 'dev')
            self.assertIn(':8888/', self.transport.requests[-1].full_url)
            self.success('status', '--profile', 'dev', '--endpoint-url', 'http://127.0.0.1:7777')
            self.assertIn(':7777/', self.transport.requests[-1].full_url)

    def test_configuration_rejects_invalid_urls_and_unsupported_region(self):
        for args in [('configure', '--endpoint-url', 'http://user:password@localhost'), ('status', '--region', 'eu-west-1'), ('status', '--timeout', '0')]:
            self.assertEqual(self.cli(*args)[0], 1)
        self.assertEqual(self.transport.requests, [])

    def test_request_tokens_only_go_to_management_endpoints(self):
        with patch.dict(os.environ, {'LOCALCLOUD_MANAGEMENT_TOKEN': 'token'}):
            self.success('s3', 'mb', 's3://assets')
            self.assertIsNone(self.transport.requests[-1].get_header('Authorization'))
            self.success('resources')
            self.assertEqual(self.transport.requests[-1].get_header('Authorization'), 'Bearer token')

<<<<<<< HEAD
=======
    def test_ci_http_client_runs_smoke_with_actual_response_headers(self):
        from localcloud.ci import HTTPClient, smoke
        self.server.management_token = 'ci-token'
        client = HTTPClient('http://127.0.0.1:4566', 'ci-token')
        self.assertEqual(smoke(client), ['health', 's3-binary-roundtrip', 'sqs-delivery', 'sqs-visibility', 'sqs-acknowledgement'])
        self.assertEqual(self.engine.overview()['buckets'], [])
        self.assertEqual(self.engine.overview()['queues'], [])

    def test_ci_runner_records_success_using_real_smoke_and_http_transport(self):
        from localcloud.ci import run
        server = SimpleNamespace(server_port=12345, serve_forever=Mock(), shutdown=Mock(), server_close=Mock())
        transport = HandlerOpener(server)
        artifacts = self.root / 'ci-artifacts'
        with patch('localcloud.ci.ThreadingHTTPServer', return_value=server), patch('localcloud.ci.urllib.request.build_opener', return_value=transport), contextlib.redirect_stdout(io.StringIO()):
            code = run([], artifacts)
        self.assertEqual(code, 0)
        report = json.loads((artifacts / 'report.json').read_text())
        self.assertTrue(report['passed'])
        self.assertEqual(len(report['checks']), 5)
        self.assertTrue(json.loads((artifacts / 'traces.json').read_text()))
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()

>>>>>>> 28aa52d (Feature: Add ec2 service)
    def test_connection_failure_has_actionable_message_without_traceback(self):
        with patch.object(self.transport, 'open', side_effect=urllib.error.URLError('Connection refused')):
            code, out, error = self.cli('status')
        self.assertEqual(code, 1)
        self.assertIn('port-forward', error)
        self.assertNotIn('Traceback', error)

    def test_unknown_aws_options_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['s3', 'cp', 'a', 's3://assets/a', '--recursive'])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.transport.requests, [])

    def test_existing_lifecycle_parsers_receive_arguments(self):
        for command, module in [('start', 'localcloud.__main__'), ('ci', 'localcloud.ci'), ('kube', 'localcloud.operator')]:
            with patch(module + '.main', return_value=0) as delegate:
                self.assertEqual(main([command, '--help']), 0)
                delegate.assert_called_once_with(['--help'])


class LauncherTests(unittest.TestCase):
    def test_checkout_launcher_works_outside_project_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(root / 'bin/localcloud'), '--help'], cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sqs', result.stdout)

    def test_pip_free_installer_installs_working_launcher(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            install = subprocess.run([sys.executable, str(root / 'scripts/install-cli.py'), '--bin-dir', directory], capture_output=True, text=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            result = subprocess.run([sys.executable, str(Path(directory) / 'localcloud'), 'jobs', '--help'], cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('logs', result.stdout)
