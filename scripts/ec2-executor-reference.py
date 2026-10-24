import json,subprocess,pathlib,uuid,time,sys
bootstrap="--bootstrap-cached" in sys.argv
root=pathlib.Path(__file__).resolve().parents[1]
node_tag=None
image='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
ns='ec2-executor-rea-'+uuid.uuid4().hex[:8]
token=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip()
report={'namespace':ns,'context':'kind-localcloud','reference_image':image}
def k(*args,body=None,timeout=30):
 p=subprocess.run(['kubectl','--context','kind-localcloud',*args],input=json.dumps(body) if body else None,capture_output=True,text=True,timeout=timeout)
 if p.returncode: raise RuntimeError((p.stderr or p.stdout)[-1500:])
 return p.stdout
def create(x): return k('create','-f','-',body=x)
try:
 report['image_already_loaded']=True
 report['reference_host_image_id']=subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],text=True).strip()
 create({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'labels':{'localcloud.audit':ns}}})
 create({'apiVersion':'v1','kind':'Secret','metadata':{'name':'reference-auth','namespace':ns},'stringData':{'token':token}})
 create({'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'reference','namespace':ns}})
 create({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':{'name':'reference','namespace':ns},'rules':[{'apiGroups':[''],'resources':['pods','pods/log','services','configmaps','secrets','persistentvolumeclaims'],'verbs':['get','list','watch','create','delete','patch','update']},{'apiGroups':['apps','batch'],'resources':['deployments','jobs'],'verbs':['get','list','watch','create','delete','patch','update']}]})
 create({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':{'name':'reference','namespace':ns},'subjects':[{'kind':'ServiceAccount','name':'reference','namespace':ns}],'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'reference'}})
 env=[{'name':'LOCALSTACK_AUTH_TOKEN','valueFrom':{'secretKeyRef':{'name':'reference-auth','key':'token'}}}]+[{'name':key,'value':value} for key,value in {'EC2_VM_MANAGER':'kubernetes','K8S_NAMESPACE':ns,'EC2_DOWNLOAD_DEFAULT_IMAGES':'0'}.items()]
 create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'reference','namespace':ns},'spec':{'replicas':1,'selector':{'matchLabels':{'app':'reference'}},'template':{'metadata':{'labels':{'app':'reference'}},'spec':{'serviceAccountName':'reference','containers':[{'name':'reference','image':'localstack/localstack:latest','imagePullPolicy':'Never','env':env,'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'1Gi'}},'readinessProbe':{'httpGet':{'path':'/_localstack/health','port':4566},'initialDelaySeconds':5,'periodSeconds':3}}]}}}})
 k('rollout','status','-n',ns,'deployment/reference','--timeout=120s',timeout=135)
 probe="""import boto3,json
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=120))
r=c.register_image(Name='rea-executor-guest',Architecture='x86_64',RootDeviceName='/dev/sda1',VirtualizationType='hvm');print(json.dumps({'image_id':r['ImageId']}))
"""
 registered=json.loads(k('exec','-n',ns,'deployment/reference','--','/opt/code/localstack/.venv/bin/python','-c',probe,timeout=45))
 ami=registered['image_id'];report['registered_image']=ami
 guest='localstack-ec2/ubuntu-26.04-resolute-raccoon:ami-61ad6e59d7b0'
 # RegisterImage supplies an API-generated unique name: no existing node/host tag is replaced.
 assert subprocess.run(['docker','image','inspect',ami],capture_output=True).returncode!=0
 subprocess.run(['docker','tag',guest,ami],check=True,capture_output=True)
 import tempfile
 try:
  with tempfile.TemporaryFile() as f:
   subprocess.run(['docker','image','save','--platform','linux/amd64',ami],stdout=f,stderr=subprocess.PIPE,check=True,timeout=90);f.seek(0)
   subprocess.run(['docker','exec','-i','localcloud-control-plane','ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=f,capture_output=True,check=True,timeout=120)
   node_tag='docker.io/library/'+ami+':latest'
 finally:subprocess.run(['docker','image','rm',ami],capture_output=True,check=True)
 probe="""import boto3,json
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=120))
results=[]
try:
 r=c.run_instances(ImageId='AMI_ID',InstanceType='t3.micro',MinCount=1,MaxCount=1,UserData='#!/bin/sh\\necho executor-user-data-ok >/tmp/rea-marker\\n');r.pop('ResponseMetadata',None);results.append({'operation':'run_instances','result':r})
except Exception as e:results.append({'operation':'run_instances','error':getattr(e,'response',None) or {'type':type(e).__name__,'message':str(e)}})
print(json.dumps(results,default=str))
""".replace('AMI_ID',ami)
 report['probe']=json.loads(k('exec','-n',ns,'deployment/reference','--','/opt/code/localstack/.venv/bin/python','-c',probe,timeout=120))
 if bootstrap:
  run=report['probe'][0].get('result',{})
  iid=run['Instances'][0]['InstanceId'];deploy='localstack-ec2-'+iid
  report['controlled_intervention']={'reason':'Reference generates an AMI-name image with imagePullPolicy Always; loaded node cache cannot satisfy registry lookup','patch':'guest imagePullPolicy=Never on owned Deployment only'}
  k('patch','deployment',deploy,'-n',ns,'--type=strategic','-p',json.dumps({'spec':{'template':{'spec':{'containers':[{'name':deploy,'imagePullPolicy':'Never'}]}}}}))
  k('rollout','status','-n',ns,'deployment/'+deploy,'--timeout=90s',timeout=100)
  def guest(*args):return k('exec','-n',ns,'deployment/'+deploy,'-c',deploy,'--',*args)
  report['guest_before_stop']=guest('sh','-c','cat /tmp/rea-marker; printf "root-persist-ok" >/root/rea-storage-marker; cat /proc/mounts')
  def api(op,args):
   source="import boto3,json;from botocore.config import Config;c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=60));r=getattr(c,"+repr(op)+")(**"+repr(args)+");r.pop('ResponseMetadata',None);print(json.dumps(r,default=str))"
   try:return json.loads(k('exec','-n',ns,'deployment/reference','--','/opt/code/localstack/.venv/bin/python','-c',source,timeout=75))
   except Exception as e:return {'error':str(e)[-1200:]}
  report['described_running']=api('describe_instances',{'InstanceIds':[iid]})
  volume=api('create_volume',{'AvailabilityZone':'us-east-1a','Size':1});report['create_volume']=volume
  vid=volume.get('VolumeId')
  if vid:
   report['attach_volume']=api('attach_volume',{'VolumeId':vid,'InstanceId':iid,'Device':'/dev/sdf'})
   report['guest_after_attach']=guest('sh','-c','cat /proc/mounts; ls -l /dev/sdf /mnt 2>&1 || true')
   report['describe_volume']=api('describe_volumes',{'VolumeIds':[vid]})
   snap=api('create_snapshot',{'VolumeId':vid});report['create_snapshot']=snap
   report['detach_volume']=api('detach_volume',{'VolumeId':vid,'InstanceId':iid})
   if snap.get('SnapshotId'):report['delete_snapshot']=api('delete_snapshot',{'SnapshotId':snap['SnapshotId']})
   report['delete_volume']=api('delete_volume',{'VolumeId':vid})
  report['stop_instances']=api('stop_instances',{'InstanceIds':[iid]})
  report['deployment_after_stop']=json.loads(k('get','deployment',deploy,'-n',ns,'-o','json'))['spec']['replicas']
  report['start_instances']=api('start_instances',{'InstanceIds':[iid]})
  try:
   k('rollout','status','-n',ns,'deployment/'+deploy,'--timeout=60s',timeout=70)
   report['guest_after_start']=guest('sh','-c','cat /tmp/rea-marker; cat /root/rea-storage-marker 2>&1 || true')
  except Exception as e:report['guest_after_start_error']=str(e)
  report['terminate_instances']=api('terminate_instances',{'InstanceIds':[iid]})
  report['remaining_deployments']=[p['metadata']['name'] for p in json.loads(k('get','deployments','-n',ns,'-o','json'))['items']]
 report['pods']=json.loads(k('get','pods','-n',ns,'-o','json'))['items']
 # Retain only status/labels, never environment or Secret contents.
 report['pods']=[{'name':p['metadata']['name'],'labels':p['metadata'].get('labels',{}),'phase':p.get('status',{}).get('phase'),'spec':{'containers':[{key:c.get(key) for key in ('name','image','imagePullPolicy','command','args','volumeMounts','securityContext')} for c in p['spec']['containers']],'volumes':p['spec'].get('volumes'),'securityContext':p['spec'].get('securityContext')},'status':p.get('status',{})} for p in report['pods']]
except Exception as e: report['error']=str(e).replace(token,'[REDACTED]')
finally:
 try:
  report['logs']=k('logs','-n',ns,'deployment/reference','--tail=35').replace(token,'[REDACTED]')
 except Exception: pass
 try: assert json.loads(k('get','namespace',ns,'-o','json'))['metadata']['labels']['localcloud.audit']==ns; k('delete','namespace',ns,'--ignore-not-found','--wait=true','--timeout=90s',timeout=100);report['namespace_cleanup_requested']=True
 except Exception: report['namespace_cleanup_requested']=False
 if node_tag and report.get('namespace_cleanup_requested'):
  p=subprocess.run(['docker','exec','localcloud-control-plane','ctr','--namespace=k8s.io','images','rm',node_tag],capture_output=True,text=True,timeout=20)
  report['node_tag_cleanup']={'tag':node_tag,'exit':p.returncode}
 (root/('docs/research/rea/ec2-executor-controlled.json' if bootstrap else 'docs/research/rea/ec2-executor-reference.json')).write_text(json.dumps(report,indent=2).replace(token,'[REDACTED]'))
print(json.dumps(report).replace(token,'[REDACTED]'))
