import argparse
import hmac
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape
from .engine import Engine, Error


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self):
        engine = self.server.engine
        path = unquote(urlsplit(self.path).path)
        size = int(self.headers.get('Content-Length', 0))
        if size < 0 or size > 8 * 1024 * 1024:
            raise Error('RequestTooLarge', 'Maximum request size is 8 MiB', 413)
        body = self.rfile.read(size)
        principal = self.headers.get('X-Local-Principal', 'test')
        if path == '/_local' and self.command == 'GET':
            return self.send(200, Path(__file__).with_name('dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        if path.startswith('/_local/'):
            required_token = getattr(self.server, 'management_token', None)
            if required_token and path != '/_local/health':
                supplied = self.headers.get('Authorization', '')
                if not hmac.compare_digest(supplied.encode(), ('Bearer ' + required_token).encode()):
                    raise Error('Unauthorized', 'A valid management bearer token is required', 401)
            if path == '/_local/executions' or path.startswith('/_local/executions/'):
                executor = getattr(self.server, 'executor', None)
                if executor is None:
                    raise Error('ExecutorDisabled', 'Start with --executor kubernetes and an image allowlist', 503)
                parts = path.rstrip('/').split('/')
                if len(parts) == 3 and self.command == 'GET':
                    return self.send(200, executor.list())
                if len(parts) == 3 and self.command == 'POST':
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise Error('InvalidRequest', 'Execution submission requires application/json', 415)
                    return self.send(201, executor.submit(json.loads(body or b'{}')))
                if len(parts) == 4 and self.command == 'GET':
                    return self.send(200, executor.status(parts[3]))
                if len(parts) == 4 and self.command == 'DELETE':
                    return self.send(200, executor.cancel(parts[3]))
                if len(parts) == 5 and parts[4] == 'logs' and self.command == 'GET':
                    return self.send(200, executor.logs(parts[3]))
                raise Error('NotFound', 'Unknown execution route', 404)
            query = parse_qs(urlsplit(self.path).query)
            if self.command == 'GET' and path == '/_local/state':
                return self.send(200, engine.tools.export_state())
            if self.command == 'GET' and path == '/_local/traces':
                return self.send(200, engine.tools.traces(query.get('principal', [None])[0]))
            if self.command == 'GET' and path == '/_local/policy-stream':
                return self.send(200, engine.tools.generated_policy(query.get('principal', ['test'])[0], query.get('source', ['successful'])[0]))
            if self.command == 'GET' and path == '/_local/faults':
                return self.send(200, engine.tools.rules())
            if self.command == 'GET' and path == '/_local/resources':
                overview = engine.overview()
                executor = getattr(self.server, 'executor', None)
                overview['executor'] = executor is not None
                overview['workloadImages'] = sorted(executor.images) if executor is not None else []
                return self.send(200, overview)
            if self.command == 'GET' and path == '/_local/health':
                return self.send(200, {'status': 'running', 'services': ['sqs', 's3']})
            if self.command == 'POST':
                # Browser management mutations must be JSON, preventing simple cross-origin form posts.
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise Error('InvalidRequest', 'Management requests require application/json', 415)
                data = json.loads(body or b'{}')
                if path == '/_local/state/import':
                    return self.send(200, engine.tools.import_state(data))
                if path == '/_local/traces/clear':
                    engine.tools.clear_traces()
                    return self.send(200, {'cleared': True})
                if path == '/_local/faults':
                    return self.send(200, engine.tools.set_rule(data))
                if path == '/_local/faults/delete':
                    engine.tools.delete_rule(data['name'])
                    return self.send(200, {'deleted': True})
                if path == '/_local/snapshots':
                    return self.send(201, engine.snapshot(data['name']))
                if path == '/_local/snapshots/restore':
                    return self.send(200, engine.snapshot(data['name'], restore=True))
                if path == '/_local/policies':
                    engine.policy(data['principal'], data['policy'])
                    return self.send(200, {'saved': True})
            raise Error('NotFound', 'Unknown management route', 404)
        target = self.headers.get('X-Amz-Target', '')
        if target.startswith('AmazonSQS.') and self.command == 'POST':
            data = json.loads(body or b'{}')
            if not isinstance(data, dict):
                raise Error('InvalidRequest', 'Expected a JSON object')
            return self.send(200, engine.sqs(target.split('.', 1)[1], data, principal), 'application/x-amz-json-1.0')
        if self.command == 'POST':
            raise Error('UnsupportedOperation', 'Only SQS JSON protocol is supported for POST', 501)
        if urlsplit(self.path).query and urlsplit(self.path).query not in ('list-type=2',):
            raise Error('UnsupportedOperation', 'S3 query options are unsupported', 501)
        parts = path.lstrip('/').split('/', 1)
        bucket, key = parts[0], parts[1] if len(parts) > 1 else ''
        result = engine.s3(self.command, bucket, key, body, principal)
        if isinstance(result, bytes):
            return self.send(200, result, 'application/octet-stream')
        if self.command == 'GET':
            if not bucket:
                xml = '<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Buckets>' + ''.join('<Bucket><Name>' + escape(n) + '</Name></Bucket>' for n in result['buckets']) + '</Buckets></ListAllMyBucketsResult>'
            else:
                xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>' + escape(bucket) + '</Name><IsTruncated>false</IsTruncated>' + ''.join('<Contents><Key>' + escape(o['key']) + '</Key><Size>' + str(o['size']) + '</Size></Contents>' for o in result['objects']) + '</ListBucketResult>'
            return self.send(200, xml, 'application/xml')
        return self.send(204 if self.command == 'DELETE' else 200, b'')

    def dispatch(self):
        try:
            self.handle_request()
        except Error as e:
            if self.headers.get('X-Amz-Target', '').startswith('AmazonSQS.') or self.path.startswith('/_local'):
                self.send(e.status, {'__type': e.code, 'message': e.message})
            else:
                self.send(e.status, f'<Error><Code>{escape(e.code)}</Code><Message>{escape(e.message)}</Message></Error>', 'application/xml')
        except (ValueError, KeyError, TypeError, AttributeError):
            self.send(400, {'__type': 'InvalidRequest', 'message': 'Malformed request or missing required field'})

    do_GET = do_POST = do_PUT = do_DELETE = dispatch


def main():
    parser = argparse.ArgumentParser(description='LocalCloud development emulator')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4566)
    parser.add_argument('--data', default='.localcloud/state.sqlite3')
    parser.add_argument('--iam', choices=['off', 'soft', 'enforce'], default='off')
    parser.add_argument('--load-state', help='Import a LocalCloud JSON bundle before serving')
    parser.add_argument('--export-state', help='Write a JSON state bundle to this file and exit')
    parser.add_argument('--public-endpoint', help='Advertised service URL used in QueueUrl responses and workloads')
    parser.add_argument('--management-token-env', help='Environment variable containing a required management bearer token')
    parser.add_argument('--executor', choices=['disabled', 'kubernetes'], default='disabled')
    parser.add_argument('--kube-api-server', help='Loopback kubectl proxy URL; default uses in-cluster credentials')
    parser.add_argument('--kube-namespace', default='localcloud')
    parser.add_argument('--workload-image', action='append', default=[], help='Exact allowed workload image; repeat to allow more')
    parser.add_argument('--instance-name', default='localcloud')
    parser.add_argument('--instance-uid', help='Optional LocalCloudInstance UID for workload ownership')
    args = parser.parse_args()
    management_token = os.environ.get(args.management_token_env or 'LOCALCLOUD_MANAGEMENT_TOKEN')
    if args.management_token_env and not management_token:
        parser.error('The specified management token environment variable is empty')
    if args.executor == 'kubernetes' and (not management_token or not args.workload_image):
        parser.error('Kubernetes execution requires a management token and at least one --workload-image')
    if args.public_endpoint:
        endpoint_url = urlsplit(args.public_endpoint)
        if endpoint_url.scheme not in ('http', 'https') or not endpoint_url.netloc or endpoint_url.username or endpoint_url.password or endpoint_url.query or endpoint_url.fragment or endpoint_url.path not in ('', '/'):
            parser.error('Public endpoint must be an HTTP(S) origin without credentials, path or query')
    Path(args.data).parent.mkdir(parents=True, exist_ok=True)
    endpoint = args.public_endpoint or f'http://{args.host}:{args.port}'
    engine = Engine(args.data, endpoint, args.iam)
    if args.load_state:
        try:
            engine.tools.import_state(json.loads(Path(args.load_state).read_text()))
        except (Error, OSError, ValueError) as error:
            engine.close()
            parser.error(str(error))
    if args.export_state:
        try:
            Path(args.export_state).write_text(json.dumps(engine.tools.export_state(), indent=2) + '\n')
        finally:
            engine.close()
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.engine = engine
    server.management_token = management_token
    server.executor = None
    engine.endpoint = args.public_endpoint.rstrip('/') if args.public_endpoint else f'http://{args.host}:{server.server_port}'
    if args.executor == 'kubernetes':
        from .kube import KubeClient, KubernetesExecutor
        try:
            server.executor = KubernetesExecutor(KubeClient(args.kube_api_server), args.kube_namespace,
                                                 engine.endpoint, args.workload_image, args.instance_name, args.instance_uid)
        except (Error, ValueError, OSError) as error:
            server.server_close()
            engine.close()
            parser.error(str(error))
    endpoint = engine.endpoint
    print(f'LocalCloud dashboard: {endpoint}/_local', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()


if __name__ == '__main__':
    main()
