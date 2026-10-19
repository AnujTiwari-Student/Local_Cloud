import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from localcloud.ci import run


class FakeServer:
    server_port = 12345
    def __init__(self, *args):
        self.stopped = self.closed = False
    def serve_forever(self):
        pass
    def shutdown(self):
        self.stopped = True
    def server_close(self):
        self.closed = True


class CIRunnerTests(unittest.TestCase):
    def execute(self, directory, command=None, process=None, failure=None):
        server = FakeServer()
        process = process or Mock(returncode=0)
        with patch('localcloud.ci.ThreadingHTTPServer', return_value=server), patch('localcloud.ci.smoke', return_value=['fixture-smoke']) as smoke, patch('localcloud.ci.subprocess.run', return_value=process, side_effect=failure) as child, contextlib.redirect_stdout(io.StringIO()):
            result = run(command or [], directory, timeout=10)
        return result, server, smoke, child

    def test_success_writes_diagnostics_and_stops_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            result, server, _, _ = self.execute(directory)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertEqual(result, 0)
            self.assertTrue(report['passed'])
            self.assertTrue(server.stopped and server.closed)
            self.assertTrue((Path(directory) / 'traces.json').exists())
            self.assertFalse((Path(directory) / 'state.json').exists())
            self.assertNotIn(server.management_token, json.dumps(report))

    def test_test_failure_preserves_exit_code_and_provides_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            result, server, _, process = self.execute(directory, ['python3', 'test.py'], Mock(returncode=7))
            self.assertEqual(result, 7)
            self.assertTrue(server.stopped and server.closed)
            options = process.call_args.kwargs
            self.assertEqual(options['env']['LOCALCLOUD_URL'], 'http://127.0.0.1:12345')
            self.assertEqual(options['env']['LOCALCLOUD_MANAGEMENT_TOKEN'], server.management_token)
            self.assertNotIn('shell', options)
            self.assertEqual(json.loads((Path(directory) / 'report.json').read_text())['command_exit_code'], 7)

    def test_timeout_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            result, server, _, _ = self.execute(directory, ['example'], failure=subprocess.TimeoutExpired(['example'], 10))
            self.assertEqual(result, 1)
            self.assertTrue(server.stopped and server.closed)
            self.assertFalse(json.loads((Path(directory) / 'report.json').read_text())['passed'])

    def test_socket_failure_is_reported_as_failure_and_artifacts_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('localcloud.ci.ThreadingHTTPServer', side_effect=PermissionError('socket blocked')), contextlib.redirect_stdout(io.StringIO()):
                result = run([], directory)
            self.assertEqual(result, 1)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertIn('socket blocked', report['error'])

    def test_invalid_fixture_fails_before_server_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'bad.json'
            fixture.write_text('{}')
            with patch('localcloud.ci.ThreadingHTTPServer') as server, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run([], Path(directory) / 'artifacts', fixture=str(fixture)), 1)
                server.assert_not_called()
