import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from localcloud.ci import main, run
from localcloud.project import initialize, load_project


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def config(self, text):
        filename = self.root / 'localcloud.toml'
        filename.write_text(text)
        return filename

    def test_initialize_creates_valid_manifest_and_refuses_overwrite(self):
        filename = self.root / 'localcloud.toml'
        initialize(filename, ['npm', 'test'])
        settings = load_project(filename)
        self.assertEqual(settings['command'], ['npm', 'test'])
        self.assertEqual(settings['cwd'], str(self.root))
        with self.assertRaises(FileExistsError):
            initialize(filename, ['different'])
        self.assertEqual(load_project(filename)['command'], ['npm', 'test'])

    def test_paths_seed_policies_and_variables_are_resolved(self):
        filename = self.config('''version = 1
[ci]
command = ["npm", "test"]
fixture = "ci/baseline.json"
principal = "app"
[ci.seed]
buckets = ["example-assets"]
queues = ["jobs"]
[ci.policy_files]
app = "ci/app.json"
[ci.env]
BUCKET = "example-assets"
''')
        loaded = load_project(filename)
        self.assertEqual(loaded['fixture'], str(self.root / 'ci/baseline.json'))
        self.assertEqual(loaded['policy_files']['app'], str(self.root / 'ci/app.json'))
        self.assertEqual(loaded['seed']['queues'], ['jobs'])
        self.assertEqual(loaded['env'], {'BUCKET': 'example-assets'})

    def test_unknown_services_settings_and_invalid_types_are_rejected(self):
        examples = ['services = ["lambda"]', 'command = "npm test"', 'timeout = 0', 'export_state = "yes"', 'unknown = true', 'principal = "ci-smoke"']
        for setting in examples:
            text = 'version = 1\n[ci]\n'
            if not setting.startswith('command'):
                text += 'command = ["npm", "test"]\n'
            filename = self.config(text + setting + '\n')
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                load_project(filename)

    def test_reserved_environment_and_smoke_policy_are_rejected(self):
        for text in ['[ci.env]\nLOCALCLOUD_URL = "http://unrelated"', '[ci.policy_files]\nci-smoke = "policy.json"']:
            filename = self.config('version = 1\n[ci]\ncommand = ["npm", "test"]\n' + text)
            with self.assertRaises(ValueError):
                load_project(filename)

    def test_missing_implicit_config_uses_smoke_only_and_explicit_missing_fails(self):
        with contextlib.chdir(self.root):
            self.assertIsNone(load_project())
            with self.assertRaises(FileNotFoundError):
                load_project('missing.toml')

    def test_auto_discovery_runs_project_command_from_its_directory(self):
        self.config('version = 1\n[ci]\ncommand = ["npm", "test"]\ntimeout = 80\n')
        with contextlib.chdir(self.root), patch('localcloud.ci.run', return_value=0) as runner, self.assertRaises(SystemExit) as result:
            main([])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(runner.call_args.args[0], ['npm', 'test'])
        self.assertEqual(runner.call_args.kwargs['cwd'], str(self.root))
        self.assertEqual(runner.call_args.args[4], 80)

    def test_cli_overrides_project_settings_and_command(self):
        filename = self.config('version = 1\n[ci]\ncommand = ["npm", "test"]\ntimeout = 80\nexport_state = true\n')
        with patch('localcloud.ci.run', return_value=0) as runner, self.assertRaises(SystemExit):
            main(['--config', str(filename), '--timeout', '20', '--no-export-state', '--', 'node', 'check.mjs'])
        self.assertEqual(runner.call_args.args[0], ['node', 'check.mjs'])
        self.assertEqual(runner.call_args.args[4:6], (20, False))

    def test_no_config_does_not_execute_automatic_app_command(self):
        self.config('not valid toml {')
        with contextlib.chdir(self.root), patch('localcloud.ci.run', return_value=0) as runner, self.assertRaises(SystemExit):
            main(['--no-config'])
        self.assertEqual(runner.call_args.args[0], [])

    def test_init_cli_writes_only_local_project_file(self):
        with contextlib.chdir(self.root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['init', '--command', 'npm', 'test']), 0)
        self.assertEqual([p.name for p in self.root.iterdir()], ['localcloud.toml'])

    def test_seed_and_policies_are_ready_before_app_test_command(self):
        policy = self.root / 'reader.json'
        policy.write_text(json.dumps({'Statement': {'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::example-assets/*'}}))
        server = Mock(server_port=12345)
        def smoke(_client):
            self.assertEqual(server.engine.overview()['buckets'][0]['name'], 'example-assets')
            self.assertEqual(server.engine.overview()['queues'][0]['name'], 'jobs')
            self.assertIn('reader', server.engine.overview()['principals'])
            return ['prepared']
        with patch('localcloud.ci.ThreadingHTTPServer', return_value=server), patch('localcloud.ci.smoke', side_effect=smoke), patch('localcloud.ci.subprocess.run', return_value=Mock(returncode=0)) as process, contextlib.redirect_stdout(io.StringIO()):
            code = run(['npm', 'test'], self.root / 'artifacts', iam='enforce', cwd=str(self.root), extra_env={'BUCKET': 'example-assets'}, policy_files={'reader': str(policy)}, seed={'buckets': ['example-assets'], 'queues': ['jobs']}, principal='app')
        self.assertEqual(code, 0)
        env = process.call_args.kwargs['env']
        self.assertEqual(env['LOCALCLOUD_PRINCIPAL'], 'app')
        self.assertEqual(env['LOCALCLOUD_CI'], '1')
        self.assertEqual(env['BUCKET'], 'example-assets')
        self.assertEqual(process.call_args.kwargs['cwd'], str(self.root))
        report = json.loads((self.root / 'artifacts/report.json').read_text())
        self.assertEqual(report['application_tests'], 'passed')

    def test_seed_preserves_queue_attributes_loaded_from_fixture(self):
        from localcloud.engine import Engine
        engine = Engine(str(self.root / 'original.db'))
        try:
            engine.sqs('CreateQueue', {'QueueName': 'jobs', 'Attributes': {'VisibilityTimeout': '300'}})
            fixture = self.root / 'fixture.json'
            fixture.write_text(json.dumps(engine.tools.export_state()))
        finally:
            engine.close()
        server = Mock(server_port=12345)
        def smoke(_client):
            attributes = server.engine.sqs('GetQueueAttributes', {'QueueName': 'jobs'}, 'ci-smoke')['Attributes']
            self.assertEqual(attributes['VisibilityTimeout'], '300')
            return ['fixture-preserved']
        with patch('localcloud.ci.ThreadingHTTPServer', return_value=server), patch('localcloud.ci.smoke', side_effect=smoke), contextlib.redirect_stdout(io.StringIO()):
            code = run([], self.root / 'artifacts', fixture=str(fixture), seed={'queues': ['jobs']})
        self.assertEqual(code, 0)

    def test_real_example_manifest_parses(self):
        filename = Path(__file__).resolve().parents[1] / 'examples/node-app/localcloud.toml'
        loaded = load_project(filename)
        self.assertEqual(loaded['iam'], 'enforce')
        for value in loaded['policy_files'].values():
            self.assertIsInstance(json.loads(Path(value).read_text()), dict)
