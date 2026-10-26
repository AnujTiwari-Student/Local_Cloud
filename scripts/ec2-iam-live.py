"""Actual Docker EC2 guest SDK role discovery and signed local service checks.

Uses cached images only, unique image tags, private temporary backend state and
owned containers. Reports shapes/results without any credential values.
"""
import base64
from contextlib import contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localcloud.engine import Engine, Error
from localcloud.__main__ import Handler
from localcloud.client import Client

SDK_PYTHON = '/opt/code/localstack/.venv/bin/python'
SDK_BASE = runpy.run_path(str(ROOT / 'scripts/ec2-kube-bootstrap-reference.py'))['IMAGE']
TRUST_EC2 = {'Statement': {'Effect': 'Allow', 'Principal': {'Service': 'ec2.amazonaws.com'}, 'Action': 'sts:AssumeRole'}}
TRUST_ROOT = {'Statement': {'Effect': 'Allow', 'Principal': {'AWS': 'arn:aws:iam::000000000000:root'}, 'Action': 'sts:AssumeRole'}}


def command(*args, timeout=45, input=None, check=True):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1500:])
    return result.stdout if check else result


def main():
    suffix = uuid.uuid4().hex[:10]
    guest_image, helper_image = 'localcloud-iam-sdk:rea-' + suffix, 'localcloud-iam-helper:rea-' + suffix
    report = {'passed': False, 'checks': [], 'sdk_base_image': SDK_BASE, 'guest_image': guest_image, 'helper_image': helper_image}
    built = []
    server = thread = engine = None
    instances = []
    with tempfile.TemporaryDirectory(prefix='localcloud-iam-live-') as temporary:
        temporary = Path(temporary)
        try:
            for image in (SDK_BASE, 'localcloud:ec2-runtime'):
                command('docker', 'image', 'inspect', image)
            for image, content in ((guest_image, 'FROM ' + SDK_BASE + '\nENV AWS_DEFAULT_REGION=us-east-1 AWS_CONFIG_FILE=/tmp/no-config AWS_SHARED_CREDENTIALS_FILE=/tmp/no-credentials\nENTRYPOINT ["' + SDK_PYTHON + '", "-c", "import time; time.sleep(86400)"]\n'), (helper_image, 'FROM localcloud:ec2-runtime\nUSER root\nCOPY localcloud /app/localcloud\nUSER 10001:10001\n')):
                dockerfile = temporary / ('Dockerfile.' + str(len(built)))
                dockerfile.write_text(content)
                command('docker', 'build', '--network', 'none', '--pull=false', '-f', str(dockerfile), '-t', image, str(ROOT), timeout=150)
                built.append(image)
            report['sdk_version'] = command('docker', 'run', '--rm', '--network', 'none', '--user', '65534:65534', '--entrypoint', SDK_PYTHON, guest_image, '-c', 'import boto3;print(boto3.__version__)').strip()
            gateway = json.loads(command('docker', 'network', 'inspect', 'bridge'))[0]['IPAM']['Config'][0]['Gateway']
            engine = Engine(str(temporary / 'state.db'), ec2_images={'ami-sdk': guest_image})
            engine.ec2.runtime.helper_image, engine.ec2.runtime.imds = helper_image, True
            server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
            server.engine, server.management_token, server.executor = engine, secrets.token_urlsafe(32), None
            engine.endpoint = f'http://{gateway}:{server.server_port}'
            local_endpoint = f'http://127.0.0.1:{server.server_port}'
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            management = Client(local_endpoint, token=server.management_token, timeout=90)
            def call(service, operation, **args):
                return management.request('POST', '/_local/' + service, {'Action': operation, **args})
            def check(label): report['checks'].append(label)
            def role(role_name, trust):
                return call('iam', 'CreateRole', RoleName=role_name, AssumeRolePolicyDocument=trust)['Role']
            def profile(role_name, profile_name):
                call('iam', 'CreateInstanceProfile', InstanceProfileName=profile_name)
                call('iam', 'AddRoleToInstanceProfile', RoleName=role_name, InstanceProfileName=profile_name)
            first = role('guest-reader', TRUST_EC2); profile('guest-reader', 'guest-reader-profile')
            second = role('guest-replacement', TRUST_EC2); profile('guest-replacement', 'guest-replacement-profile')
            assume = role('cli-reader', TRUST_ROOT)
            permissions = {'Statement': [{'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::iam-fixture/*'}, {'Effect': 'Deny', 'Action': 's3:PutObject', 'Resource': '*'}]}
            for name in ('guest-reader', 'guest-replacement', 'cli-reader'):
                call('iam', 'PutRolePolicy', RoleName=name, PolicyName='read-only', PolicyDocument=permissions)
            engine.s3('PUT', 'iam-fixture', '')
            engine.s3('PUT', 'iam-fixture', 'marker', b'role-reader-ok')
            engine.policy('test', {'Statement': {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}})
            engine.iam_mode = 'enforce'
            userdata = '#!/bin/sh\n' + SDK_PYTHON + " - <<'BOOTSTRAP'\nimport boto3,json\nc=boto3.Session().get_credentials()\nassert c and c.method=='iam-role', 'Metadata credential chain unavailable'\nopen('/tmp/credential-bootstrap.json','w').write(json.dumps({'method':c.method,'has_token':bool(c.token)}))\nBOOTSTRAP\n"
            launched = call('ec2', 'RunInstances', ImageId='ami-sdk', Name='iam-live', IamInstanceProfile={'Name': 'guest-reader-profile'}, MetadataOptions={'HttpTokens': 'required'}, UserData=base64.b64encode(userdata.encode()).decode())['Instances'][0]
            instance = launched['InstanceId']; instances.append(instance)
            name = 'localcloud-' + instance
            def guest(code, *arguments, sensitive=None):
                result = command('docker', 'exec', '-i', name, SDK_PYTHON, '-c', code, *arguments, timeout=45, input=json.dumps(sensitive) if sensitive is not None else None)
                return json.loads(result)
            result = guest("import json;print(open('/tmp/credential-bootstrap.json').read())")
            assert result == {'method': 'iam-role', 'has_token': True}, result
            check('actual-docker-user-data-discovers-role-credentials-before-state-commit')
            sdk_code = r'''
import boto3,json,sys
from botocore.config import Config
from botocore.exceptions import ClientError
endpoint=sys.argv[1]
session=boto3.Session()
credentials=session.get_credentials()
assert credentials.method=='iam-role'
config=Config(retries={'max_attempts':0},connect_timeout=3,read_timeout=5,s3={'addressing_style':'path'},request_checksum_calculation='when_required',response_checksum_validation='when_required')
sts=session.client('sts',endpoint_url=endpoint,config=config)
identity=sts.get_caller_identity()
s3=session.client('s3',endpoint_url=endpoint,config=config)
assert s3.get_object(Bucket='iam-fixture',Key='marker')['Body'].read()==b'role-reader-ok'
try:s3.put_object(Bucket='iam-fixture',Key='blocked',Body=b'no')
except ClientError as error:assert error.response['Error']['Code']=='AccessDenied';denied=True
else:raise AssertionError('Write was allowed')
print(json.dumps({'method':credentials.method,'identity_arn':identity['Arn'],'read':True,'write_denied':denied}))
'''
            result = guest(sdk_code, engine.endpoint)
            assert result['identity_arn'].endswith('assumed-role/guest-reader/' + instance), result
            report['guest_result'] = result
            check('actual-guest-sdk-automatic-imdsv2-role-discovery')
            check('signed-sts-getcalleridentity-reflects-instance-role-session')
            check('signed-s3-read-allowed-and-write-explicitly-denied')
            # Exercise generated IAM/STS Query responses and signed assumed role
            # sessions using the SDK installed in the actual running EC2 guest.
            admin_code = r'''
import boto3,json,sys
from botocore.config import Config
endpoint,management_token,rolearn=json.load(sys.stdin)
cfg=Config(retries={'max_attempts':0},connect_timeout=3,read_timeout=5,s3={'addressing_style':'path'},request_checksum_calculation='when_required',response_checksum_validation='when_required')
admin=boto3.Session(aws_access_key_id='test',aws_secret_access_key='test',aws_session_token=management_token,region_name='us-east-1')
iam=admin.client('iam',endpoint_url=endpoint,config=cfg)
assert iam.get_instance_profile(InstanceProfileName='guest-reader-profile')['InstanceProfile']['Roles'][0]['RoleName']=='guest-reader'
sts=admin.client('sts',endpoint_url=endpoint,config=cfg)
result=sts.assume_role(RoleArn=rolearn,RoleSessionName='sdk-session')
c=result['Credentials'];assumed=boto3.Session(aws_access_key_id=c['AccessKeyId'],aws_secret_access_key=c['SecretAccessKey'],aws_session_token=c['SessionToken'],region_name='us-east-1')
identity=assumed.client('sts',endpoint_url=endpoint,config=cfg).get_caller_identity()
assert identity['Arn'].endswith('/cli-reader/sdk-session')
print(json.dumps({'profile_sdk':True,'assume_role_identity':identity['Arn']}))
'''
            result = guest(admin_code, sensitive=[engine.endpoint, server.management_token, assume['Arn']])
            report['assume_role_result'] = result
            check('iam-query-sdk-profile-shape-and-sts-assumerole-session')
            # Every negative check obtains secrets in this host process only;
            # nothing secret is added to report or printed to capture output.
            credential_bundle = engine.identity.instance_credentials(instance)
            creds = credential_bundle['Credentials']
            def status(url, token):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=5) as response:
                        response.read(); return response.status
                except urllib.error.HTTPError as error: return error.code
            capability = engine.identity.instance_broker(instance)
            assert status(local_endpoint + '/_local/instance-credentials/' + instance, capability) == 200
            assert status(local_endpoint + '/_local/instance-credentials/' + instance, server.management_token) == 401
            assert status(local_endpoint + '/_local/instance-credentials/i-other', capability) == 401
            check('broker-capability-bound-to-instance-and-not-management-token')
            negative_code = r'''
import boto3,json,sys
from botocore.config import Config
from botocore.exceptions import ClientError
endpoint,access,secret,token=json.load(sys.stdin)
config=Config(retries={'max_attempts':0},read_timeout=4,connect_timeout=3)
result=[]
for suffix,changed in [('signature',{'aws_secret_access_key':'wrong-secret'}),('token',{'aws_session_token':'wrong-token'})]:
 kw={'aws_access_key_id':access,'aws_secret_access_key':secret,'aws_session_token':token,'region_name':'us-east-1'};kw.update(changed)
 try:boto3.client('sts',endpoint_url=endpoint,config=config,**kw).get_caller_identity()
 except ClientError as error:result.append({'case':suffix,'code':error.response['Error']['Code'],'status':error.response['ResponseMetadata']['HTTPStatusCode']})
 else:raise AssertionError('Invalid session accepted')
print(json.dumps(result))
'''
            result = guest(negative_code, sensitive=[engine.endpoint, creds['AccessKeyId'], creds['SecretAccessKey'], creds['Token']])
            assert result == [{'case': 'signature', 'code': 'SignatureDoesNotMatch', 'status': 403}, {'case': 'token', 'code': 'InvalidClientTokenId', 'status': 403}], result
            report['negative_signatures'] = result
            check('invalid-signature-and-session-token-rejected-over-http')
            with engine.lock, engine.db:
                engine.db.execute('UPDATE identity_sessions SET expiration=? WHERE accesskey=?', (time.time() - 1, creds['AccessKeyId']))
            expired_code = negative_code[:negative_code.index('result=[]')] + "\ntry:boto3.client('sts',endpoint_url=endpoint,config=config,aws_access_key_id=access,aws_secret_access_key=secret,aws_session_token=token,region_name='us-east-1').get_caller_identity()\nexcept ClientError as error:print(json.dumps({'code':error.response['Error']['Code'],'status':error.response['ResponseMetadata']['HTTPStatusCode']}))\nelse:raise AssertionError('Expired session accepted')\n"
            result = guest(expired_code, sensitive=[engine.endpoint, creds['AccessKeyId'], creds['SecretAccessKey'], creds['Token']])
            assert result == {'code': 'ExpiredToken', 'status': 403}, result
            check('expired-session-rejected-over-http')
            renewed = engine.identity.instance_credentials(instance)['Credentials']
            assert renewed['AccessKeyId'] != creds['AccessKeyId']
            check('credential-rotation-issues-new-session')
            association = call('ec2', 'DescribeIamInstanceProfileAssociations', Filters=[{'Name': 'instance-id', 'Values': [instance]}])['IamInstanceProfileAssociations'][0]
            call('ec2', 'ReplaceIamInstanceProfileAssociation', AssociationId=association['AssociationId'], IamInstanceProfile={'Name': 'guest-replacement-profile'})
            replaced = guest(sdk_code, engine.endpoint)
            assert replaced['identity_arn'].endswith('assumed-role/guest-replacement/' + instance), replaced
            check('live-profile-replacement-refreshes-sdk-metadata-role')
            try: engine.identity.lookup_credentials(renewed['AccessKeyId'], renewed['Token'])
            except Error as error: assert error.code == 'InvalidClientTokenId'
            else: raise AssertionError('Replaced credentials remain valid')
            call('ec2', 'DisassociateIamInstanceProfile', AssociationId=association['AssociationId'])
            assert status(local_endpoint + '/_local/instance-credentials/' + instance, capability) == 404
            check('disassociation-revokes-credentials-and-broker-role-delivery')
            call('ec2', 'TerminateInstances', InstanceIds=[instance]); instances.remove(instance)
            assert status(local_endpoint + '/_local/instance-credentials/' + instance, capability) == 401
            check('termination-revokes-instance-broker-capability')
            report['passed'] = True
        except Exception as error:
            report['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            if engine:
                for identifier in instances:
                    try: engine.ec2.call('TerminateInstances', {'InstanceIds': [identifier]}, 'test')
                    except Exception: pass
                runtime_owner = engine.ec2.runtime.owner
                # Delete only containers carrying this temporary engine's
                # private runtime owner or validated instance owner records.
                candidates = command('docker', 'ps', '-aq', '--filter', 'label=localcloud.runtime=' + runtime_owner).split()
                for candidate in candidates: command('docker', 'rm', '-f', candidate, check=False)
                report['owned_runtime_containers_remaining'] = command('docker', 'ps', '-aq', '--filter', 'label=localcloud.runtime=' + runtime_owner).split()
                remaining = [identifier for identifier in instances if command('docker', 'inspect', 'localcloud-' + identifier, check=False).returncode == 0]
                report['guest_containers_remaining'] = remaining
            if server: server.shutdown(); server.server_close()
            if thread: thread.join(timeout=5)
            if engine: engine.close()
            image_cleanup = [command('docker', 'image', 'rm', image, check=False).returncode == 0 for image in reversed(built)]
            report['image_tags_removed'] = all(image_cleanup)
    target = ROOT / 'docs/research/rea/ec2-iam-live-report.json'
    target.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] and not report.get('owned_runtime_containers_remaining') and not report.get('guest_containers_remaining') and report.get('image_tags_removed') else 1


if __name__ == '__main__': raise SystemExit(main())
