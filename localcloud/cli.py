"""AWS-style command interface for supported LocalCloud services and tools."""
import argparse
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from .client import Client, MAX_BODY, Profiles, private_write, token_from_kubernetes
from .engine import Error, valid_name


def json_value(value):
    if value.startswith('file://'):
        value = Path(value[7:]).expanduser().read_text()
    return json.loads(value)


def attributes(value):
    if value.startswith(('file://', '{')):
        result = json_value(value)
    else:
        try:
            result = dict(pair.split('=', 1) for pair in value.split(','))
        except ValueError:
            raise ValueError('Use Key=Value pairs, a JSON object, or file://attributes.json') from None
    if not isinstance(result, dict):
        raise ValueError('Attributes must be an object')
    return result


def read_body(filename):
    filename = filename.removeprefix('fileb://')
    with Path(filename).expanduser().open('rb') as stream:
        data = stream.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise ValueError('File exceeds the emulator limit of 8 MiB')
    return data


def s3_uri(value):
    if not value.startswith('s3://'):
        raise ValueError('Expected s3://bucket or s3://bucket/key')
    bucket, _, key = value[5:].partition('/')
    if not bucket or any(c in bucket for c in '?#@'):
        raise ValueError('Invalid S3 bucket URI')
    return bucket, key


