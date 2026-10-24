"""Observe storage on isolated, normally licensed LocalStack Docker EC2 guests.

No host firewall or route changes. Only reference-created guests are inspected.
"""
import json
import os
import pathlib
import subprocess
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGE = 'localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
GUEST_IMAGE = 'ami-61ad6e59d7b0'
INNER = r'''

import boto3,docker,json,time
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=40))
d=docker.from_env();rows=[];instances=[];snapshots=[];volumes=[]
def call(label,op,**kw):
 try:
  r=getattr(c,op)(**kw);status=r.pop('ResponseMetadata')['HTTPStatusCode'];rows.append({'case':label,'operation':op,'parameters':kw,'status':status,'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'operation':op,'parameters':kw,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def guest_for(i):
 for _ in range(50):
  gs=[x for x in d.containers.list(all=True) if i in x.name]
  if gs:return gs[0]
  time.sleep(.2)
 raise RuntimeError('Guest missing for '+i)
def inspect(label,g):
 g.reload();r=g.exec_run(['sh','-c','printf "DEVICES\\n"; test ! -e /dev/sdf || ls -l /dev/sdf; test ! -e /dev/sdg || ls -l /dev/sdg; printf "MOUNTS\\n"; cat /proc/mounts; printf "MARKER\\n"; test ! -f /tmp/rea-storage-marker || cat /tmp/rea-storage-marker'],user='root')
 rows.append({'case':label,'container_id':g.id,'mounts':g.attrs['Mounts'],'guest_exit':r.exit_code,'guest_output':r.output.decode(errors='replace')[:20000]})
try:
 mappings=[{'DeviceName':'/dev/sda1','Ebs':{'VolumeSize':3,'DeleteOnTermination':True}},{'DeviceName':'/dev/sdf','Ebs':{'VolumeSize':1,'DeleteOnTermination':False}},{'DeviceName':'/dev/sdg','Ebs':{'VolumeSize':2,'DeleteOnTermination':True}}]
 launched=call('launch-root-data-mappings','run_instances',ImageId='ami-61ad6e59d7b0',InstanceType='t3.micro',MinCount=1,MaxCount=1,BlockDeviceMappings=mappings)
 i=launched['Instances'][0]['InstanceId'];instances.append(i);g=guest_for(i)
 detail=call('describe-launched','describe_instances',InstanceIds=[i])['Reservations'][0]['Instances'][0]
 mapped=detail.get('BlockDeviceMappings',[]);volumes.extend(x['Ebs']['VolumeId'] for x in mapped)
 inspect('launch-guest-storage',g)
 written=g.exec_run(['sh','-c','echo root-snapshot-marker >/tmp/rea-storage-marker; sync; cat /tmp/rea-storage-marker'],user='root'); rows.append({'case':'source-root-marker-written','exit':written.exit_code,'output':written.output.decode(errors='replace')}); assert written.exit_code==0 and written.output==b'root-snapshot-marker\n'
 root=next(x['Ebs']['VolumeId'] for x in mapped if x['DeviceName']=='/dev/sda1')
 s=call('snapshot-root','create_snapshot',VolumeId=root)['SnapshotId'];snapshots.append(s)
 call('describe-root-snapshot','describe_snapshots',SnapshotIds=[s])
 restored=call('restore-root-volume','create_volume',AvailabilityZone='us-east-1a',SnapshotId=s)['VolumeId'];volumes.append(restored)
 call('describe-restored-volume','describe_volumes',VolumeIds=[restored])
 r2=call('launch-from-root-snapshot','run_instances',ImageId='ami-61ad6e59d7b0',InstanceType='t3.micro',MinCount=1,MaxCount=1,BlockDeviceMappings=[{'DeviceName':'/dev/sda1','Ebs':{'SnapshotId':s,'VolumeSize':3,'DeleteOnTermination':True}}])
 if r2.get('Instances'):
  i2=r2['Instances'][0]['InstanceId'];instances.append(i2);g2=guest_for(i2);inspect('snapshot-launched-guest',g2)
  x=call('describe-snapshot-launched','describe_instances',InstanceIds=[i2]);volumes.extend(m['Ebs']['VolumeId'] for m in x['Reservations'][0]['Instances'][0].get('BlockDeviceMappings',[]))
 call('terminate-mapped-instance','terminate_instances',InstanceIds=[i]);instances.remove(i)
 for m in mapped:call('post-terminate-'+m['DeviceName'],'describe_volumes',VolumeIds=[m['Ebs']['VolumeId']])
except Exception as e:rows.append({'case':'scenario-error','error':{'type':type(e).__name__,'message':str(e)}})
finally:
 for i in instances:call('cleanup-terminate','terminate_instances',InstanceIds=[i])
 for v in dict.fromkeys(volumes):call('cleanup-delete-volume','delete_volume',VolumeId=v)
 for s in snapshots:call('cleanup-delete-snapshot','delete_snapshot',SnapshotId=s)
 rows.append({'case':'cleanup-guest-containers','remaining':[x.id for x in d.containers.list(all=True) if any(i in x.name for i in instances)]})
print(json.dumps(rows,default=str))

'''


