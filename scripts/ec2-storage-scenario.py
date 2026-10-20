"""Bounded EBS attribute/copy reference scenario; no real AWS endpoint by default."""
import json,os
import boto3
from botocore.config import Config

def scenario():
 c=boto3.client('ec2',endpoint_url=os.environ.get('EC2_PROBE_ENDPOINT','http://127.0.0.1:4566'),region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',aws_session_token=os.environ.get('EC2_PROBE_TOKEN'),config=Config(retries={'max_attempts':0},read_timeout=25))
 rows=[];cleanup=[]
 def call(label,op,**args):
  try:
   result=getattr(c,op)(**args);status=result.pop('ResponseMetadata')['HTTPStatusCode'];rows.append({'case':label,'operation':op,'parameters':args,'status':status,'result':result});return result
  except Exception as error:
   r=getattr(error,'response',{});rows.append({'case':label,'operation':op,'parameters':args,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(error).__name__,'message':str(error)})});return {}
 try:
  call('regions','describe_regions',RegionNames=['us-east-1'])
  call('account-vpc','describe_account_attributes',AttributeNames=['supported-platforms','default-vpc'])
  volume=call('volume','create_volume',AvailabilityZone='us-east-1a',Size=1)['VolumeId'];cleanup.append(('delete_volume',{'VolumeId':volume}))
  call('volume-auto-io','describe_volume_attribute',VolumeId=volume,Attribute='autoEnableIO')
  call('volume-modify','modify_volume_attribute',VolumeId=volume,AutoEnableIO={'Value':True})
  call('volume-auto-io-modified','describe_volume_attribute',VolumeId=volume,Attribute='autoEnableIO')
  snap=call('snapshot','create_snapshot',VolumeId=volume,Description='Before copy')['SnapshotId'];cleanup.append(('delete_snapshot',{'SnapshotId':snap}))
  call('snapshot-permissions','describe_snapshot_attribute',SnapshotId=snap,Attribute='createVolumePermission')
  call('snapshot-permission-add','modify_snapshot_attribute',SnapshotId=snap,CreateVolumePermission={'Add':[{'UserId':'111111111111'}]})
  call('snapshot-permissions-modified','describe_snapshot_attribute',SnapshotId=snap,Attribute='createVolumePermission')
  call('snapshot-permission-reset','reset_snapshot_attribute',SnapshotId=snap,Attribute='createVolumePermission')
  call('snapshot-permissions-reset','describe_snapshot_attribute',SnapshotId=snap,Attribute='createVolumePermission')
  copied=call('copy-snapshot','copy_snapshot',SourceRegion='us-east-1',SourceSnapshotId=snap,Description='Copied')['SnapshotId'];cleanup.append(('delete_snapshot',{'SnapshotId':copied}))
  call('describe-copy','describe_snapshots',SnapshotIds=[copied])
  instance=call('instance','run_instances',ImageId=os.environ.get('EC2_PROBE_IMAGE','ami-03cf127a'),MinCount=1,MaxCount=1)['Instances'][0]['InstanceId'];cleanup.append(('terminate_instances',{'InstanceIds':[instance]}))
  batch=call('snapshot-batch','create_snapshots',InstanceSpecification={'InstanceId':instance},Description='Batch')
  for item in batch.get('Snapshots',[]):cleanup.append(('delete_snapshot',{'SnapshotId':item['SnapshotId']}))
 except Exception as e:rows.append({'case':'probe-interrupted','status':None,'error':str(e)})
 finally:
  for op,args in reversed(cleanup):call('cleanup-'+op,op,**args)
 return rows
if __name__=='__main__':print(json.dumps(scenario(),default=str))
