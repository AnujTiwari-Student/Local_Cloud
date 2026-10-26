"""Bounded AWS Query codecs and SigV4 verification for local role sessions."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

from .engine import Error


def query_request(body):
    fields = {}
    for key, value in parse_qsl(body.decode(), keep_blank_values=True):
        if key in fields:
            raise Error('InvalidParameterValue', 'Duplicate Query parameter')
        fields[key] = value
    operation = fields.pop('Action', '')
    version = fields.pop('Version', '')
    service = {'2010-05-08': 'iam', '2011-06-15': 'sts'}.get(version)
    if not service:
        raise Error('InvalidAction', 'Unsupported IAM/STS Query version')
    for key in ('PolicyDocument', 'AssumeRolePolicyDocument'):
        if key in fields:
            fields[key] = json.loads(fields[key])
    for key in ('DurationSeconds', 'MaxSessionDuration', 'MaxItems'):
        if key in fields:
            try:
                fields[key] = int(fields[key])
            except ValueError:
                raise Error('InvalidParameterValue', key + ' must be an integer') from None
    return service, operation, fields


def query_response(service, operation, result, request_id):
    namespace = 'https://iam.amazonaws.com/doc/2010-05-08/' if service == 'iam' else 'https://sts.amazonaws.com/doc/2011-06-15/'
    root = Element(operation + 'Response', xmlns=namespace)
    output = SubElement(root, operation + 'Result')
    def write(node, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if item is not None:
                    child = SubElement(node, key)
                    if key in ('AssumeRolePolicyDocument', 'PolicyDocument'):
                        child.text = quote(json.dumps(item, separators=(',', ':')), safe='')
                    else:
                        write(child, item)
        elif isinstance(value, list):
            for item in value:
                write(SubElement(node, 'member'), item)
        else:
            node.text = str(value).lower() if isinstance(value, bool) else str(value)
    write(output, result)
    SubElement(SubElement(root, 'ResponseMetadata'), 'RequestId').text = request_id
    return tostring(root, encoding='utf-8')


def verify_session(engine, method, url, headers, body):
    """Return a verified local session, or None for the legacy test caller.

    This is not verification of arbitrary AWS credentials. Only locally issued
    ASIA sessions are accepted here; legacy test credentials retain their
    existing emulator behavior. Streaming and presigned requests are excluded.
    """
    authorization = headers.get('Authorization', '')
    match = re.fullmatch(r'AWS4-HMAC-SHA256 Credential=([^/ ,]+)/([0-9]{8})/([^/ ,]+)/([^/ ,]+)/aws4_request, SignedHeaders=([^, ]+), Signature=([0-9a-f]{64})', authorization)
    if not match:
        if 'ASIA' in authorization or headers.get('X-Amz-Security-Token', '').startswith('lcsts-'):
            raise Error('InvalidClientTokenId', 'Malformed temporary credential authorization', 403)
        return None
    access, day, region, service, signed, signature = match.groups()
    if not access.startswith('ASIA'):
        return None
    record = engine.identity.lookup_credentials(access, headers.get('X-Amz-Security-Token', ''))
    names = signed.split(';')
    if names != sorted(set(names)) or not {'host', 'x-amz-date', 'x-amz-security-token'} <= set(names):
        raise Error('SignatureDoesNotMatch', 'Required session headers must be signed', 403)
    timestamp = headers.get('X-Amz-Date', '')
    try:
        requested = datetime.strptime(timestamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise Error('SignatureDoesNotMatch', 'Invalid signature timestamp', 403) from None
    if timestamp[:8] != day or abs((datetime.now(timezone.utc) - requested).total_seconds()) > 900:
        raise Error('RequestExpired', 'Request signature timestamp is outside the accepted window', 403)
    if region != 'us-east-1' or service not in ('s3', 'sqs', 'ec2', 'iam', 'sts'):
        raise Error('SignatureDoesNotMatch', 'Unsupported credential scope', 403)
    canonical_headers = ''
    for name in names:
        value = headers.get(name)
        if value is None:
            raise Error('SignatureDoesNotMatch', 'Signed header is missing', 403)
        canonical_headers += name + ':' + ' '.join(value.split()) + '\n'
    parsed = urlsplit(url)
    if any(key.lower().startswith('x-amz-') for key, _ in parse_qsl(parsed.query)):
        raise Error('UnsupportedOperation', 'Presigned temporary-credential requests are unsupported', 501)
    path = parsed.path or '/'
    if service != 's3':
        path = quote(path, safe='/~')
    query = '&'.join(quote(k, safe='~') + '=' + quote(v, safe='~') for k, v in sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    payload = hashlib.sha256(body).hexdigest()
    supplied_hash = headers.get('X-Amz-Content-SHA256')
    if supplied_hash and supplied_hash != payload:
        raise Error('SignatureDoesNotMatch', 'Streaming and unsigned payloads are unsupported for local role sessions', 403)
    canonical = '\n'.join((method, path, query, canonical_headers, signed, payload))
    scope = '/'.join((day, region, service, 'aws4_request'))
    message = 'AWS4-HMAC-SHA256\n' + timestamp + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
    key = ('AWS4' + record['secret_access_key']).encode()
    for part in (day, region, service, 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    calculated = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, signature):
        raise Error('SignatureDoesNotMatch', 'Temporary credential signature did not match', 403)
    return {**record, 'signed_service': service}
