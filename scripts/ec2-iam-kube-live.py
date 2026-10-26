"""Isolated Kubernetes guest role credentials, dynamic profiles and signed AWS calls.

Requires existing kind-localcloud and cached LocalCloud runtime/demo images.
Own unique namespace/images only; no credentials are printed or persisted in report.
"""
import base64
import importlib.util
import ipaddress
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('kube_fixture', ROOT / 'scripts/ec2-kubernetes-live.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
command, kubectl, wait = fixture.command, fixture.kubectl, fixture.wait
CONTEXT, NODE = fixture.CONTEXT, fixture.NODE

BOOTSTRAP = """#!/bin/sh
set -eu
python3 - <<'BOOT'
import json,os,urllib.request
http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
base=os.environ['AWS_EC2_METADATA_SERVICE_ENDPOINT']+'/latest/'
token=http.open(urllib.request.Request(base+'api/token',method='PUT',data=b'',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
def get(path):return http.open(urllib.request.Request(base+path,headers={'X-aws-ec2-metadata-token':token}),timeout=3).read().decode()
role=get('meta-data/iam/security-credentials/')
credentials=json.loads(get('meta-data/iam/security-credentials/'+role))
assert credentials['Code']=='Success' and credentials['AccessKeyId'].startswith('ASIA')
open('/tmp/bootstrap-role','w').write(role)
print('role credentials available before guest application: '+role)
BOOT
"""

GUEST = """import hashlib,hmac,json,os,urllib.request,urllib.error,urllib.parse,xml.etree.ElementTree as ET
from datetime import datetime,timezone
http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
base=os.environ['AWS_EC2_METADATA_SERVICE_ENDPOINT']+'/latest/'
token=http.open(urllib.request.Request(base+'api/token',method='PUT',data=b'',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
def get(path):return http.open(urllib.request.Request(base+path,headers={'X-aws-ec2-metadata-token':token}),timeout=3).read().decode()
try:role=get('meta-data/iam/security-credentials/')
except urllib.error.HTTPError as error:
 print(json.dumps({'metadata_status':error.code}));raise SystemExit()
c=json.loads(get('meta-data/iam/security-credentials/'+role))
def signed(method,path,body=b'',service='s3',extra=None):
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');day=stamp[:8]
 headers={'host':urllib.parse.urlsplit(ENDPOINT).netloc,'x-amz-date':stamp,'x-amz-security-token':c['Token']}
 if extra:headers.update(extra)
 names=';'.join(sorted(headers));canonical_headers=''.join(k+':'+headers[k]+'\\n' for k in sorted(headers))
 canonical='\\n'.join((method,path,'',canonical_headers,names,hashlib.sha256(body).hexdigest()))
 scope=day+'/us-east-1/'+service+'/aws4_request';text='AWS4-HMAC-SHA256\\n'+stamp+'\\n'+scope+'\\n'+hashlib.sha256(canonical.encode()).hexdigest()
 key=('AWS4'+c['SecretAccessKey']).encode()
 for part in(day,'us-east-1',service,'aws4_request'):key=hmac.new(key,part.encode(),hashlib.sha256).digest()
 headers['Authorization']='AWS4-HMAC-SHA256 Credential='+c['AccessKeyId']+'/'+scope+', SignedHeaders='+names+', Signature='+hmac.new(key,text.encode(),hashlib.sha256).hexdigest()
 request=urllib.request.Request(ENDPOINT+path,data=body if method!='GET' else None,method=method,headers=headers)
 try:
  with http.open(request,timeout=5) as response:return response.status,response.read()
 except urllib.error.HTTPError as error:return error.code,error.read()
read_status,body=signed('GET','/fixture/readme')
write_status,_=signed('PUT','/fixture/forbidden',b'no')
status,identity=signed('POST','/',b'Action=GetCallerIdentity&Version=2011-06-15','sts',{'content-type':'application/x-www-form-urlencoded'})
assert status==200,(status,identity)
print(json.dumps({'role':role,'read_status':read_status,'read_body':body.decode(),'write_status':write_status,'caller_arn':ET.fromstring(identity).findtext('.//{*}Arn')}))
"""


def main():
    from localcloud.engine import Engine
    from localcloud.kube import KubeClient
    from localcloud.__main__ import Handler
    namespace = 'ec2-iam-kube-' + uuid.uuid4().hex[:8]
    guest = 'localcloud-ec2-web:iam-' + uuid.uuid4().hex[:8]
    helper = 'localcloud-ec2-helper:iam-' + uuid.uuid4().hex[:8]
    report = {'passed': False, 'namespace': namespace, 'context': CONTEXT, 'checks': [],
              'credential_redaction': 'Credential values remain inside guest/helper memory and private temporary state; no values in this report',
              'authorization': 'caller-authorized kubectl proxy, not a new minimal RBAC test'}
    server = engine = proxy = thread = None
    made_namespace = False; host_tags = []; node_tags = []
    def check(value): report['checks'].append(value)
    with tempfile.TemporaryDirectory(prefix='localcloud-iam-kube-') as directory:
        root = Path(directory)
        try:
            command('docker', 'tag', fixture.SOURCE_IMAGE, guest); host_tags.append(guest)
            dockerfile = root/'Dockerfile.helper'
            dockerfile.write_text('FROM localcloud:ec2-runtime\nUSER root\nCOPY localcloud /app/localcloud\nUSER 10001:10001\n')
            command('docker','build','--network','none','--pull=false','-f',str(dockerfile),'-t',helper,str(ROOT),timeout=120);host_tags.append(helper)
            for image in host_tags:
                with tempfile.TemporaryFile() as archive:
                    subprocess.run(['docker','image','save','--platform','linux/amd64',image],stdout=archive,stderr=subprocess.PIPE,check=True,timeout=90);archive.seek(0)
                    subprocess.run(['docker','exec','-i',NODE,'ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=archive,capture_output=True,check=True,timeout=120)
                node_tags.append('docker.io/library/'+image)
            gateway=next(entry['Gateway'] for entry in json.loads(command('docker','network','inspect','kind'))[0]['IPAM']['Config'] if ipaddress.ip_address(entry['Gateway']).version==4)
            kubectl('create','-f','-',input=json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,'labels':{'localcloud.audit':namespace}}}));made_namespace=True
            proxy_port=fixture.free_port();proxylog=(root/'proxy.log').open('w')
            proxy=subprocess.Popen(['kubectl','--context',CONTEXT,'proxy','--address=127.0.0.1','--port='+str(proxy_port)],stdout=proxylog,stderr=subprocess.STDOUT)
            proxy_url='http://127.0.0.1:'+str(proxy_port)
            def ready_proxy():
                import urllib.request
                with urllib.request.urlopen(proxy_url+'/version',timeout=1) as response:return response.status==200
            wait(ready_proxy,20)
            engine=Engine(str(root/'state.db'),ec2_images={'ami-web':guest},ec2_kubernetes={'client':KubeClient(proxy_url),'namespace':namespace,'helper_image':helper,'image_pull_policy':'Never'})
            server=ThreadingHTTPServer(('0.0.0.0',0),Handler)
            engine.endpoint='http://'+gateway+':'+str(server.server_port)
            server.engine,server.management_token=engine,secrets.token_urlsafe(32)
            server.executor=None;server.instance_name='iam-kube-live'
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            trust={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com'},'Action':'sts:AssumeRole'}]}
            for name in ('worker','other'):
                engine.identity.call('iam','CreateRole',{'RoleName':name,'AssumeRolePolicyDocument':trust})
                engine.identity.call('iam','PutRolePolicy',{'RoleName':name,'PolicyName':'read','PolicyDocument':{'Statement':[{'Effect':'Allow','Action':['s3:GetObject','sts:GetCallerIdentity'],'Resource':'*'}]}})
                engine.identity.call('iam','CreateInstanceProfile',{'InstanceProfileName':name})
                engine.identity.call('iam','AddRoleToInstanceProfile',{'InstanceProfileName':name,'RoleName':name})
            engine.s3('PUT','fixture','');engine.s3('PUT','fixture','readme',b'role-read-ok')
            engine.policy('test',{'Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]});engine.iam_mode='enforce'
            def ec2(op,**args):return engine.ec2.call(op,args,'test')
            item=ec2('RunInstances',ImageId='ami-web',IamInstanceProfile={'Name':'worker'},MetadataOptions={'HttpTokens':'required'},UserData=base64.b64encode(BOOTSTRAP.encode()).decode(),PortMappings=[{'HostPort':0,'ContainerPort':8080}])['Instances'][0]
            instance=item['InstanceId']
            def describe():return ec2('DescribeInstances',InstanceIds=[instance])['Reservations'][0]['Instances'][0]
            item=wait(lambda:(x if (x:=describe())['State']['Name']=='running' else None),90)
            pod=item['PodName'];poduid=item['PodUid']
            def guest_exec(code):return kubectl('exec','-n',namespace,pod,'-c','guest','--','python3','-c',code,timeout=30).strip()
            assert guest_exec("print(open('/tmp/bootstrap-role').read())")=='worker'
            check('kubernetes-user-data-reads-instance-role-credentials-before-app-start')
            def exercise():return json.loads(guest_exec('ENDPOINT='+repr(engine.endpoint)+'\n'+GUEST))
            first=exercise();assert first['role']=='worker' and first['read_status']==200 and first['read_body']=='role-read-ok' and first['write_status']==403,first
            assert first['caller_arn'].endswith('/worker/'+instance),first
            report['initial']=first
            check('guest-signed-sts-caller-identity-and-s3-read')
            check('role-policy-denies-signed-write')
            association=ec2('DescribeIamInstanceProfileAssociations',Filters=[{'Name':'instance-id','Values':[instance]}])['IamInstanceProfileAssociations'][0]['AssociationId']
            ec2('ReplaceIamInstanceProfileAssociation',AssociationId=association,IamInstanceProfile={'Name':'other'})
            second=exercise();assert second['role']=='other' and second['caller_arn'].endswith('/other/'+instance),second
            assert describe()['PodUid']==poduid
            report['replaced']=second
            check('profile-replacement-updates-live-imds-without-restarting-pod')
            ec2('DisassociateIamInstanceProfile',AssociationId=association)
            assert exercise()=={'metadata_status':404}
            check('profile-detach-removes-live-imds-credentials')
            ec2('AssociateIamInstanceProfile',InstanceId=instance,IamInstanceProfile={'Name':'worker'})
            assert exercise()['role']=='worker'
            check('profile-reattach-restores-live-credentials')
            ec2('StopInstances',InstanceIds=[instance]);wait(lambda:describe()['State']['Name']=='stopped',60)
            ec2('StartInstances',InstanceIds=[instance]);item=wait(lambda:(x if (x:=describe())['State']['Name']=='running' else None),90)
            pod=item['PodName'];assert item['PodUid']!=poduid
            assert guest_exec("print(open('/tmp/bootstrap-role').read())")=='worker'
            assert exercise()['role']=='worker'
            check('new-pod-bootstrap-and-role-credentials-survive-stop-start')
            ec2('TerminateInstances',InstanceIds=[instance]);wait(lambda:describe()['State']['Name']=='terminated',60)
            check('termination-cleans-workloads-and-revokes-instance-access')
            report['passed']=True
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)[-2000:]
            if made_namespace:
                try:
                    pods=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items']
                    report['failure_pods']=[{'name':p['metadata']['name'],'phase':p.get('status',{}).get('phase'),'init':p.get('status',{}).get('initContainerStatuses',[]),'containers':p.get('status',{}).get('containerStatuses',[])} for p in pods]
                    report['user_data_logs']=[kubectl('logs','-n',namespace,p['metadata']['name'],'-c','user-data','--tail=10',check=False).stdout for p in pods]
                except Exception as diagnostic:report['diagnostic_error']=str(diagnostic)[-1000:]

        finally:
            if server:server.shutdown();server.server_close()
            if thread:thread.join(timeout=5)
            if engine:engine.close()
            if proxy:proxy.terminate();proxy.wait(timeout=10)
            if made_namespace:
                kubectl('delete','namespace',namespace,'--wait=true','--timeout=90s',timeout=100);report['namespace_removed']=True
            report['node_tags_removed']=[]
            for tag in node_tags:
                p=command('docker','exec',NODE,'ctr','--namespace=k8s.io','images','rm',tag,check=False);report['node_tags_removed'].append(p.returncode==0)
            report['host_tags_removed']=[]
            for tag in host_tags:
                p=command('docker','image','rm',tag,check=False);report['host_tags_removed'].append(p.returncode==0)
    target=ROOT/'docs/research/rea/ec2-iam-kube-live-report.json';target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['passed'] and report.get('namespace_removed') and all(report['node_tags_removed']) and all(report['host_tags_removed']) else 1


if __name__=='__main__':raise SystemExit(main())
