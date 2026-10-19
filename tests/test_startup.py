import errno
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from localcloud.__main__ import main
from localcloud.engine import Engine


class StartupTests(unittest.TestCase):
    def check_bind_failure(self, number):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(str(Path(directory) / 'db'))
            stderr = io.StringIO()
            with patch('localcloud.__main__.Engine', return_value=engine), patch.object(engine, 'close', wraps=engine.close) as close, patch('localcloud.__main__.ThreadingHTTPServer', side_effect=OSError(number, 'bind failed')), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exit:
                    main(['--port', '4566'])
                self.assertEqual(exit.exception.code, 2)
                close.assert_called_once()
            return stderr.getvalue()

    def test_occupied_port_has_recovery_instructions(self):
        message = self.check_bind_failure(errno.EADDRINUSE)
        self.assertIn('127.0.0.1:4566 is already in use', message)
        self.assertIn('kubectl port-forward', message)
        self.assertIn('--port 4567 --data .localcloud/state-4567.sqlite3', message)
        self.assertIn('configure --endpoint-url', message)
        self.assertNotIn('Traceback', message)

    def test_other_bind_error_is_reported_without_traceback(self):
        message = self.check_bind_failure(errno.EACCES)
        self.assertIn('Cannot listen on 127.0.0.1:4566', message)
        self.assertNotIn('already in use', message)
