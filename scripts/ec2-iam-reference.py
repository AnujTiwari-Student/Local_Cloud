"""Licensed, isolated LocalStack IAM/STS/EC2 instance-profile observations.

Credential values and the LocalStack license token are never written to reports.
Only API responses and owned guest behavior are inspected, not provider code.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
INNER=r'''
import boto3,docker,json,time,uuid
from botocore.config import Config
config=Config(retries={'max_attempts':0},read_timeout=50)
clients={s:boto3.client(s,endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=config) for s in ('iam','sts','ec2')}
rows=[];roles=[];profiles=[];instances=[];guest_ids=[]
def redact(value):
 if isinstance(value,dict):
  return {k:({'redacted':True,'length':len(str(v)),'prefix':str(v)[:4] if k=='AccessKeyId' else None} if k in ('AccessKeyId','SecretAccessKey','SessionToken','Token') else redact(v)) for k,v in value.items()}
 if isinstance(value,list):return [redact(v) for v in value]
 return value
def call(label,service,op,**args):
 try:
  r=getattr(clients[service],op)(**args);meta=r.pop('ResponseMetadata',{});rows.append({'case':label,'service':service,'operation':op,'parameters':args,'status':meta.get('HTTPStatusCode'),'result':redact(r)});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'service':service,'operation':op,'parameters':args,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def direct_metadata(i,case):
 import urllib.request,urllib.error
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base='http://127.0.0.1:4510/latest/'
 headers={'x-localstack-imds-account-id':'000000000000','x-localstack-imds-region-name':'us-east-1','x-localstack-imds-instance-id':i}
 observed=[]
 def req(path,method='GET',extra=None):
  try:
   r=op.open(urllib.request.Request(base+path,method=method,data=b'' if method=='PUT' else None,headers={**headers,**(extra or {})}),timeout=2)
   return r.status,r.read().decode()
  except urllib.error.HTTPError as error:return error.code,error.read().decode()
  except Exception as error:return None,str(error)
 for path in ('meta-data/instance-id','meta-data/iam/','meta-data/iam/info','meta-data/iam/security-credentials/'):
  status,body=req(path);observed.append({'path':path,'status':status,'body':body})
  if path.endswith('security-credentials/') and status==200:
   for role in body.strip().splitlines():
    status,raw=req(path+role)
    try:
     creds=json.loads(raw);body=redact(creds)
     if all(k in creds for k in ('AccessKeyId','SecretAccessKey','Token')):
      probe=boto3.client('sts',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id=creds['AccessKeyId'],aws_secret_access_key=creds['SecretAccessKey'],aws_session_token=creds['Token'],config=config)
      identity=probe.get_caller_identity();identity.pop('ResponseMetadata',None);observed.append({'case':'imds-credential-getcalleridentity','result':identity})
    except Exception:body={'error':'credential or identity response could not be parsed; raw payload omitted'}
    observed.append({'path':path+role,'status':status,'body':body})
 status,token=req('api/token','PUT',{'X-aws-ec2-metadata-token-ttl-seconds':'60'});observed.append({'case':'token-issuance','status':status,'length':len(token) if status==200 else 0})
 if status==200:
  s,b=req('meta-data/iam/security-credentials/',extra={'X-aws-ec2-metadata-token':token});observed.append({'case':'token-role-list','status':s,'body':b})
 rows.append({'case':case,'transport':'reference-localhost4510 with explicit owninstanceidentityheaders','instance_id':i,'observations':observed})
def guest_probe(i,case):
 if DIRECT_METADATA:return direct_metadata(i,case)
 d=docker.from_env();guest=None
 for _ in range(40):
  candidates=[x for x in d.containers.list(all=True) if i in x.name]
  if candidates:guest=candidates[0];break
  time.sleep(.25)
 if not guest:rows.append({'case':case,'error':'owned guest not found'});return
 if guest.id not in guest_ids:guest_ids.append(guest.id)
 code=r''' + '"""' + r'''
import json,urllib.request,urllib.error
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base='http://169.254.169.254/latest/';out=[]
def req(path,method='GET',headers=None):
 try:
  r=op.open(urllib.request.Request(base+path,method=method,data=b'' if method=='PUT' else None,headers=headers or {}),timeout=1);data=r.read().decode();return r.status,data
 except urllib.error.HTTPError as e:return e.code,e.read().decode()
 except Exception as e:return None,str(e)
for path in ('meta-data/iam/','meta-data/iam/info','meta-data/iam/security-credentials/'):
 status,data=req(path);out.append({'path':path,'status':status,'body':data})
 if path.endswith('security-credentials/') and status==200:
  for role in data.strip().splitlines():
   status,raw=req(path+role)
   try:
    body=json.loads(raw)
    for key in ('AccessKeyId','SecretAccessKey','Token','SessionToken'):
     if key in body:body[key]={'redacted':True,'length':len(body[key]),'prefix':body[key][:4] if key=='AccessKeyId' else None}
   except Exception:body=raw
   out.append({'path':path+role,'status':status,'body':body})