def save_download(filename, data, key):
    path = Path(filename).expanduser()
    if path.is_dir() or str(filename).endswith('/'):
        basename = key.rsplit('/', 1)[-1]
        if not basename or basename in ('.', '..'):
            raise ValueError('Specify a destination filename for this object key')
        path = path / basename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def parser():
    common = argparse.ArgumentParser(add_help=False)
    for flag, options in [('--endpoint-url', {'help': 'Backend origin; default http://127.0.0.1:4566'}),
                          ('--profile', {'help': 'Saved profile name; default default'}),
                          ('--principal', {'help': 'Local IAM caller; default test'}),
                          ('--region', {'help': 'Only us-east-1 is supported'}),
                          ('--config-file', {'help': 'Override the local profiles file'}),
                          ('--output', {'choices': ['json', 'text']}),
                          ('--timeout', {'type': float, 'help': 'HTTP timeout in seconds; default 15'})]:
        common.add_argument(flag, default=argparse.SUPPRESS, **options)
    root = argparse.ArgumentParser(prog='localcloud', description='AWS-style CLI for LocalCloud S3, SQS and developer tools', parents=[common], allow_abbrev=False)
    groups = root.add_subparsers(dest='group', required=True)
    def group(name, help):
        item = groups.add_parser(name, help=help, parents=[common], allow_abbrev=False)
        return item.add_subparsers(dest='operation', required=True)
    def operation(group, name, **options):
        return group.add_parser(name, parents=[common], allow_abbrev=False, **options)
    def queue(op):
        selector = op.add_mutually_exclusive_group(required=True)
        selector.add_argument('--queue-url')
        selector.add_argument('--queue-name', help='Convenience alternative to --queue-url')
    for name, help in [('start', 'Start the local backend; use localcloud start --help'), ('ci', 'Run CI checks; use localcloud ci --help'), ('kube', 'Kubernetes operator and previews; use localcloud kube --help')]:
        groups.add_parser(name, help=help)
    groups.add_parser('status', help='Check backend health', parents=[common], allow_abbrev=False)
    groups.add_parser('resources', help='List resources and instance settings', parents=[common], allow_abbrev=False)
    configure = groups.add_parser('configure', help='Save endpoint and caller to a profile', parents=[common], allow_abbrev=False)
    configure.add_argument('--list', action='store_true', help='Show effective settings without revealing the token')
    auth = group('auth', 'Manage the management bearer token')
    login = operation(auth, 'login')
    source = login.add_mutually_exclusive_group()
    source.add_argument('--token-stdin', action='store_true')
    source.add_argument('--kubernetes-secret', help='Read the token key using your current kubectl context')
    login.add_argument('--namespace', default='localcloud')
    login.add_argument('--context')
    operation(auth, 'logout')
    operation(auth, 'status')
    s3 = group('s3', 'High-level S3 commands')
    for name in ('mb', 'rb', 'rm'):
        operation(s3, name).add_argument('uri')
    operation(s3, 'ls').add_argument('uri', nargs='?')
    cp = operation(s3, 'cp')
    cp.add_argument('source')
    cp.add_argument('destination')
    s3api = group('s3api', 'S3 API-style commands')
    operation(s3api, 'list-buckets')
    for name in ('create-bucket', 'delete-bucket', 'list-objects-v2', 'put-object', 'get-object', 'delete-object'):
        op = operation(s3api, name)
        op.add_argument('--bucket', required=True)
        if name in ('put-object', 'get-object', 'delete-object'):
            op.add_argument('--key', required=True)
        if name == 'put-object':
            op.add_argument('--body', required=True, help='Local file path')
        if name == 'get-object':
            op.add_argument('outfile')
        if name == 'list-objects-v2':
            op.add_argument('--prefix', default='')
    sqs = group('sqs', 'SQS operations with AWS-style flags')
    create = operation(sqs, 'create-queue')
    create.add_argument('--queue-name', required=True)
    create.add_argument('--attributes', default='{}', help='VisibilityTimeout=30, JSON, or file://path')
    listing = operation(sqs, 'list-queues')
    listing.add_argument('--queue-name-prefix')
    operation(sqs, 'get-queue-url').add_argument('--queue-name', required=True)
    for name in ('get-queue-attributes', 'delete-queue', 'purge-queue', 'send-message', 'receive-message', 'delete-message', 'change-message-visibility'):
        op = operation(sqs, name)
        queue(op)
        if name == 'get-queue-attributes':
            op.add_argument('--attribute-names', nargs='+', default=['All'])
        if name == 'send-message':
            op.add_argument('--message-body', required=True, help='Message text or file://path')
            op.add_argument('--delay-seconds', type=int)
        if name == 'receive-message':
            op.add_argument('--max-number-of-messages', type=int)
            op.add_argument('--visibility-timeout', type=int)
            op.add_argument('--wait-time-seconds', type=int, help='Only 0 is currently supported')
        if name in ('delete-message', 'change-message-visibility'):
            op.add_argument('--receipt-handle', required=True)
        if name == 'change-message-visibility':
            op.add_argument('--visibility-timeout', type=int, required=True)
    snapshots = group('snapshots', 'Named resource state snapshots')
    operation(snapshots, 'list')
    operation(snapshots, 'save').add_argument('name')
    operation(snapshots, 'restore', help='Replace current resource state and policies').add_argument('name')
    state = group('state', 'Transfer resource state as files')
    operation(state, 'export').add_argument('filename')
    operation(state, 'import', help='Validate and replace current resource state').add_argument('filename')
    iam = group('iam', 'Local policy management and proposals')
    put = operation(iam, 'put-policy')
    put.add_argument('--user-name', required=True, help='Local principal whose entire policy will be replaced')
    put.add_argument('--policy-document', required=True, help='JSON or file://policy.json')
    operation(iam, 'list-principals')
    generate = operation(iam, 'generate-policy')
    generate.add_argument('--user-name', required=True)
    generate.add_argument('--source', choices=['successful', 'violations'], default='successful')
    traces = group('traces', 'Inspect request metadata')
    operation(traces, 'list').add_argument('--user-name')
    operation(traces, 'clear')
    faults = group('faults', 'Configure service errors and latency')
    operation(faults, 'list')
    operation(faults, 'remove').add_argument('name')
    fault = operation(faults, 'add')
    fault.add_argument('--name', required=True)
    fault.add_argument('--service', choices=['s3', 'sqs', '*'], required=True)
    fault.add_argument('--operation', dest='fault_operation', default='*')
    fault.add_argument('--probability', type=float, default=1)
    fault.add_argument('--latency-ms', type=int, default=0)
    fault.add_argument('--error-code')
    fault.add_argument('--status-code', type=int, default=503)
    jobs = group('jobs', 'Run and inspect Kubernetes workloads')
    operation(jobs, 'list')
    for name in ('status', 'logs', 'delete'):
        operation(jobs, name).add_argument('name')
    job = operation(jobs, 'run')
    job.add_argument('--image', required=True)
    job.add_argument('--env', action='append', default=[], help='KEY=VALUE; repeat for more variables')
    job.add_argument('--timeout-seconds', type=int, default=300)
    job.add_argument('--ttl-seconds', type=int, default=600)
    job.add_argument('--command', nargs=argparse.REMAINDER, required=True, help='Workload argv; this option must come last')
    return root


