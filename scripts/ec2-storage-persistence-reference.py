"""Owned, normally licensed LocalStack EC2 persistence experiment.

No real AWS calls or host block devices are modified. API encryption is metadata
unless a separate observation proves more. Filesystem persistence uses one owned
Docker volume, removed only after resources are cleaned up.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
PYTHON='/opt/code/localstack/.venv/bin/python'
PREFIX=r'''
import boto3,json,docker,time,os
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=45))
rows=[]
def call(label,op,**kw):
 try:
  r=getattr(c,op)(**kw);meta=r.pop('ResponseMetadata',{});rows.append({'case':label,'operation':op,'parameters':kw,'status':meta.get('HTTPStatusCode'),'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'operation':op,'parameters':kw,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
'''

def command(*args,timeout=90):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-2500:])
 return p.stdout

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=['mock','docker'],default='docker');args=parser.parse_args()
 name='localcloud-rea-storage-persist-'+uuid.uuid4().hex[:10];volume=name+'-data'
 token=(Path.home()/'.config/localstack/rea-auth-token').read_text().strip()
 env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=token)
 report={'reference_image':IMAGE,'backend':args.backend,'reference_name':name,'state_volume':volume,'steps':[],'limits':['A volume encryption flag is not evidence of at-rest encryption.','No host block writes, actual size quota test, or performance benchmark.','One normal graceful controller restart, not crash/power-loss recovery.']}
 created=False;volume_created=False;state={}
 def ready():
  port=json.loads(command('docker','inspect',name))[0]['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
  for _ in range(180):
   try:
    with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health',timeout=1) as r:
     if r.status==200:return
   except OSError:time.sleep(.5)
  raise RuntimeError('Reference readiness deadline')
 def run(code):
  p=subprocess.run(['docker','exec','-i',name,PYTHON,'-'],input=PREFIX+'\n'+code,capture_output=True,text=True,timeout=150)
  if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-2500:])
  return json.loads(p.stdout)
 try:
  command('docker','volume','create','--label','localcloud.audit='+name,volume);volume_created=True
  cmd=['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-v',volume+':/var/lib/localstack','-v','/var/run/docker.sock:/var/run/docker.sock','-e','LOCALSTACK_AUTH_TOKEN','-e','PERSISTENCE=1','-e','EC2_VM_MANAGER='+args.backend,IMAGE]
  subprocess.run(cmd,env=env,check=True,capture_output=True,timeout=60);created=True;ready()
  initial=run("""
v=call('create-volume','create_volume',AvailabilityZone='us-east-1a',Size=1)
e=call('create-encrypted','create_volume',AvailabilityZone='us-east-1a',Size=1,Encrypted=True)
s=call('create-snapshot','create_snapshot',VolumeId=v['VolumeId'])
print(json.dumps({'volume':v.get('VolumeId'),'encrypted_volume':e.get('VolumeId'),'snapshot':s.get('SnapshotId'),'rows':rows},default=str))
""")
  state.update({k:v for k,v in initial.items() if k!='rows'});report['steps'].append({'phase':'initial','rows':initial['rows']})
  if args.backend=='docker':
   instance=run("""
d=docker.from_env();before={x.id for x in d.containers.list(all=True)}
r=call('run-instance','run_instances',ImageId='ami-61ad6e59d7b0',InstanceType='t3.micro',MinCount=1,MaxCount=1)
i=r['Instances'][0]['InstanceId'];guest=None
for _ in range(60):
 gs=[x for x in d.containers.list(all=True) if x.id not in before and i in x.name]
 if gs:guest=gs[0];break
 time.sleep(.5)
if guest is None:raise RuntimeError('Owned guest identification failed')
x=guest.exec_run(['sh','-c','echo survives-controller-restart >/tmp/rea-storage-persist-marker; cat /tmp/rea-storage-persist-marker'],user='root')
print(json.dumps({'instance':i,'guest_id':guest.id,'marker_exit':x.exit_code,'marker':x.output.decode(),'mounts':guest.attrs['Mounts'],'rows':rows},default=str))
""")
   state.update({k:v for k,v in instance.items() if k in ('instance','guest_id')});report['steps'].append({'phase':'guest-before-controller-restart',**instance})
  # Stop gracefully so the persistence checkpoint can be written.
  command('docker','stop','--time','30',name,timeout=45);command('docker','start',name);ready()
  observed=run("state="+repr(state)+"\n"+"""