def main():
    name = 'localcloud-rea-storage-edges-' + uuid.uuid4().hex[:10]
    env = dict(os.environ, LOCALSTACK_AUTH_TOKEN=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
    baseline_networks = json.loads(subprocess.check_output(['docker', 'network', 'ls', '--filter', 'name=^localstack-ec2-link-local$', '--format', '{{json .}}']).decode().strip() or 'null')
    report = {'reference_image': IMAGE, 'guest_image': GUEST_IMAGE, 'reference_container': name,
              'baseline_link_local_network': baseline_networks,
              'scope_limits': ['Two normally licensed Docker guests, explicit root/data mappings, one snapshot and a restored volume.',
                               'A successful API encryption flag does not establish at-rest encryption.',
                               'Root snapshot launch and deletion metadata tested; live-writer crash consistency, raw block I/O and physical encryption not established.',
                               'No encrypted provider code was inspected; no host block device or filesystem was modified.']}
    created = False
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--label', 'localcloud.audit='+name,
                        '-p', '127.0.0.1::4566', '-v', '/var/run/docker.sock:/var/run/docker.sock',
                        '-e', 'LOCALSTACK_AUTH_TOKEN', '-e', 'EC2_VM_MANAGER=docker', IMAGE], env=env, check=True, capture_output=True)
        created = True
        info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
        port = info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
        for _ in range(120):
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.5)
        else:
            raise RuntimeError('Reference health deadline exceeded')
        p = subprocess.run(['docker', 'exec', '-i', name, '/opt/code/localstack/.venv/bin/python', '-'], input=INNER, capture_output=True, text=True, timeout=210)
        report.update(exit=p.returncode, records=json.loads(p.stdout) if p.returncode == 0 else [], stderr=p.stderr[-2000:])
    finally:
        if created:
            info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
            assert info['Config']['Labels']['localcloud.audit'] == name
            subprocess.run(['docker', 'rm', '-f', '-v', name], check=True, capture_output=True)
            report['reference_container_removed'] = True
        report['captured_guest_containers_remaining'] = []
        for row in report.get('records', []):
            if row['case'].startswith('launch-') and row.get('result', {}).get('Instances'):
                instance_id = row['result']['Instances'][0]['InstanceId']
                report['captured_guest_containers_remaining'] = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=localstack-ec2.'+instance_id]).decode().split()
        # The link-local network is globally shared by LocalStack. Preserve a preexisting one.
        report['link_local_network_cleanup'] = 'preexisting shared network preserved' if baseline_networks else 'not removed without exclusive ownership label'
    (ROOT/'docs/research/rea/ec2-storage-edges-reference.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, default=str))
    return 0 if report.get('exit') == 0 and not any(r['case']=='scenario-error' for r in report.get('records', [])) else 1



if __name__ == '__main__':
    raise SystemExit(main())