def s3_command(args, client):
    op = args.operation
    if op == 'cp':
        source_remote, destination_remote = args.source.startswith('s3://'), args.destination.startswith('s3://')
        if not (source_remote or destination_remote):
            raise ValueError('At least one cp path must be an s3:// URI')
        if source_remote:
            source_bucket, source_key = s3_uri(args.source)
            if not source_key or source_key.endswith('/'):
                raise ValueError('Specify an object key; recursive transfers are not implemented')
            data = client.request('GET', client.s3_path(source_bucket, source_key), raw=True)
            basename = source_key.rsplit('/', 1)[-1]
        else:
            data = read_body(args.source)
            basename = Path(args.source).name
        if destination_remote:
            bucket, key = s3_uri(args.destination)
            if not key or key.endswith('/'):
                key += basename
            client.request('PUT', client.s3_path(bucket, key), data)
            destination = 's3://' + bucket + '/' + key
        else:
            destination = save_download(args.destination, data, source_key)
        verb = 'copy' if source_remote and destination_remote else ('download' if source_remote else 'upload')
        return {'source': args.source, 'destination': destination, 'bytes': len(data)}, f'{verb}: {args.source} to {destination}'
    if op == 'ls':
        if args.uri is None:
            result = client.buckets()
            return result, '\n'.join(b['Name'] for b in result['Buckets'])
        bucket, prefix = s3_uri(args.uri)
        result = client.objects(bucket, prefix)
        return result, '\n'.join(f"{o['Size']:>10} {o['Key']}" for o in result['Contents'])
    bucket, key = s3_uri(args.uri)
    if op in ('mb', 'rb') and key:
        raise ValueError('Specify a bucket URI without an object key')
    if op == 'rm' and not key:
        raise ValueError('rm needs an object key; use rb for an empty bucket')
    client.request('PUT' if op == 'mb' else 'DELETE', client.s3_path(bucket, key), b'' if op == 'mb' else None)
    return {'Bucket': bucket, **({'Key': key} if key else {})}, f"{ {'mb':'make_bucket','rb':'remove_bucket','rm':'delete'}[op]}: {args.uri}"


