"""Test-only stdin/stdout transport into the actual Python HTTP handler.

This permits cross-language contract checks where TCP sockets are unavailable.
It is never used by the example application or normal localcloud ci runs.
"""
import base64
import email.parser
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from localcloud.__main__ import Handler
from localcloud.engine import Engine
from localcloud.project import load_project


class Connection:
    def __init__(self, raw):
        self.raw, self.output = raw, bytearray()
    def makefile(self, *args):
        return io.BytesIO(self.raw)
    def sendall(self, data):
        self.output.extend(data)


def main():
    project = load_project(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(str(Path(directory) / 'state.db'), iam_mode=project['iam'])
        try:
            engine.policy('ci-smoke', {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}})
            for principal, filename in project['policy_files'].items():
                engine.policy(principal, json.loads(Path(filename).read_text()))
            for bucket in project['seed']['buckets']:
                engine.s3('PUT', bucket, '', principal='ci-smoke')
            for queue in project['seed']['queues']:
                engine.sqs('CreateQueue', {'QueueName': queue}, principal='ci-smoke')
            server = SimpleNamespace(engine=engine, management_token='bridge-test-token', executor=None)
            for line in sys.stdin:
                item = json.loads(line)
                url = urlsplit(item['url'])
                path = url.path + ('?' + url.query if url.query else '')
                body = base64.b64decode(item.get('body', ''))
                headers = {**item['headers'], 'Content-Length': str(len(body))}
                raw = (item['method'] + ' ' + path + ' HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers.items()) + '\r\n').encode() + body
                connection = Connection(raw)
                Handler(connection, ('127.0.0.1', 12345), server)
                head, payload = bytes(connection.output).split(b'\r\n\r\n', 1)
                status = int(head.split()[1])
                parsed = email.parser.BytesParser().parsebytes(head.split(b'\r\n', 1)[1])
                print(json.dumps({'id': item['id'], 'status': status, 'headers': dict(parsed.items()), 'body': base64.b64encode(payload).decode()}), flush=True)
        finally:
            engine.close()


if __name__ == '__main__':
    main()
