"""Role sessions must authenticate real HTTP requests and retain their policies."""
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from types import SimpleNamespace
import test_http

from localcloud.__main__ import Handler
from localcloud.engine import Engine
from localcloud.cli import parser, execute


def signed_request(endpoint, credentials, method='GET', path='/', body=b'', service='s3', extra=None):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    day = stamp[:8]
    headers = {'host': urllib.parse.urlsplit(endpoint).netloc, 'x-amz-date': stamp,
               'x-amz-security-token': credentials['SessionToken']}
    if extra: headers.update(extra)
    names = ';'.join(sorted(headers))
    canonical_headers = ''.join(key + ':' + headers[key] + '\n' for key in sorted(headers))
    canonical = '\n'.join((method, path, '', canonical_headers, names, hashlib.sha256(body).hexdigest()))
    scope = day + '/us-east-1/' + service + '/aws4_request'
    text = 'AWS4-HMAC-SHA256\n' + stamp + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
    key = ('AWS4' + credentials['SecretAccessKey']).encode()
    for part in (day, 'us-east-1', service, 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    headers['Authorization'] = 'AWS4-HMAC-SHA256 Credential=' + credentials['AccessKeyId'] + '/' + scope + ', SignedHeaders=' + names + ', Signature=' + hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
    return urllib.request.Request(endpoint + path, data=body if method != 'GET' else None, method=method, headers=headers)


class IdentityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.tmp.name + '/state.db')
        self.server = SimpleNamespace(engine=self.engine, management_token='management-secret')
        self.endpoint = 'http://127.0.0.1:4566'
        self.engine.identity.call('iam', 'CreateRole', {'RoleName': 'reader', 'AssumeRolePolicyDocument': {'Statement': [{'Effect': 'Allow', 'Action': 'sts:AssumeRole', 'Principal': {'AWS': 'arn:aws:iam::000000000000:user/test'}}]}})
        self.engine.identity.call('iam', 'PutRolePolicy', {'RoleName': 'reader', 'PolicyName': 'read', 'PolicyDocument': {'Statement': [{'Effect': 'Allow', 'Action': 's3:ListAllMyBuckets', 'Resource': '*'}]}})
        self.credentials = self.engine.identity.call('sts', 'AssumeRole', {'RoleArn': 'arn:aws:iam::000000000000:role/reader', 'RoleSessionName': 'example'})['Credentials']
        self.engine.iam_mode = 'enforce'

    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()

    def request(self, request):
        parsed = urllib.parse.urlsplit(request.full_url)
        return test_http.HTTPTests.request(self, (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), request.data or b'', request.get_method(), dict(request.header_items()))

    def test_role_signed_read_allowed_write_denied_and_header_cannot_change_role(self):
        status, _ = self.request(signed_request(self.endpoint, self.credentials))
        self.assertEqual(status, 200)
        request = signed_request(self.endpoint, self.credentials, 'PUT', '/forbidden')
        request.add_header('X-Local-Principal', 'test')
        status, body = self.request(request)
        self.assertEqual(status, 403); self.assertIn(b'AccessDenied', body)
        self.assertEqual(self.engine.tools.traces()[0]['principal'], 'arn:aws:iam::000000000000:role/reader')

    def test_bad_token_signature_expiration_and_wrong_service_are_rejected(self):
        bad = dict(self.credentials, SessionToken='wrong')
        self.assertEqual(self.request(signed_request(self.endpoint, bad))[0], 403)
        bad = dict(self.credentials, SecretAccessKey='wrong')
        status, body = self.request(signed_request(self.endpoint, bad))
        self.assertEqual(status, 403); self.assertIn(b'SignatureDoesNotMatch', body)
        self.assertEqual(self.request(signed_request(self.endpoint, self.credentials, service='ec2'))[0], 403)
        with self.engine.lock, self.engine.db:
            self.engine.db.execute('UPDATE identity_sessions SET expiration=0')
        status, body = self.request(signed_request(self.endpoint, self.credentials))
        self.assertEqual(status, 403); self.assertIn(b'ExpiredToken', body)

    def test_query_sts_caller_identity_and_policy_document_roundtrip(self):
        body = urllib.parse.urlencode({'Action': 'GetCallerIdentity', 'Version': '2011-06-15'}).encode()
        request = signed_request(self.endpoint, self.credentials, 'POST', '/', body, 'sts', {'content-type': 'application/x-www-form-urlencoded'})
        status, payload = self.request(request)
        self.assertEqual(status, 200, payload)
        root = ET.fromstring(payload)
        self.assertEqual(root.findtext('.//{*}Arn'), 'arn:aws:sts::000000000000:assumed-role/reader/example')
        self.engine.iam_mode = 'off'
        body = urllib.parse.urlencode({'Action': 'GetRole', 'Version': '2010-05-08', 'RoleName': 'reader'}).encode()
        request = urllib.request.Request(self.endpoint, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Bearer management-secret'})
        status, payload = self.request(request)
        self.assertEqual(status, 200)
        document = ET.fromstring(payload).findtext('.//{*}AssumeRolePolicyDocument')
        self.assertEqual(json.loads(urllib.parse.unquote(document))['Statement'][0]['Principal']['AWS'], ['arn:aws:iam::000000000000:user/test'])

    def test_broker_capability_cannot_access_other_instance_or_management_routes(self):
        with self.engine.transaction() as state:
            state['instances'] = {}
            capability = self.engine.identity.instance_broker('i-00000000000000001')
        def get(path, token): return self.request(urllib.request.Request(self.endpoint + path, headers={'Authorization': 'Bearer ' + token}))
        self.assertEqual(get('/_local/instance-credentials/i-00000000000000002', capability)[0], 401)
        self.assertEqual(get('/_local/instance-credentials/i-00000000000000001', 'management-secret')[0], 401)
        self.assertEqual(get('/_local/resources', capability)[0], 401)
        self.assertEqual(get('/_local/instance-credentials/i-00000000000000001', capability)[0], 404)

    def test_identity_cli_builds_management_payloads(self):
        class Capture:
            def request(self, *args): return args
        args = parser().parse_args(['iam', 'create-role', '--role-name', 'worker', '--assume-role-policy-document', '{"Statement":[]}'])
        self.assertEqual(execute(args, Capture()), ('POST', '/_local/iam', {'Action': 'CreateRole', 'RoleName': 'worker', 'AssumeRolePolicyDocument': {'Statement': []}}))
        args = parser().parse_args(['sts', 'get-caller-identity'])
        self.assertEqual(execute(args, Capture()), ('POST', '/_local/sts', {'Action': 'GetCallerIdentity'}))
