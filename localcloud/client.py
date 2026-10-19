"""HTTP transport and private local profiles for the command-line client."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from .engine import Error, valid_name

MAX_BODY = 8 * 1024 * 1024


def endpoint_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Endpoint must be an HTTP(S) origin without credentials, path, query or fragment')
    # Validate the port before attempting a request.
    parsed.port
    return value.rstrip('/')


def private_write(path, content):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Profiles:
    def __init__(self, filename=None):
        self.path = Path(filename or os.environ.get('LOCALCLOUD_CONFIG') or
                         Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'localcloud' / 'config.json').expanduser()
        self.data = {'version': 1, 'profiles': {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if not isinstance(self.data, dict) or self.data.get('version') != 1 or not isinstance(self.data.get('profiles'), dict) or any(not isinstance(v, dict) for v in self.data['profiles'].values()):
                    raise ValueError()
                if any(not isinstance(v.get(k, ''), str) for v in self.data['profiles'].values() for k in ('endpoint_url', 'principal', 'token')):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('Invalid LocalCloud configuration file; repair it or choose --config-file') from None

    def get(self, name):
        valid_name(name)
        return self.data['profiles'].get(name, {}).copy()

    def update(self, name, changes):
        profile = self.get(name)
        for key, value in changes.items():
            if value is None:
                profile.pop(key, None)
            else:
                profile[key] = value
        self.data['profiles'][name] = profile
        private_write(self.path, json.dumps(self.data, indent=2) + '\n')


def token_from_kubernetes(secret, namespace, context=None):
    from .kube import dns_name
    dns_name(secret)
    dns_name(namespace)
    command = ['kubectl']
    if context:
        command += ['--context', context]
    command += ['-n', namespace, 'get', 'secret', secret, '-o', 'json', '--request-timeout=15s']
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        raise ValueError('kubectl is required for --kubernetes-secret') from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError('Could not read the Kubernetes Secret; check context, namespace, permissions and its name') from None
    try:
        token = base64.b64decode(json.loads(result.stdout)['data']['token'], validate=True).decode()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError('The Kubernetes Secret must contain a UTF-8 token key') from None
    return token


class Client:
    def __init__(self, endpoint, principal='test', token='', timeout=15):
        self.endpoint = endpoint_url(endpoint)
        valid_name(principal)
        self.principal, self.token, self.timeout = principal, token.strip(), timeout
        if not self.token.isascii() or any(c in self.token for c in '\r\n'):
            raise ValueError('Management token must be a single-line ASCII value')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, data=None, target=None, raw=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Invalid API path')
        headers = {'X-Local-Principal': self.principal}
        if path.startswith('/_local/') and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        if target:
            headers['X-Amz-Target'] = 'AmazonSQS.' + target
            headers['Content-Type'] = 'application/x-amz-json-1.0'
        if data is not None and len(data) > MAX_BODY:
            raise ValueError('Request exceeds the emulator limit of 8 MiB')
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError('Response exceeds the CLI limit of 16 MiB')
                if raw:
                    return payload
                if 'json' in response.headers.get('Content-Type', ''):
                    return json.loads(payload or b'{}')
                return payload
        except urllib.error.HTTPError as error:
            payload = error.read(65536)
            code, message = 'HTTPError', str(error.reason)
            try:
                value = json.loads(payload)
                code, message = value.get('__type', code), value.get('message', message)
            except (ValueError, AttributeError):
                try:
                    value = ET.fromstring(payload)
                    code, message = value.findtext('Code', code), value.findtext('Message', message)
                except ET.ParseError:
                    pass
            if error.code == 401:
                message += '; run localcloud auth login or set LOCALCLOUD_MANAGEMENT_TOKEN'
            raise Error(str(code), str(message), error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ValueError(f'Cannot reach {self.endpoint}; check that the backend or kubectl port-forward is running ({error.reason if isinstance(error, urllib.error.URLError) else type(error).__name__})') from None

    def sqs(self, operation, payload):
        return self.request('POST', '/', payload, target=operation)

    @staticmethod
    def s3_path(bucket='', key=''):
        path = '/' + urllib.parse.quote(bucket, safe='')
        if key:
            path += '/' + urllib.parse.quote(key, safe='/')
        return path

    def buckets(self):
        root = ET.fromstring(self.request('GET', '/'))
        return {'Buckets': [{'Name': item.findtext('{*}Name')} for item in root.findall('.//{*}Bucket')]}

    def objects(self, bucket, prefix=''):
        root = ET.fromstring(self.request('GET', self.s3_path(bucket) + '?list-type=2'))
        return {'Name': bucket, 'Contents': [{'Key': item.findtext('{*}Key'), 'Size': int(item.findtext('{*}Size', '0'))}
                                           for item in root.findall('{*}Contents') if item.findtext('{*}Key', '').startswith(prefix)]}