call('describe-volume-after-restart','describe_volumes',VolumeIds=[state['volume'],state['encrypted_volume']])
call('describe-snapshot-after-restart','describe_snapshots',SnapshotIds=[state['snapshot']])
v=call('restore-volume-after-restart','create_volume',AvailabilityZone='us-east-1a',SnapshotId=state['snapshot'])
call('grow-restored-volume','modify_volume',VolumeId=v['VolumeId'],Size=2)
call('describe-grown-volume','describe_volumes',VolumeIds=[v['VolumeId']])
out={'rows':rows,'restored_volume':v.get('VolumeId')}
if state.get('instance'):
 call('attach-restored-volume-after-restart','attach_volume',VolumeId=v['VolumeId'],InstanceId=state['instance'],Device='/dev/sdf')
 call('describe-instance-after-restart','describe_instances',InstanceIds=[state['instance']])
 d=docker.from_env()
 try:
  matches=[g for g in d.containers.list(all=True) if state['instance'] in g.name]
  out['matching_guest_containers']=[{'id':g.id,'name':g.name} for g in matches]
  if len(matches)!=1:raise RuntimeError('Expected exactly one matching guest after restart')
  guest=matches[0];guest.reload();out['guest_id_after_restart']=guest.id;out['same_guest_container']=guest.id==state['guest_id'];out['guest_state']=guest.attrs['State']['Status'];out['mounts']=guest.attrs['Mounts']
  x=guest.exec_run(['sh','-c','cat /tmp/rea-storage-persist-marker; test ! -b /dev/sdf; test ! -b /dev/xvdf'],user='root');out['marker_exit']=x.exit_code;out['marker']=x.output.decode()
 except Exception as e:out['guest_error']={'type':type(e).__name__,'message':str(e)}
print(json.dumps(out,default=str))
""")
  if observed.get('restored_volume'):state['restored_volume']=observed['restored_volume']
  report['steps'].append({'phase':'after-controller-restart',**observed})
 except Exception as e:report['error']={'type':type(e).__name__,'message':str(e).replace(token,'[REDACTED]')}
 finally:
  if created:
   try:
    clean=run("state="+repr(state)+"\n"+"""
if state.get('instance'):call('cleanup-instance','terminate_instances',InstanceIds=[state['instance']])
for k in ('restored_volume','volume','encrypted_volume'):
 if state.get(k):call('cleanup-'+k,'delete_volume',VolumeId=state[k])
if state.get('snapshot'):call('cleanup-snapshot','delete_snapshot',SnapshotId=state['snapshot'])
print(json.dumps(rows,default=str))
""");report['cleanup_api']=clean
   except Exception as e:report['cleanup_error']=str(e).replace(token,'[REDACTED]')
   try:report['logs']=command('docker','logs','--tail','45',name).replace(token,'[REDACTED]')
   except Exception:pass
   info=json.loads(command('docker','inspect',name))[0];assert info['Config']['Labels']['localcloud.audit']==name
   command('docker','rm','-f','-v',name);report['reference_removed']=True
  if state.get('guest_id'):
   # Successful normal termination should remove the guest. If controller recovery
   # failed, remove only the exact new container recorded by this fixture.
   p=subprocess.run(['docker','inspect',state['guest_id']],capture_output=True,text=True)
   if p.returncode==0:
    guest=json.loads(p.stdout)[0]
    assert state['instance'] in guest['Name']
    command('docker','rm','-f','-v',state['guest_id']);report['owned_guest_fallback_removed']=True
   report['guest_remaining']=subprocess.run(['docker','inspect',state['guest_id']],capture_output=True).returncode==0
  if volume_created:
   info=json.loads(command('docker','volume','inspect',volume))[0];assert info['Labels']['localcloud.audit']==name
   command('docker','volume','rm',volume);report['state_volume_removed']=True
  text=json.dumps(report,indent=2,default=str).replace(token,'[REDACTED]')+'\n'
  path=ROOT/'docs/research/rea'/('ec2-storage-persistence-'+args.backend+'.json');path.write_text(text)
  print(json.dumps({'artifact':str(path.relative_to(ROOT)),'sha256':hashlib.sha256(text.encode()).hexdigest(),'state_ids':state,'report':report},default=str).replace(token,'[REDACTED]'))
 return 0 if not report.get('error') and report.get('reference_removed') and report.get('state_volume_removed') and not report.get('guest_remaining') else 1
if __name__=='__main__':raise SystemExit(main())
