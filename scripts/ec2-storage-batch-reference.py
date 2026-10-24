"""Normal-license isolated mock EC2 multi-volume snapshot API observations.

API records do not establish atomic snapshots or actual file/block consistency.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
INNER = r'''
import boto3,json
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=25))
rows=[];instance=None;volumes=[];snapshots=[];baseline_snapshots=set()
def call(label,op,**kw):
 try:
  r=getattr(c,op)(**kw);meta=r.pop('ResponseMetadata',{});rows.append({'case':label,'operation':op,'parameters':kw,'status':meta.get('HTTPStatusCode'),'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'operation':op,'parameters':kw,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def batch(label,**kw):
 r=call(label,'create_snapshots',**kw)
 snapshots.extend(s['SnapshotId'] for s in r.get('Snapshots',[]))
 return r
try:
 shape=c.meta.service_model.operation_model('CreateSnapshots').input_shape
 rows.append({'case':'public-sdk-input-shapes','CreateSnapshots':list(shape.members),'InstanceSpecification':list(shape.members['InstanceSpecification'].members),'CopyTagsFromSource':{'type':shape.members['CopyTagsFromSource'].type_name,'enum':shape.members['CopyTagsFromSource'].enum}})
 baseline_snapshots={x['SnapshotId'] for x in c.describe_snapshots()['Snapshots']}
 launched=call('launch-three-mappings','run_instances',ImageId='ami-03cf127a',MinCount=1,MaxCount=1,BlockDeviceMappings=[{'DeviceName':'/dev/sda1','Ebs':{'VolumeSize':3,'DeleteOnTermination':True}},{'DeviceName':'/dev/sdf','Ebs':{'VolumeSize':1,'DeleteOnTermination':False}},{'DeviceName':'/dev/sdg','Ebs':{'VolumeSize':2,'DeleteOnTermination':True}}])
 instance=launched['Instances'][0]['InstanceId']
 full=call('describe-instance','describe_instances',InstanceIds=[instance])
 item=full['Reservations'][0]['Instances'][0]
 mappings=item.get('BlockDeviceMappings',[])
 volumes.extend(m['Ebs']['VolumeId'] for m in mappings)
 by_device={m['DeviceName']:m['Ebs']['VolumeId'] for m in mappings}
 for device,volume in by_device.items():
  call('tag-volume-'+device,'create_tags',Resources=[volume],Tags=[{'Key':'Origin','Value':device},{'Key':'Shared','Value':'volume'}])
 batch('all-volumes',InstanceSpecification={'InstanceId':instance},Description='All mappings')
 batch('exclude-boot',InstanceSpecification={'InstanceId':instance,'ExcludeBootVolume':True},Description='Data mappings')
 batch('explicit-include-boot',InstanceSpecification={'InstanceId':instance,'ExcludeBootVolume':False})
 batch('copy-volume-tags',InstanceSpecification={'InstanceId':instance},CopyTagsFromSource='volume')
 batch('snapshot-tag-overrides',InstanceSpecification={'InstanceId':instance},CopyTagsFromSource='volume',TagSpecifications=[{'ResourceType':'snapshot','Tags':[{'Key':'Shared','Value':'snapshot'},{'Key':'Batch','Value':'yes'}]}])
 batch('no-volume-tags',InstanceSpecification={'InstanceId':instance},TagSpecifications=[{'ResourceType':'snapshot','Tags':[{'Key':'Batch','Value':'only'}]}])
 if 'ExcludeDataVolumeIds' in shape.members['InstanceSpecification'].members:
  data=by_device.get('/dev/sdf')
  if data:batch('exclude-one-data',InstanceSpecification={'InstanceId':instance,'ExcludeDataVolumeIds':[data]})
  data_ids=[v for d,v in by_device.items() if d!='/dev/sda1']
  batch('exclude-boot-and-data',InstanceSpecification={'InstanceId':instance,'ExcludeBootVolume':True,'ExcludeDataVolumeIds':data_ids})
  batch('exclude-unknown-data',InstanceSpecification={'InstanceId':instance,'ExcludeDataVolumeIds':['vol-00000000000000000']})
 batch('invalid-copy-tag-value',InstanceSpecification={'InstanceId':instance},CopyTagsFromSource='invalid')
 before=c.describe_snapshots()
 batch('unknown-instance',InstanceSpecification={'InstanceId':'i-00000000000000000'})
 batch('missing-instance',InstanceSpecification={})
 after=c.describe_snapshots()
 rows.append({'case':'invalid-request-snapshot-count-comparison','before':len(before.get('Snapshots',[])),'after':len(after.get('Snapshots',[]))})
 call('all-volume-records','describe_volumes',VolumeIds=volumes)
except Exception as e:rows.append({'case':'scenario-error','error':{'type':type(e).__name__,'message':str(e)}})
finally:
 # The fixture owns the entire disposable reference; include any partial results.
 remaining=c.describe_snapshots()
 for snap in remaining.get('Snapshots',[]):
  if snap['SnapshotId'] not in baseline_snapshots:call('cleanup-delete-snapshot','delete_snapshot',SnapshotId=snap['SnapshotId'])
 if instance:call('cleanup-terminate','terminate_instances',InstanceIds=[instance])
 remaining=c.describe_volumes()
 for volume in remaining.get('Volumes',[]):
  if volume['VolumeId'] in volumes:call('cleanup-delete-volume','delete_volume',VolumeId=volume['VolumeId'])
print(json.dumps(rows,default=str))
'''


def main():
    name='localcloud-rea-storage-batch-'+uuid.uuid4().hex[:10]
    token=(Path.home()/'.config/localstack/rea-auth-token').read_text().strip()
    env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=token)
    report={'reference_image':IMAGE,'executor':'mock','limits':['API metadata observations only; no atomic byte-level snapshot conclusion.']}
    created=False
    try:
        subprocess.run(['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-e','LOCALSTACK_AUTH_TOKEN','-e','EC2_VM_MANAGER=mock',IMAGE],env=env,check=True,capture_output=True);created=True
        info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
        port=info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
        for _ in range(100):
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health',timeout=1) as response:
                    if response.status==200:break
            except OSError:time.sleep(.5)
        else:raise RuntimeError('Reference health deadline exceeded')
        p=subprocess.run(['docker','exec','-i',name,'/opt/code/localstack/.venv/bin/python','-'],input=INNER,capture_output=True,text=True,timeout=180)
        report.update(exit=p.returncode,records=json.loads(p.stdout) if not p.returncode else [],stderr=p.stderr[-2000:].replace(token,'[REDACTED]'))
    except Exception as e:report['error']=str(e).replace(token,'[REDACTED]')
    finally:
        if created:
            info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
            assert info['Config']['Labels']['localcloud.audit']==name
            subprocess.run(['docker','rm','-f','-v',name],check=True,capture_output=True)
            report['controller_cleanup_completed']=True
        text=json.dumps(report,indent=2).replace(token,'[REDACTED]')
        (ROOT/'docs/research/rea/ec2-storage-batch-reference.json').write_text(text+'\n')
    print(json.dumps({'reference_image':report['reference_image'],'exit':report.get('exit'),'cases':[r for r in report.get('records',[]) if not r['case'].startswith('cleanup-')],'controller_cleanup_completed':report.get('controller_cleanup_completed')}))
    return 0 if report.get('exit')==0 and not any(r['case']=='scenario-error' for r in report.get('records',[])) and report.get('controller_cleanup_completed') else 1


if __name__=='__main__':raise SystemExit(main())
