"""SDK Query and localcloud CLI checks against an isolated LocalCloud backend."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
REF='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
spec=importlib.util.spec_from_file_location('audit',ROOT/'scripts/rea-runtime-audit.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
SDK=r'''
import boto3,json,os
from botocore.config import Config
c=boto3.client('ec2',endpoint_url=os.environ['PROBE_ENDPOINT'],region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',aws_session_token=os.environ['PROBE_TOKEN'],config=Config(retries={'max_attempts':0},read_timeout=20))
checks=[];volumes=[];snapshots=[];instance=None
try:
 assert c.get_ebs_encryption_by_default()['EbsEncryptionByDefault'] is False
 assert c.enable_ebs_encryption_by_default()['EbsEncryptionByDefault'] is True
 assert c.get_ebs_encryption_by_default()['EbsEncryptionByDefault'] is True
 checks.append('sdk-encryption-default-query-serialization')
 volume=c.create_volume(AvailabilityZone='us-east-1a',Size=1);volumes.append(volume['VolumeId'])
 assert volume['Encrypted'] is False
 assert c.disable_ebs_encryption_by_default()['EbsEncryptionByDefault'] is False
 checks.append('reference-compatible-default-flag-without-data-encryption-claim')
 c.modify_volume(VolumeId=volume['VolumeId'],Size=2)
 c.modify_volume(VolumeId=volume['VolumeId'],VolumeType='gp3')
 c.modify_volume(VolumeId=volume['VolumeId'],Iops=4000,Throughput=250)
 rows=c.describe_volumes_modifications(VolumeIds=[volume['VolumeId']])['VolumesModifications']
 assert len(rows)==3 and rows[0]['TargetSize']==2 and rows[1]['TargetVolumeType']=='gp3' and rows[2]['OriginalIops']==3000 and rows[2]['TargetThroughput']==250
 assert all(row['ModificationState']=='completed' and row['Progress']==100 for row in rows)
 checks.append('sdk-query-complete-volume-modification-history')
 instance=c.run_instances(ImageId='ami-local',MinCount=1,MaxCount=1,BlockDeviceMappings=[{'DeviceName':'/dev/sda1','Ebs':{'VolumeSize':1}},{'DeviceName':'/dev/sdf','Ebs':{'VolumeSize':1}}])['Instances'][0]
 data_volume=instance['BlockDeviceMappings'][1]['Ebs']['VolumeId']
 c.create_tags(Resources=[data_volume],Tags=[{'Key':'Origin','Value':'volume'},{'Key':'Shared','Value':'volume'}])
 result=c.create_snapshots(InstanceSpecification={'InstanceId':instance['InstanceId'],'ExcludeBootVolume':True},CopyTagsFromSource='volume',TagSpecifications=[{'ResourceType':'snapshot','Tags':[{'Key':'Shared','Value':'snapshot'}]}])
 snapshots.extend(x['SnapshotId'] for x in result['Snapshots'])
 assert len(result['Snapshots'])==1
 tags={t['Key']:t['Value'] for t in result['Snapshots'][0]['Tags']}
 assert tags=={'Origin':'volume','Shared':'snapshot'},tags
 checks.append('sdk-string-copy-tags-and-boot-volume-exclusion')
finally:
 if instance:c.terminate_instances(InstanceIds=[instance['InstanceId']])
 for snap in snapshots:c.delete_snapshot(SnapshotId=snap)
 for volume in volumes:c.delete_volume(VolumeId=volume)
print(json.dumps({'checks':checks,'passed':True,'cleanup_completed':True}))
'''

def main():
    report={'passed':False,'checks':[]}
    with tempfile.TemporaryDirectory(prefix='localcloud-storage-wire-') as temporary:
        with audit.backend(Path(temporary)/'state.db') as (engine,client,token):
            env=dict(os.environ,PROBE_ENDPOINT=engine.endpoint,PROBE_TOKEN=token)
            process=subprocess.run(['docker','run','--rm','--network','host','-i','-e','PROBE_ENDPOINT','-e','PROBE_TOKEN','--entrypoint','/opt/code/localstack/.venv/bin/python',REF,'-'],input=SDK,env=env,capture_output=True,text=True,timeout=90)
            if process.returncode:
                report['error']=process.stderr[-3000:]
            else:
                report.update(json.loads(process.stdout))
                env=dict(os.environ,LOCALCLOUD_CONFIG=str(Path(temporary)/'profiles.json'),LOCALCLOUD_MANAGEMENT_TOKEN=token)
                commands=[('enable-ebs-encryption-by-default',True),('get-ebs-encryption-by-default',True),('disable-ebs-encryption-by-default',False)]
                for name,expected in commands:
                    p=subprocess.run([sys.executable,'-m','localcloud.cli','--endpoint-url',engine.endpoint,'ec2',name],env=env,capture_output=True,text=True,timeout=15)
                    if p.returncode:raise RuntimeError(p.stderr)
                    assert json.loads(p.stdout)['EbsEncryptionByDefault'] is expected
                report['checks'].append('cli-enable-get-disable-ebs-encryption-default')
                report['remaining_volumes']=len(client.request('POST','/_local/ec2',{'Action':'DescribeVolumes'})['Volumes'])
                report['remaining_snapshots']=len(client.request('POST','/_local/ec2',{'Action':'DescribeSnapshots'})['Snapshots'])
                assert not report['remaining_volumes'] and not report['remaining_snapshots']
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] and report.get('cleanup_completed') else 1

if __name__=='__main__':raise SystemExit(main())