status,token=req('api/token','PUT',{'X-aws-ec2-metadata-token-ttl-seconds':'60'})
out.append({'case':'imdsv2-token','status':status,'length':len(token) if status==200 else 0})
if status==200:
 s,b=req('meta-data/iam/security-credentials/',headers={'X-aws-ec2-metadata-token':token});out.append({'case':'imdsv2-role-list','status':s,'body':b})
print(json.dumps(out))
''' + '"""' + r'''
 for attempt in range(2):
  result=guest.exec_run(['python3','-c',code],user='root')
  try:
   out=json.loads(result.output.decode());
   if any(x.get('status') for x in out):break
  except Exception:out={'error':result.output.decode(errors='replace')[:300]}
  time.sleep(.5)
 rows.append({'case':case,'instance_id':i,'guest_id':guest.id,'exit':result.exit_code,'observations':out})
try:
 suffix=uuid.uuid4().hex[:8];r1='rea-ec2-role-'+suffix;r2=r1+'-b';p1='rea-ec2-profile-'+suffix;p2=p1+'-b'
 trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com','AWS':'arn:aws:iam::000000000000:root'},'Action':'sts:AssumeRole'}]})
 call('missing-role','iam','get_role',RoleName='missing-role-'+suffix)
 call('missing-profile','iam','get_instance_profile',InstanceProfileName='missing-profile-'+suffix)
 for role in (r1,r2):
  r=call('create-role-'+role,'iam','create_role',RoleName=role,AssumeRolePolicyDocument=trust);roles.append(role)
 call('duplicate-role','iam','create_role',RoleName=r1,AssumeRolePolicyDocument=trust)
 call('put-role-policy','iam','put_role_policy',RoleName=r1,PolicyName='read-fixture',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:ListAllMyBuckets','sts:GetCallerIdentity'],'Resource':'*'}]}))
 call('get-role-policy','iam','get_role_policy',RoleName=r1,PolicyName='read-fixture')
 for profile in (p1,p2):call('create-profile-'+profile,'iam','create_instance_profile',InstanceProfileName=profile);profiles.append(profile)
 call('duplicate-profile','iam','create_instance_profile',InstanceProfileName=p1)
 call('empty-profile','iam','get_instance_profile',InstanceProfileName=p1)
 call('add-role','iam','add_role_to_instance_profile',InstanceProfileName=p1,RoleName=r1)
 call('duplicate-add-role','iam','add_role_to_instance_profile',InstanceProfileName=p1,RoleName=r1)
 call('add-second-role','iam','add_role_to_instance_profile',InstanceProfileName=p1,RoleName=r2)
 call('add-other-profile-role','iam','add_role_to_instance_profile',InstanceProfileName=p2,RoleName=r2)
 call('populated-profile','iam','get_instance_profile',InstanceProfileName=p1)
 call('list-profiles','iam','list_instance_profiles')
 call('list-profiles-for-role','iam','list_instance_profiles_for_role',RoleName=r1)
 assumed=call('assume-role','sts','assume_role',RoleArn='arn:aws:iam::000000000000:role/'+r1,RoleSessionName='rea-session',DurationSeconds=900)
 if assumed.get('Credentials'):
  creds=assumed['Credentials'];clients['assumed']=boto3.client('sts',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id=creds['AccessKeyId'],aws_secret_access_key=creds['SecretAccessKey'],aws_session_token=creds['SessionToken'],config=config)
  call('assumed-get-caller-identity','assumed','get_caller_identity')
 call('assume-missing-role','sts','assume_role',RoleArn='arn:aws:iam::000000000000:role/no-such-role-'+suffix,RoleSessionName='rea-missing')
 call('root-get-caller-identity','sts','get_caller_identity')
 invalid=call('run-invalid-profile','ec2','run_instances',ImageId='ami-61ad6e59d7b0',MinCount=1,MaxCount=1,InstanceType='t3.micro',IamInstanceProfile={'Name':'missing-profile-'+suffix})
 instances.extend(x['InstanceId'] for x in invalid.get('Instances',[]))
 launched=call('run-with-profile','ec2','run_instances',ImageId='ami-61ad6e59d7b0',MinCount=1,MaxCount=1,InstanceType='t3.micro',IamInstanceProfile={'Name':p1})
 if not launched.get('Instances'):raise RuntimeError('Reference instance launch failed')
 i=launched['Instances'][0]['InstanceId'];instances.append(i)
 call('describe-launch-profile','ec2','describe_instances',InstanceIds=[i])
 assoc=call('describe-associations','ec2','describe_iam_instance_profile_associations',Filters=[{'Name':'instance-id','Values':[i]}])
 guest_probe(i,'launch-profile-metadata')
 call('duplicate-association','ec2','associate_iam_instance_profile',InstanceId=i,IamInstanceProfile={'Name':p1})
 aid=next((x['AssociationId'] for x in assoc.get('IamInstanceProfileAssociations',[]) if x.get('State')!='disassociated'),None)
 if aid:
  call('replace-profile','ec2','replace_iam_instance_profile_association',AssociationId=aid,IamInstanceProfile={'Name':p2})
  call('describe-replaced','ec2','describe_iam_instance_profile_associations',AssociationIds=[aid])
  guest_probe(i,'replaced-profile-metadata')
  call('disassociate-profile','ec2','disassociate_iam_instance_profile',AssociationId=aid)
  call('describe-disassociated','ec2','describe_iam_instance_profile_associations',AssociationIds=[aid])
  guest_probe(i,'disassociated-profile-metadata')
 call('associate-existing','ec2','associate_iam_instance_profile',InstanceId=i,IamInstanceProfile={'Arn':'arn:aws:iam::000000000000:instance-profile/'+p1})
 call('missing-instance-associate','ec2','associate_iam_instance_profile',InstanceId='i-00000000000000000',IamInstanceProfile={'Name':p1})
 call('missing-association-replace','ec2','replace_iam_instance_profile_association',AssociationId='iip-assoc-00000000000000000',IamInstanceProfile={'Name':p1})
 call('missing-association-disassociate','ec2','disassociate_iam_instance_profile',AssociationId='iip-assoc-00000000000000000')
except Exception as error:rows.append({'case':'scenario-error','error':type(error).__name__+': '+str(error)})
finally:
 for i in instances:call('cleanup-terminate','ec2','terminate_instances',InstanceIds=[i])
 for profile in profiles:
  current=call('cleanup-profile-read','iam','get_instance_profile',InstanceProfileName=profile)
  for role in current.get('InstanceProfile',{}).get('Roles',[]):call('cleanup-remove-role','iam','remove_role_from_instance_profile',InstanceProfileName=profile,RoleName=role['RoleName'])
  call('cleanup-delete-profile','iam','delete_instance_profile',InstanceProfileName=profile)
 for role in roles:
  if role==roles[0]:call('cleanup-delete-role-policy','iam','delete_role_policy',RoleName=role,PolicyName='read-fixture')
  call('cleanup-delete-role','iam','delete_role',RoleName=role)
 # Only previously captured exact guest IDs may be forcibly cleaned.
 d=docker.from_env()
 for gid in guest_ids:
  try:g=d.containers.get(gid);g.remove(force=True)
  except docker.errors.NotFound:pass
 rows.append({'case':'cleanup-guests','captured_ids':guest_ids,'remaining':[x.id for x in d.containers.list(all=True) if x.id in guest_ids]})
print(json.dumps(rows,default=str))
'''

INNER="DIRECT_METADATA="+repr("--direct-metadata" in sys.argv)+"\n"+INNER

if "--api-only" in sys.argv:
 INNER=INNER[:INNER.index(" invalid=call('run-invalid-profile'")]+INNER[INNER.index("except Exception as error:rows.append("):]

def main():
 name='localcloud-rea-iam-'+uuid.uuid4().hex[:10]
 env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=(Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
 report={'reference_image':IMAGE,'reference_container':name,'scope':'normal licensed Docker executor; isolated fixture roles, profiles and guest; no provider extraction','credential_redaction':'All API and IMDS credential values replaced with length/prefix metadata before stdout or report persistence'}
 created=False
 try:
  subprocess.run(['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-v','/var/run/docker.sock:/var/run/docker.sock','-e','LOCALSTACK_AUTH_TOKEN','-e','EC2_VM_MANAGER=docker',IMAGE],env=env,check=True,capture_output=True);created=True
  info=json.loads(subprocess.check_output(['docker','inspect',name]))[0];port=info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
  for _ in range(120):
   try:
    with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health',timeout=1) as response:
     if response.status==200:break
   except OSError:time.sleep(.5)
  else:raise RuntimeError('Reference health deadline exceeded')
  p=subprocess.run(['docker','exec','-i',name,'/opt/code/localstack/.venv/bin/python','-'],input=INNER,capture_output=True,text=True,timeout=240)
  report.update(exit=p.returncode,records=json.loads(p.stdout) if p.returncode==0 else [],stderr=p.stderr[-2000:])
 finally:
  if created:
   obj=json.loads(subprocess.check_output(['docker','inspect',name]))[0];assert obj['Config']['Labels']['localcloud.audit']==name
   subprocess.run(['docker','rm','-f','-v',name],check=True,capture_output=True);report['reference_container_removed']=True
 path=ROOT/('docs/research/rea/ec2-iam-api-reference-report.json' if '--api-only' in sys.argv else ('docs/research/rea/ec2-iam-direct-metadata-reference-report.json' if '--direct-metadata' in sys.argv else 'docs/research/rea/ec2-iam-reference-report.json'));path.write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report,default=str));return 0 if report.get('exit')==0 and not any(x['case']=='scenario-error' for x in report.get('records',[])) else 1
if __name__=='__main__':raise SystemExit(main())
