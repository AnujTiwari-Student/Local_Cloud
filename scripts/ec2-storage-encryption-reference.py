"""Bounded normal-license EBS encryption/default-key and resize API probe.

This tests metadata and validation in one isolated mock executor. API flags do
not establish actual encryption, block devices, quotas or performance limits.
"""
import json, os, pathlib, subprocess, time, urllib.request, uuid
ROOT=pathlib.Path(__file__).resolve().parents[1]
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
INNER=r'''
import boto3,json
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=20))
rows=[];volumes=[];snapshots=[]
def call(label,op,**args):
 try:
  r=getattr(c,op)(**args);status=r.pop('ResponseMetadata')['HTTPStatusCode'];rows.append({'case':label,'operation':op,'parameters':args,'status':status,'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'operation':op,'parameters':args,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def volume(label,**args):
 r=call(label,'create_volume',AvailabilityZone='us-east-1a',Size=1,**args)
 if r.get('VolumeId'):volumes.append(r['VolumeId'])
 return r
try:
 call('initial-encryption-default','get_ebs_encryption_by_default')
 call('initial-default-key','get_ebs_default_kms_key_id')
 v=volume('unencrypted-default')
 call('enable-default-encryption','enable_ebs_encryption_by_default')
 call('enabled-encryption-default','get_ebs_encryption_by_default')
 volume('implicit-after-enable')
 volume('explicit-false-after-enable',Encrypted=False)
 call('set-default-key','modify_ebs_default_kms_key_id',KmsKeyId='arn:aws:kms:us-east-1:000000000000:key/11111111-2222-3333-4444-555555555555')
 call('modified-default-key','get_ebs_default_kms_key_id')
 # A deliberately invalid key's 500 does not establish whether valid KMS integration works.
 kms=boto3.client('kms',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=20))
 try:
  kr=kms.create_key(Description='Isolated EC2 default key probe');km=kr['KeyMetadata'];rows.append({'case':'create-local-kms-key','operation':'kms.create_key','status':kr['ResponseMetadata']['HTTPStatusCode'],'result':{'KeyId':km['KeyId'],'Arn':km['Arn']}})
  call('set-valid-default-key','modify_ebs_default_kms_key_id',KmsKeyId=km['Arn'])
  call('valid-default-key-read','get_ebs_default_kms_key_id')
  volume('implicit-after-valid-default-key')
  dr=kms.schedule_key_deletion(KeyId=km['KeyId'],PendingWindowInDays=7);rows.append({'case':'cleanup-local-kms-key','operation':'kms.schedule_key_deletion','status':dr['ResponseMetadata']['HTTPStatusCode']})
 except Exception as e:
  er=getattr(e,'response',{});rows.append({'case':'valid-key-followup-error','status':er.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':er.get('Error',{'type':type(e).__name__,'message':str(e)})})
 volume('implicit-with-default-key')
 volume('explicit-key-encrypted',Encrypted=True,KmsKeyId='arn:aws:kms:us-east-1:000000000000:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
 volume('explicit-key-unencrypted',Encrypted=False,KmsKeyId='alias/rea-key')
 call('reset-default-key','reset_ebs_default_kms_key_id')
 call('reset-key-read','get_ebs_default_kms_key_id')
 call('disable-default-encryption','disable_ebs_encryption_by_default')
 call('disabled-encryption-default','get_ebs_encryption_by_default')
 encrypted=volume('encrypted-for-snapshot',Encrypted=True)
 if encrypted.get('VolumeId'):
  s=call('encrypted-snapshot','create_snapshot',VolumeId=encrypted['VolumeId']).get('SnapshotId')
  if s:
   snapshots.append(s)
   volume('restore-encrypted-default',SnapshotId=s)
   volume('restore-encrypted-false',SnapshotId=s,Encrypted=False)
   volume('restore-encrypted-key',SnapshotId=s,Encrypted=True,KmsKeyId='alias/rea-key')
 if v.get('VolumeId'):
  key=v['VolumeId']
  call('resize-grow','modify_volume',VolumeId=key,Size=2)
  call('resize-shrink','modify_volume',VolumeId=key,Size=1)
  call('resize-zero','modify_volume',VolumeId=key,Size=0)
  call('resize-gp3','modify_volume',VolumeId=key,VolumeType='gp3',Iops=3000,Throughput=125)
  call('resize-low-iops','modify_volume',VolumeId=key,Iops=1)
  call('resize-low-throughput','modify_volume',VolumeId=key,Throughput=1)
  call('resize-invalid-type','modify_volume',VolumeId=key,VolumeType='not-real')
  call('resize-describe','describe_volumes',VolumeIds=[key])
  call('resize-modifications','describe_volumes_modifications',VolumeIds=[key])
except Exception as e:rows.append({'case':'scenario-error','error':str(e)})
finally:
 for s in reversed(snapshots):call('cleanup-snapshot','delete_snapshot',SnapshotId=s)
 for v in reversed(volumes):call('cleanup-volume','delete_volume',VolumeId=v)
print(json.dumps(rows,default=str))
'''

def main():
 name='localcloud-rea-storage-encryption-'+uuid.uuid4().hex[:10]
 token=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip()
 env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=token);created=False;report={'reference_image':IMAGE,'executor':'mock','scope':'Metadata and validation only; no encryption or raw-device claim'}
 try:
  subprocess.run(['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-e','LOCALSTACK_AUTH_TOKEN','-e','EC2_VM_MANAGER=mock',IMAGE],env=env,capture_output=True,check=True,timeout=30);created=True
  info=json.loads(subprocess.check_output(['docker','inspect',name]))[0];port=info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
  for _ in range(120):
   try:
    with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health',timeout=1) as r:
     if r.status==200:break
   except OSError:time.sleep(.5)
  else:raise RuntimeError('Reference health deadline exceeded')
  p=subprocess.run(['docker','exec','-i',name,'/opt/code/localstack/.venv/bin/python','-'],input=INNER,capture_output=True,text=True,timeout=220)
  report.update(exit=p.returncode,records=json.loads(p.stdout) if p.returncode==0 else [],stderr=p.stderr[-2000:])
 except Exception as e:report['error']=str(e).replace(token,'[REDACTED]')
 finally:
  if created:
   info=json.loads(subprocess.check_output(['docker','inspect',name]))[0];assert info['Config']['Labels']['localcloud.audit']==name
   subprocess.run(['docker','rm','-f','-v',name],capture_output=True,check=True,timeout=30);report['cleanup_completed']=True
  out=ROOT/'docs/research/rea/ec2-storage-encryption-reference.json';out.write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report))
 return 0 if report.get('exit')==0 and report.get('cleanup_completed') and not any(r['case']=='scenario-error' for r in report.get('records',[])) else 1
if __name__=='__main__':raise SystemExit(main())