def execute(args, client):
    group, op = args.group, getattr(args, 'operation', None)
    if group == 'status':
        return client.request('GET', '/_local/health')
    if group == 'resources':
        return client.request('GET', '/_local/resources')
    if group == 's3api':
        if op == 'list-buckets':
            return client.buckets()
        if op == 'list-objects-v2':
            return client.objects(args.bucket, args.prefix)
        key = getattr(args, 'key', '')
        if op in ('get-object', 'put-object', 'delete-object') and not key:
            raise ValueError('Object key cannot be empty')
        path = client.s3_path(args.bucket, key)
        if op == 'get-object':
            data = client.request('GET', path, raw=True)
            save_download(args.outfile, data, key)
            return {'ContentLength': len(data)}
        if op == 'put-object':
            data = read_body(args.body)
            client.request('PUT', path, data)
            return {'ETag': '"' + hashlib.md5(data).hexdigest() + '"'}
        return client.request('PUT' if op == 'create-bucket' else 'DELETE', path, b'' if op == 'create-bucket' else None) or {}
    if group == 'sqs':
        payload = {}
        if op in ('create-queue', 'get-queue-url'):
            payload['QueueName'] = args.queue_name
        elif op != 'list-queues':
            if getattr(args, 'queue_name', None):
                valid_name(args.queue_name)
            payload['QueueUrl'] = args.queue_url or client.endpoint + '/000000000000/' + args.queue_name
        if op == 'create-queue':
            payload['Attributes'] = attributes(args.attributes)
        if op == 'send-message':
            payload['MessageBody'] = Path(args.message_body[7:]).expanduser().read_text() if args.message_body.startswith('file://') else args.message_body
        fields = {'queue_name_prefix': 'QueueNamePrefix', 'attribute_names': 'AttributeNames', 'delay_seconds': 'DelaySeconds', 'max_number_of_messages': 'MaxNumberOfMessages', 'visibility_timeout': 'VisibilityTimeout', 'wait_time_seconds': 'WaitTimeSeconds', 'receipt_handle': 'ReceiptHandle'}
        for attribute, name in fields.items():
            value = getattr(args, attribute, None)
            if value is not None:
                payload[name] = value
        return client.sqs(''.join(word.title() for word in op.split('-')), payload)
    if group == 'snapshots':
        if op == 'list':
            return client.request('GET', '/_local/resources')['snapshots']
        return client.request('POST', '/_local/snapshots' + ('/restore' if op == 'restore' else ''), {'name': args.name})
    if group == 'state':
        if op == 'export':
            bundle = client.request('GET', '/_local/state')
            private_write(args.filename, json.dumps(bundle, indent=2) + '\n')
            return {'exported': str(Path(args.filename).expanduser())}
        return client.request('POST', '/_local/state/import', json.loads(Path(args.filename).expanduser().read_text()))
    if group == 'iam':
        if op == 'list-principals':
            return client.request('GET', '/_local/resources')['principals']
        if op == 'put-policy':
            return client.request('POST', '/_local/policies', {'principal': args.user_name, 'policy': json_value(args.policy_document)})
        return client.request('GET', '/_local/policy-stream?' + urllib.parse.urlencode({'principal': args.user_name, 'source': args.source}))
    if group == 'traces':
        if op == 'clear':
            return client.request('POST', '/_local/traces/clear', {})
        return client.request('GET', '/_local/traces' + ('?' + urllib.parse.urlencode({'principal': args.user_name}) if args.user_name else ''))
    if group == 'faults':
        if op == 'list':
            return client.request('GET', '/_local/faults')
        if op == 'remove':
            return client.request('POST', '/_local/faults/delete', {'name': args.name})
        rule = {'name': args.name, 'service': args.service, 'operation': args.fault_operation, 'probability': args.probability, 'latency_ms': args.latency_ms}
        if args.error_code:
            rule['error'] = {'code': args.error_code, 'status': args.status_code}
        return client.request('POST', '/_local/faults', rule)
    if group == 'jobs':
        if op == 'list':
            return client.request('GET', '/_local/executions')
        if op == 'run':
            if not args.command:
                raise ValueError('--command requires an executable and optional arguments')
            env = {}
            for item in args.env:
                key, separator, value = item.partition('=')
                if not separator or not key:
                    raise ValueError('--env requires KEY=VALUE')
                env[key] = value
            return client.request('POST', '/_local/executions', {'image': args.image, 'command': args.command, 'env': env, 'timeoutSeconds': args.timeout_seconds, 'ttlSeconds': args.ttl_seconds})
        path = '/_local/executions/' + urllib.parse.quote(args.name, safe='')
        return client.request('DELETE' if op == 'delete' else 'GET', path + ('/logs' if op == 'logs' else ''))
    raise ValueError('Unsupported command')


