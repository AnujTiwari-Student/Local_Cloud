"""Per-instance IMDS sidecar; never exposes management credentials."""
import base64
import fcntl
import json
import os
import secrets
import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Metadata:
    def __init__(self, instance, credentials=None):
        self.instance, self.tokens = instance, {}
        self.credentials = credentials
        self.lock = threading.Lock()

    def request(self, method, path, headers):
        instance = self.instance
        options = instance.get('MetadataOptions') or {}
        if options.get('HttpEndpoint', 'enabled') == 'disabled':
            return 404, ''
        if path == '/latest/api/token' and method == 'PUT':
            if headers.get('X-Forwarded-For'):
                return 403, ''
            try:
                ttl = int(headers.get('X-aws-ec2-metadata-token-ttl-seconds', ''))
                if not 1 <= ttl <= 21600:
                    raise ValueError()
            except (ValueError, TypeError):
                return 400, ''
            token = secrets.token_urlsafe(48)
            with self.lock:
                self.tokens = {k: v for k, v in self.tokens.items() if v > time.monotonic()}
                if len(self.tokens) >= 1000:
                    return 429, ''
                self.tokens[token] = time.monotonic() + ttl
            return 200, token
        if method != 'GET':
            return 405, ''
        supplied = headers.get('X-aws-ec2-metadata-token')
        if supplied or options.get('HttpTokens') == 'required':
            with self.lock:
                if self.tokens.get(supplied, 0) <= time.monotonic():
                    return 401, ''
        # Credentials are fetched only after IMDS endpoint/token checks. The
        # callback uses an instance-scoped capability, never management auth.
        if path.startswith('/latest/meta-data/iam'):
            if path not in ('/latest/meta-data/iam', '/latest/meta-data/iam/', '/latest/meta-data/iam/info', '/latest/meta-data/iam/security-credentials', '/latest/meta-data/iam/security-credentials/') and not path.startswith('/latest/meta-data/iam/security-credentials/'):
                return 404, ''
            if not self.credentials:
                return 404, ''
            try:
                bundle = self.credentials()
            except Exception as error:
                from .engine import Error
                if isinstance(error, Error):
                    return error.status, ''
                return 503, ''
            if path in ('/latest/meta-data/iam', '/latest/meta-data/iam/'):
                return 200, 'info\nsecurity-credentials/'
            if path == '/latest/meta-data/iam/info':
                return 200, json.dumps(bundle['Info'])
            if path in ('/latest/meta-data/iam/security-credentials', '/latest/meta-data/iam/security-credentials/'):
                return 200, bundle['RoleName']
            if path == '/latest/meta-data/iam/security-credentials/' + bundle['RoleName']:
                return 200, json.dumps(bundle['Credentials'])
            return 404, ''
        placement = instance.get('Placement') or {}
        values = {'instance-id': instance['InstanceId'], 'ami-id': instance['ImageId'], 'instance-type': instance['InstanceType'],
                  'local-ipv4': instance.get('PrivateIpAddress', ''), 'hostname': instance.get('PrivateDnsName', ''),
                  'local-hostname': instance.get('PrivateDnsName', ''), 'public-ipv4': instance.get('PublicIpAddress', ''),
                  'placement/availability-zone': placement.get('AvailabilityZone', 'us-east-1a'), 'placement/region': 'us-east-1',
                  'reservation-id': instance.get('ReservationId', ''), 'ami-launch-index': str(instance.get('AmiLaunchIndex', 0))}
        if path == '/latest/dynamic/instance-identity/document':
            return 200, json.dumps({'accountId': '000000000000', 'architecture': 'x86_64', 'availabilityZone': placement.get('AvailabilityZone', 'us-east-1a'),
                                    'imageId': instance['ImageId'], 'instanceId': instance['InstanceId'], 'instanceType': instance['InstanceType'],
                                    'privateIp': instance.get('PrivateIpAddress', ''), 'region': 'us-east-1', 'version': '2017-09-30'})
        if path == '/latest/user-data':
            return (200, base64.b64decode(instance['UserData'])) if instance.get('UserData') else (404, '')
        prefix = '/latest/meta-data/'
        if path == '/latest/meta-data':
            path += '/'
        if path.startswith(prefix):
            key = path[len(prefix):]
            if key in values and values[key]:
                return 200, values[key]
            if not key or key.endswith('/'):
                names = sorted({name[len(key):].split('/')[0] + ('/' if '/' in name[len(key):] else '') for name in values if name.startswith(key) and values[name]})
                if names:
                    if not key and self.credentials:
                        try:
                            self.credentials()
                            names.append('iam/')
                        except Exception:
                            pass
                    return 200, '\n'.join(sorted(names))
        return 404, ''


def broker_provider(environment=None):
    """Return a dynamic provider with a bounded initial-transaction fallback."""
    from urllib.request import Request, build_opener, ProxyHandler
    from urllib.error import HTTPError, URLError
    from datetime import datetime, timezone
    from .engine import Error
    environment = os.environ if environment is None else environment
    url, token = environment.get('LOCALCLOUD_IMDS_BROKER_URL'), environment.get('LOCALCLOUD_IMDS_BROKER_TOKEN')
    if not url or not token:
        return None
    opener = build_opener(ProxyHandler({}))
    initial = json.loads(environment.get('LOCALCLOUD_IMDS_INITIAL_CREDENTIALS', 'null'))
    started = time.monotonic()
    def startup_fallback():
        if initial and time.monotonic() - started <= 30:
            try:
                expiry = datetime.fromisoformat(initial['Credentials']['Expiration'].replace('Z', '+00:00'))
                if expiry > datetime.now(timezone.utc):
                    return initial
            except (KeyError, ValueError, TypeError):
                pass
        raise Error('MetadataCredentialsUnavailable', 'Instance credential broker unavailable', 503)
    def fetch():
        try:
            with opener.open(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=.5) as response:
                return json.loads(response.read(65536))
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 503:
                # The synchronous Docker launch transaction has not committed
                # yet. Broker returns immediately rather than deadlocking it.
                return startup_fallback()
            # Explicit detach, expired capability and trust denial never use
            # stale fallback credentials.
            raise Error('MetadataCredentialsUnavailable', 'Instance credentials unavailable', code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            return startup_fallback()
    return fetch


def main():
    instance = json.loads(os.environ['LOCALCLOUD_IMDS_DATA'])
    metadata = Metadata(instance, broker_provider())
    # NET_ADMIN is restricted to the guest's network namespace, never the host.
    # Add a loopback alias so guest requests use the standard link-local IP.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        address = struct.pack('16sH2s4s8s', b'lo:lcimds', socket.AF_INET, b'\0' * 2, socket.inet_aton('169.254.169.254'), b'\0' * 8)
        fcntl.ioctl(sock, 0x8916, address)  # SIOCSIFADDR
        mask = struct.pack('16sH2s4s8s', b'lo:lcimds', socket.AF_INET, b'\0' * 2, socket.inet_aton('255.255.255.255'), b'\0' * 8)
        fcntl.ioctl(sock, 0x891c, mask)  # SIOCSIFNETMASK
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def dispatch(self):
            status, body = metadata.request(self.command, urlsplit(self.path).path, self.headers)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = do_POST = do_DELETE = dispatch
    class Server(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer normally reverse-resolves the address before listen().
            # Link-local IMDS has no DNS record; that lookup can stall startup.
            socketserver.TCPServer.server_bind(self)
            self.server_name = 'localcloud-imds'
            self.server_port = self.server_address[1]
    Server(('169.254.169.254', 80), Handler).serve_forever()


if __name__ == '__main__':
    main()
