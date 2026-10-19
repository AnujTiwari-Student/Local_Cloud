"""Project-root CI configuration, parsed with Python's standard TOML reader."""
import json
from pathlib import Path
import re
import tomllib
from .engine import valid_name

DEFAULTS = {'command': [], 'artifacts': 'artifacts/localcloud', 'fixture': None,
            'iam': 'soft', 'timeout': 300, 'export_state': False,
            'services': ['s3', 'sqs'], 'principal': 'test', 'env': {},
            'seed': {'buckets': [], 'queues': []}, 'policy_files': {}}


def load_project(filename=None, disabled=False):
    path = Path(filename or 'localcloud.toml').expanduser().resolve()
    if disabled or (filename is None and not path.exists()):
        return None
    with path.open('rb') as source:
        data = tomllib.load(source)
    if set(data) != {'version', 'ci'} or type(data['version']) is not int or data['version'] != 1 or not isinstance(data['ci'], dict):
        raise ValueError('Expected version = 1 and a [ci] table in localcloud.toml')
    settings = data['ci']
    unknown = set(settings) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unknown CI settings: ' + ', '.join(sorted(unknown)))
    if 'command' not in settings or not isinstance(settings['command'], list) or not settings['command'] or any(not isinstance(v, str) or '\x00' in v or not v for v in settings['command']):
        raise ValueError('ci.command must be a nonempty argv array, such as ["npm", "test"]')
    if settings.get('iam', 'soft') not in ('off', 'soft', 'enforce'):
        raise ValueError('ci.iam must be off, soft or enforce')
    if type(settings.get('timeout', 300)) is not int or settings.get('timeout', 300) < 1:
        raise ValueError('ci.timeout must be a positive integer')
    if type(settings.get('export_state', False)) is not bool:
        raise ValueError('ci.export_state must be a boolean')
    services = settings.get('services', ['s3', 'sqs'])
    if not isinstance(services, list) or not services or any(v not in ('s3', 'sqs') for v in services):
        raise ValueError('ci.services supports only s3 and sqs')
    valid_name(settings.get('principal', 'test'))
    if settings.get('principal') == 'ci-smoke':
        raise ValueError('ci-smoke is reserved for the runner; choose an application principal')
    variables = settings.get('env', {})
    if not isinstance(variables, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or not isinstance(v, str) or '\x00' in v for k, v in variables.items()):
        raise ValueError('ci.env must map environment variable names to strings')
    if any(k.startswith(('LOCALCLOUD_', 'AWS_')) for k in variables):
        raise ValueError('ci.env cannot override reserved LOCALCLOUD_ or AWS_ variables; use ci.principal for the caller')
    seed = settings.get('seed', {})
    if not isinstance(seed, dict) or set(seed) - {'buckets', 'queues'}:
        raise ValueError('ci.seed supports buckets and queues only')
    for key, names in seed.items():
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise ValueError('ci.seed resource names must be arrays of strings')
        if names and ('s3' if key == 'buckets' else 'sqs') not in services:
            raise ValueError('A seeded resource requires its service in ci.services')
        for name in names:
            if key == 'queues':
                valid_name(name)
            elif not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', name):
                raise ValueError('Invalid seed bucket name')
    policy_files = settings.get('policy_files', {})
    if not isinstance(policy_files, dict):
        raise ValueError('ci.policy_files must map principal names to JSON file paths')
    for principal, filename in policy_files.items():
        valid_name(principal)
        if principal == 'ci-smoke':
            raise ValueError('ci-smoke is reserved for the runner')
        if not isinstance(filename, str) or not filename:
            raise ValueError('Policy file paths must be nonempty strings')
    result = {**DEFAULTS, **settings, 'env': variables.copy(), 'seed': {'buckets': [], 'queues': [], **seed}}
    result['cwd'], result['config'] = str(path.parent), str(path)
    for key in ('artifacts', 'fixture'):
        value = result[key]
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(f'ci.{key} must be a nonempty path')
            result[key] = str((path.parent / Path(value).expanduser()).resolve())
    result['policy_files'] = {principal: str((path.parent / Path(filename).expanduser()).resolve()) for principal, filename in policy_files.items()}
    return result


def initialize(filename, command):
    path = Path(filename).expanduser()
    if not command or any(not isinstance(v, str) or not v or '\x00' in v for v in command):
        raise ValueError('Supply the application test command after --command')
    text = ('# LocalCloud project CI. Commands are argv arrays, not shell scripts.\n'
            'version = 1\n\n[ci]\ncommand = ' + json.dumps(command, ensure_ascii=False) + '\n'
            'services = ["s3", "sqs"]\niam = "soft"\ntimeout = 300\n'
            'artifacts = "artifacts/localcloud"\n')
    # Exclusive create: never replace an existing project configuration.
    with path.open('x', encoding='utf-8') as destination:
        destination.write(text)
    return path