def emit(value, output='json'):
    if output == 'json':
        print(json.dumps(value, indent=2))
    elif isinstance(value, dict):
        print('\t'.join(json.dumps(v) if isinstance(v, (dict, list)) else str(v) for v in value.values()))
    elif isinstance(value, list):
        for row in value:
            emit(row, 'text')
    else:
        print(value)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # These commands keep their own complete parsers and backward-compatible module entry points.
    if argv and argv[0] in ('start', 'ci', 'kube'):
        command, rest = argv[0], argv[1:]
        if command == 'start':
            from .__main__ import main as delegate
        elif command == 'ci':
            from .ci import main as delegate
            if rest[:1] == ['run']:
                rest = rest[1:]
        else:
            from .operator import main as delegate
        return delegate(rest)
    args = parser().parse_args(argv)
    try:
        profiles = Profiles(getattr(args, 'config_file', None))
        name = getattr(args, 'profile', os.environ.get('LOCALCLOUD_PROFILE', 'default'))
        saved = profiles.get(name)
        endpoint = getattr(args, 'endpoint_url', None) or os.environ.get('LOCALCLOUD_URL') or os.environ.get('LOCALCLOUD_ENDPOINT_URL') or saved.get('endpoint_url', 'http://127.0.0.1:4566')
        principal = getattr(args, 'principal', None) or os.environ.get('LOCALCLOUD_PRINCIPAL') or saved.get('principal', 'test')
        token = os.environ.get('LOCALCLOUD_MANAGEMENT_TOKEN') or saved.get('token', '')
        if getattr(args, 'region', 'us-east-1') != 'us-east-1':
            raise ValueError('Only us-east-1 is implemented; --region cannot enable additional regions')
        timeout = getattr(args, 'timeout', 15)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('--timeout must be positive')
        client = Client(endpoint, principal, token, timeout)
        output = getattr(args, 'output', None)
        if args.group == 'configure':
            if not args.list:
                profiles.update(name, {'endpoint_url': client.endpoint, 'principal': principal})
            emit({'profile': name, 'endpoint_url': client.endpoint, 'principal': principal, 'token_configured': bool(token), 'config_file': str(profiles.path)}, output or 'json')
            return 0
        if args.group == 'auth':
            if args.operation == 'login':
                if args.kubernetes_secret:
                    token = token_from_kubernetes(args.kubernetes_secret, args.namespace, args.context)
                elif args.token_stdin:
                    token = sys.stdin.read(65537).strip()
                else:
                    token = getpass.getpass('Management token: ').strip()
                if not token or len(token) > 65536:
                    raise ValueError('Management token is empty or too long')
                client = Client(endpoint, principal, token, timeout)
                # Verify before saving, so a bad pasted token never replaces working credentials.
                client.request('GET', '/_local/resources')
                profiles.update(name, {'token': client.token, 'endpoint_url': client.endpoint, 'principal': principal})
                if os.environ.get('LOCALCLOUD_MANAGEMENT_TOKEN') and os.environ['LOCALCLOUD_MANAGEMENT_TOKEN'].strip() != client.token:
                    print('LOCALCLOUD_MANAGEMENT_TOKEN overrides the saved token; unset it to use this login.', file=sys.stderr)
                result = {'profile': name, 'authenticated': True, 'token_saved': True}
            elif args.operation == 'logout':
                profiles.update(name, {'token': None})
                result = {'profile': name, 'token_removed': True, 'environment_token_present': bool(os.environ.get('LOCALCLOUD_MANAGEMENT_TOKEN'))}
            else:
                client.request('GET', '/_local/resources')
                result = {'profile': name, 'management_access': True, 'token_configured': bool(token)}
            emit(result, output or 'json')
            return 0
        if args.group == 's3':
            result, message = s3_command(args, client)
            if output == 'json':
                emit(result)
            else:
                print(message)
        else:
            result = execute(args, client)
            if args.group == 'jobs' and args.operation == 'logs' and output != 'json':
                print('Pod pending' if result.get('pending') else result.get('logs', ''), end='\n' if result.get('pending') or not result.get('logs', '').endswith('\n') else '')
            else:
                emit(result, output or 'json')
        return 0
    except Error as error:
        print(f'localcloud: {error.code}: {error.message} (HTTP {error.status})', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
        print('localcloud: ' + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('localcloud: cancelled', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
