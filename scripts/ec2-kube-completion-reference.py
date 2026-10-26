"""Normally licensed LocalStack EC2 Kubernetes drift/restart/metadata audit.

Own namespace/PVC/AMI tag only. A marked cached-image pull-policy intervention
is necessary for guest execution; this is distinct from unmodified behavior.
"""
import json
import sys
import pathlib
import subprocess
import tempfile
import time
import uuid

ROOT=pathlib.Path(__file__).resolve().parents[1]
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
CONTEXT='kind-localcloud';NODE='localcloud-control-plane'
PYTHON='/opt/code/localstack/.venv/bin/python'

class ProbeDone(Exception):
 pass


def main():
 ns='ec2-kube-ref-completion-'+uuid.uuid4().hex[:8]
 token=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip()
 report={'namespace':ns,'reference_image':IMAGE,'context':CONTEXT,'records':[]};tag=None;created=False
 def k(*args,body=None,timeout=35,check=True):
  p=subprocess.run(['kubectl','--context',CONTEXT,*args],input=json.dumps(body) if body is not None else None,capture_output=True,text=True,timeout=timeout)
  if check and p.returncode:raise RuntimeError((p.stderr or p.stdout)[-2000:])
  return p.stdout if check else p
 def create(body):return k('create','-f','-',body=body)
 def api(case,op,**args):
  code="""import boto3,json
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=35))
try:
 r=getattr(c,OP)(**ARGS);meta=r.pop('ResponseMetadata',{});out={'status':meta.get('HTTPStatusCode'),'result':r}
except Exception as e:
 r=getattr(e,'response',{});out={'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})}
print(json.dumps(out,default=str))
""".replace('OP',repr(op)).replace('ARGS',repr(args))
  result=json.loads(k('exec','-n',ns,'deployment/reference','--',PYTHON,'-c',code,timeout=50))
  report['records'].append({'case':case,'operation':op,'parameters':args,**result});return result.get('result',{})
 def objects():
  return json.loads(k('get','deployments,pods,services,configmaps,persistentvolumeclaims','-n',ns,'-o','json'))['items']
 def snapshot(case):
  rows=[]
  for x in objects():
   rows.append({'kind':x['kind'],'name':x['metadata']['name'],'uid':x['metadata']['uid'],'phase':x.get('status',{}).get('phase'),'replicas':x.get('spec',{}).get('replicas'),'readyReplicas':x.get('status',{}).get('readyReplicas'),'containers':[{'name':c['name'],'image':c['image'],'imagePullPolicy':c.get('imagePullPolicy')} for c in x.get('spec',{}).get('containers',[])],'volumes':x.get('spec',{}).get('volumes')})
  report['records'].append({'case':case,'objects':rows});return rows
 try:
  create({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'labels':{'localcloud.audit':ns}}});created=True
  create({'apiVersion':'v1','kind':'Secret','metadata':{'name':'reference-auth','namespace':ns},'stringData':{'token':token}})
  create({'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'reference','namespace':ns}})
  create({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':{'name':'reference','namespace':ns},'rules':[{'apiGroups':[''],'resources':['pods','pods/log','services','configmaps','secrets','persistentvolumeclaims'],'verbs':['get','list','watch','create','delete','patch','update']},{'apiGroups':['apps','batch'],'resources':['deployments','replicasets','jobs'],'verbs':['get','list','watch','create','delete','patch','update']}]})
  create({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':{'name':'reference','namespace':ns},'subjects':[{'kind':'ServiceAccount','name':'reference','namespace':ns}],'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'reference'}})
  create({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'reference-state','namespace':ns,'labels':{'localcloud.audit':ns}},'spec':{'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'1Gi'}}}})
  env=[{'name':'LOCALSTACK_AUTH_TOKEN','valueFrom':{'secretKeyRef':{'name':'reference-auth','key':'token'}}}]+[{'name':a,'value':b} for a,b in {'EC2_VM_MANAGER':'kubernetes','K8S_NAMESPACE':ns,'EC2_DOWNLOAD_DEFAULT_IMAGES':'0','PERSISTENCE':'1','SNAPSHOT_SAVE_STRATEGY':'ON_REQUEST','SNAPSHOT_LOAD_STRATEGY':'ON_STARTUP'}.items()]
  create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'reference','namespace':ns},'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'reference'}},'template':{'metadata':{'labels':{'app':'reference'}},'spec':{'serviceAccountName':'reference','securityContext':{'fsGroup':1000},'containers':[{'name':'reference','image':'localstack/localstack:latest','imagePullPolicy':'Never','env':env,'volumeMounts':[{'name':'state','mountPath':'/var/lib/localstack'}],'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'1Gi'}},'readinessProbe':{'httpGet':{'path':'/_localstack/health','port':4566},'initialDelaySeconds':5,'periodSeconds':3}}],'volumes':[{'name':'state','persistentVolumeClaim':{'claimName':'reference-state'}}]}}}})
  create({'apiVersion':'v1','kind':'Service','metadata':{'name':'localstack-main','namespace':ns,'labels':{'localcloud.audit':ns}},'spec':{'selector':{'app':'reference'},'ports':[{'name':'edge','port':4566,'targetPort':4566}]}})
  report['reference_gateway_service']='localstack-main:4566 selects own reference Deployment'
  create({'apiVersion':'v1','kind':'Service','metadata':{'name':'localstack','namespace':ns,'labels':{'localcloud.audit':ns}},'spec':{'selector':{'app':'reference'},'ports':[{'name':'metadata','port':4510,'targetPort':4510},{'name':'edge','port':4566,'targetPort':4566}]}})
  report['reference_metadata_service']='localstack.'+ns+'.svc.cluster.local:4510 selects own reference Deployment metadata server'
  k('rollout','status','-n',ns,'deployment/reference','--timeout=120s',timeout=135)
  refpods=json.loads(k('get','pods','-n',ns,'-l','app=reference','-o','json'))['items']
  report['reference_node_images']=[{'image':c.get('image'),'imageID':c.get('imageID')} for p in refpods for c in p.get('status',{}).get('containerStatuses',[])]
  report['reference_startup_logs']=k('logs','-n',ns,'deployment/reference','--tail=12').replace(token,'[REDACTED]')
  ami=api('register-image','register_image',Name='rea-completion-'+ns,Architecture='x86_64',RootDeviceName='/dev/sda1',VirtualizationType='hvm')['ImageId']
  guest='localstack-ec2/ubuntu-26.04-resolute-raccoon:ami-61ad6e59d7b0'
  assert subprocess.run(['docker','image','inspect',ami],capture_output=True).returncode!=0
  subprocess.run(['docker','tag',guest,ami],capture_output=True,check=True)
  try:
   with tempfile.TemporaryFile() as f:
    subprocess.run(['docker','image','save','--platform','linux/amd64',ami],stdout=f,stderr=subprocess.PIPE,check=True,timeout=90);f.seek(0)
    subprocess.run(['docker','exec','-i',NODE,'ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=f,capture_output=True,check=True,timeout=120)
   tag='docker.io/library/'+ami+':latest'
  finally:subprocess.run(['docker','image','rm',ami],capture_output=True,check=True)
  run=api('run-instance','run_instances',ImageId=ami,InstanceType='t3.micro',MinCount=1,MaxCount=1,UserData='#!/bin/sh\necho REA-bootstrap >/tmp/rea-bootstrap\necho REA-console\npython3 - <<\'PYIMDS\'\nimport urllib.request,json,time\nr=[]\nfor _ in range(10):\n try:\n  op=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n  with op.open("http://169.254.169.254/latest/meta-data/instance-id",timeout=1) as x:r.append({"status":x.status,"body":x.read(100).decode()})\n  break\n except Exception as e:r.append({"error":str(e)});time.sleep(.2)\nopen("/tmp/rea-bootstrap-metadata.json","w").write(json.dumps(r))\nPYIMDS\n')
  iid=run['Instances'][0]['InstanceId'];dep='localstack-ec2-'+iid
  snapshot('stock-created-workloads')
  report['controlled_intervention']={'reason':'Stock Always policy bypasses node cache and attempts unavailable AMI-name registry pull','patch':'Never only on own EC2 guest Deployment'}
  k('patch','deployment',dep,'-n',ns,'--type=strategic','-p',json.dumps({'spec':{'template':{'spec':{'containers':[{'name':dep,'imagePullPolicy':'Never'}]}}}}))
  k('rollout','status','-n',ns,'deployment/'+dep,'--timeout=90s',timeout=100)
  def guest_exec(code):
   p=k('exec','-n',ns,'deployment/'+dep,'-c',dep,'--','python3','-c',code,check=False)
   return {'exit':p.returncode,'output':p.stdout[-5000:],'stderr':p.stderr[-1000:]}
  report['guest_initial']=guest_exec("import pathlib,json;print(json.dumps({'bootstrap':pathlib.Path('/tmp/rea-bootstrap').read_text(),'bootstrap_metadata':pathlib.Path('/tmp/rea-bootstrap-metadata.json').read_text() if pathlib.Path('/tmp/rea-bootstrap-metadata.json').exists() else 'not-yet-written'}))")
  report['metadata_initial']=guest_exec("""import urllib.request,urllib.error,json
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));u='http://169.254.169.254/latest/';rows=[]
for p in ['meta-data/instance-id','user-data','meta-data/iam/security-credentials/']:
 try:
  with op.open(u+p,timeout=3) as r:rows.append({'path':p,'status':r.status,'body':r.read(1000).decode()})
 except Exception as e:rows.append({'path':p,'error':str(e)})
try:
 t=op.open(urllib.request.Request(u+'api/token',method='PUT',data=b'',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
 with op.open(urllib.request.Request(u+'meta-data/instance-id',headers={'X-aws-ec2-metadata-token':t}),timeout=3) as r:rows.append({'path':'v2-instance-id','status':r.status,'body':r.read(1000).decode()})
except Exception as e:rows.append({'path':'v2-instance-id','error':str(e)})
print(json.dumps(rows))
""")
  if '--metadata-diagnostic' in sys.argv:
   allpods=json.loads(k('get','pods','-n',ns,'-o','json'))['items']
   active=[x for x in allpods if iid in x['metadata']['name'] and x.get('status',{}).get('phase')=='Running']
   report['guest_pod_owner_references']=[x['metadata'].get('ownerReferences',[]) for x in active]
   report['guest_proxy_configuration']=[{'name':c['name'],'command':c.get('command'),'args':c.get('args')} for x in active for c in x['spec']['containers']]
   report['proxy_logs']=k('logs','-n',ns,'deployment/'+dep,'-c','imds-proxy-sidecar','--tail=30',check=False).stderr
   log=k('logs','-n',ns,'deployment/'+dep,'-c','imds-proxy-sidecar','--tail=30',check=False)
   report['proxy_logs']={'exit':log.returncode,'stdout':log.stdout,'stderr':log.stderr}
   report['guest_gateway_health']=guest_exec("import urllib.request,json;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));print(op.open('http://localstack-main:4566/_localstack/health',timeout=3).status)")
   api('diagnostic-require-imdsv2','modify_instance_metadata_options',InstanceId=iid,HttpTokens='required')
   report['metadata_token_enforcement']=guest_exec("""import urllib.request,urllib.error,json
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));u='http://169.254.169.254/latest/';rows=[]
try:
 with op.open(u+'meta-data/instance-id',timeout=3) as r:rows.append({'case':'without-token','status':r.status,'body':r.read(200).decode()})
except urllib.error.HTTPError as e:rows.append({'case':'without-token','status':e.code})
except Exception as e:rows.append({'case':'without-token','error':str(e)})
try:
 with op.open(urllib.request.Request(u+'api/token',method='PUT',data=b'',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3) as r:t=r.read().decode();rows.append({'case':'issue-token','status':r.status})
 with op.open(urllib.request.Request(u+'meta-data/instance-id',headers={'X-aws-ec2-metadata-token':t}),timeout=3) as r:rows.append({'case':'with-token','status':r.status,'body':r.read(200).decode()})
except Exception as e:rows.append({'case':'with-token','error':str(e)})
print(json.dumps(rows))
""")
   raise ProbeDone()
  api('describe-running','describe_instances',InstanceIds=[iid]);snapshot('before-lifecycle')
  api('stop','stop_instances',InstanceIds=[iid]);time.sleep(2);snapshot('after-stop')
  api('start','start_instances',InstanceIds=[iid]);time.sleep(2);snapshot('after-start')
  api('reboot','reboot_instances',InstanceIds=[iid]);time.sleep(3);snapshot('after-reboot')
  api('modify-metadata','modify_instance_metadata_options',InstanceId=iid,HttpTokens='required')
  report['metadata_after_required']=guest_exec("""import urllib.request,json
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));rows=[]
try:
 with op.open('http://169.254.169.254/latest/meta-data/instance-id',timeout=3) as r:rows.append({'status':r.status,'body':r.read(200).decode()})
except Exception as e:rows.append({'error':str(e)})
print(json.dumps(rows))
""")
  # Preserve controller-state files, then replace only the owned reference Pod.
  api('tag-before-restart','create_tags',Resources=[iid],Tags=[{'Key':'BeforeRestart','Value':'preserved'}])
  k('rollout','restart','deployment/reference','-n',ns)
  k('rollout','status','deployment/reference','-n',ns,'--timeout=120s',timeout=135)
  api('describe-after-controller-restart','describe_instances',InstanceIds=[iid]);snapshot('after-controller-restart')
  # Deliberately remove own guest workload and inspect API-driven reconciliation.
  k('delete','deployment',dep,'-n',ns,'--ignore-not-found','--wait=true','--timeout=45s',timeout=55)
  api('describe-after-guest-delete','describe_instances',InstanceIds=[iid]);snapshot('after-guest-delete-describe')
  api('start-after-guest-delete','start_instances',InstanceIds=[iid]);time.sleep(3);snapshot('after-guest-delete-start')
  api('reboot-after-guest-delete','reboot_instances',InstanceIds=[iid]);snapshot('after-guest-delete-reboot')
  api('terminate-after-guest-delete','terminate_instances',InstanceIds=[iid]);snapshot('after-terminate')
 except ProbeDone:pass
 except Exception as error:report['error']=type(error).__name__+': '+str(error).replace(token,'[REDACTED]')
 finally:
  if created:
   try:report['reference_final_logs']=k('logs','-n',ns,'deployment/reference','--tail=40').replace(token,'[REDACTED]')
   except Exception:pass
   try:
    obj=json.loads(k('get','namespace',ns,'-o','json'));assert obj['metadata']['labels']['localcloud.audit']==ns
    k('delete','namespace',ns,'--wait=true','--timeout=90s',timeout=100);report['namespace_removed']=True
   except Exception as error:report['cleanup_error']=str(error)
  if tag and report.get('namespace_removed'):
   p=subprocess.run(['docker','exec',NODE,'ctr','--namespace=k8s.io','images','rm',tag],capture_output=True,text=True,timeout=30)
   report['node_tag_removed']=p.returncode==0
  text=json.dumps(report,indent=2,default=str).replace(token,'[REDACTED]')
  (ROOT/('docs/research/rea/ec2-kube-completion-reference-metadata-diagnostic-report.json' if '--metadata-diagnostic' in sys.argv else 'docs/research/rea/ec2-kube-completion-reference-report.json')).write_text(text+'\n')
 print(json.dumps(report,default=str).replace(token,'[REDACTED]'))
 return 0 if not report.get('error') and report.get('namespace_removed') and report.get('node_tag_removed') else 1

if __name__=='__main__':raise SystemExit(main())
