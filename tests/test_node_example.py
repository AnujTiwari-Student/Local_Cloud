import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


class NodeExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node 22+ is optional for the Node application example')
        version = subprocess.run([cls.node, '--version'], capture_output=True, text=True, check=True).stdout.strip()
        if int(version.lstrip('v').split('.')[0]) < 22:
            raise unittest.SkipTest('Node application example requires Node 22+')
        cls.root = Path(__file__).resolve().parents[1]

    def test_unit_examples(self):
        result = subprocess.run([self.node, str(self.root / 'examples/node-app/test/unit.test.mjs')], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('# fail 0', result.stdout)

    def test_integration_examples_against_real_handler_over_stdio(self):
        env = os.environ.copy()
        env['LOCALCLOUD_TEST_PYTHON'] = sys.executable
        result = subprocess.run([self.node, str(self.root / 'tests/support/node_contract.mjs')], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('# tests 6', result.stdout)
        self.assertIn('# fail 0', result.stdout)
