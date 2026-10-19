import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from localcloud.engine import Engine


class CLITests(unittest.TestCase):
    def test_export_and_load_without_opening_a_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.db'
            engine = Engine(str(original))
            engine.s3('PUT', 'assets', '')
            engine.s3('PUT', 'assets', 'hello', b'world')
            engine.close()
            bundle = root / 'bundle.json'
            first = subprocess.run([sys.executable, '-m', 'localcloud', '--data', str(original), '--export-state', str(bundle)], capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            result = root / 'result.json'
            second = subprocess.run([sys.executable, '-m', 'localcloud', '--data', str(root / 'destination.db'), '--load-state', str(bundle), '--export-state', str(result)], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(bundle.read_text()), json.loads(result.read_text()))

    def test_invalid_startup_bundle_exits_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bad.json'
            bundle.write_text('{"format":"wrong"}')
            result = subprocess.run([sys.executable, '-m', 'localcloud', '--data', str(root / 'state.db'), '--load-state', str(bundle)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Unsupported state bundle', result.stderr)
            self.assertNotIn('PermissionError', result.stderr)
