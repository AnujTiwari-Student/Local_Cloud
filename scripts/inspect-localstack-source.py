#!/usr/bin/env python3
"""Record or verify a bounded, non-executing inspection of public LocalStack source.

Usage: python3 scripts/inspect-localstack-source.py --source /path/to/checkout
Use --verify docs/research/localstack-source-manifest.json to check pinned bytes.
No upstream modules are imported, installed or executed.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

COMMIT = '8b9a79f05846835cf4dff63ab7eefdde9df83783'
FILES = ['LICENSE.txt', *['localstack-core/localstack/' + path for path in (
    'aws/app.py', 'aws/gateway.py', 'aws/chain.py', 'aws/api/core.py', 'aws/skeleton.py',
    'aws/handlers/service.py', 'aws/handlers/service_plugin.py', 'aws/protocol/service_router.py',
    'services/plugins.py', 'services/stores.py', 'services/ec2/provider.py', 'services/ec2/models.py',
    'services/sqs/provider.py', 'services/s3/provider.py', 'services/providers.py', 'services/moto.py',
    'services/ec2/patches.py')]]


def inspect(source, commit=COMMIT):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Commit must be a full 40-character Git SHA')
    files = []
    for relative in FILES:
        content = (Path(source) / relative).read_bytes()
        definitions = []
        for number, line in enumerate(content.decode().splitlines(), 1):
            match = re.match(r'^(class|def) ([A-Za-z_][A-Za-z_0-9]*)', line)
            if match:
                definitions.append({'kind': match[1], 'name': match[2], 'line': number})
        files.append({'path': relative, 'bytes': len(content),
                      'sha256': hashlib.sha256(content).hexdigest(),
                      'git_blob_sha1': hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest(),
                      'url': f'https://github.com/localstack/localstack/blob/{commit}/{relative}',
                      'definitions': definitions})
    return {'repository': 'https://github.com/localstack/localstack', 'commit': commit,
            'inspection': 'static source; no upstream module executed', 'files': files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--commit', default=COMMIT)
    parser.add_argument('--verify', help='Check exact file set and hashes against this source manifest')
    parser.add_argument('--output', help='Write inspection JSON, otherwise print it')
    args = parser.parse_args()
    try:
        result = inspect(args.source, args.commit)
        if args.verify:
            expected = json.loads(Path(args.verify).read_text())
            actual = {f['path']: f['sha256'] for f in result['files']}
            wanted = {f['path']: f['sha256'] for f in expected['files']}
            if expected['commit'] != args.commit or actual != wanted:
                raise ValueError('Reference identity mismatch: commit, file set or file bytes differ')
            result = {'verified': True, 'commit': args.commit, 'files': len(actual)}
        body = json.dumps(result, indent=2) + '\n'
        if args.output:
            Path(args.output).write_text(body)
        else:
            print(body, end='')
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
