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
d=docker.from_env();rows=[];cleanup=[];guest=None
def call(label,op,**kw):
 try:
  r=getattr(c,op)(**kw);status=r.pop('ResponseMetadata')['HTTPStatusCode'];rows.append({'case':label,'operation':op,'parameters':kw,'status':status,'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':label,'operation':op,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def inspect(label):
 guest.reload();r=guest.exec_run(['sh','-c','cat /proc/mounts; command -v lsblk >/dev/null && lsblk -J || true; test ! -f /tmp/rea-root-marker || cat /tmp/rea-root-marker'],user='root')
 rows.append({'case':label,'container_id':guest.id,'mounts':guest.attrs['Mounts'],'privileged':guest.attrs['HostConfig']['Privileged'],'guest_exit':r.exit_code,'guest_output':r.output.decode(errors='replace')[:18000]})
try:
 v=call('create-volume','create_volume',AvailabilityZone='us-east-1a',Size=1)['VolumeId'];cleanup.append(('delete_volume',{'VolumeId':v}))
 call('create-encrypted','create_volume',AvailabilityZone='us-east-1a',Size=1,Encrypted=True)
 # Include every created volume in cleanup, even if encryption is metadata-only.
 for row in rows[:]:
  if row['case']=='create-encrypted' and row.get('result',{}).get('VolumeId'):cleanup.append(('delete_volume',{'VolumeId':row['result']['VolumeId']}))
 before={x.id for x in d.containers.list(all=True)}
 i=call('run','run_instances',ImageId='ami-61ad6e59d7b0',InstanceType='t3.micro',MinCount=1,MaxCount=1)['Instances'][0]['InstanceId'];cleanup.append(('terminate_instances',{'InstanceIds':[i]}))
 for _ in range(60):
  gs=[x for x in d.containers.list(all=True) if x.id not in before and i in x.name]
  if gs:guest=gs[0];break
  time.sleep(.5)
 if guest is None:raise RuntimeError('Owned guest not found')
 inspect('before-attach')
 call('attach-running','attach_volume',VolumeId=v,InstanceId=i,Device='/dev/sdf');inspect('after-attach')
 guest.exec_run(['sh','-c','echo root-survives >/tmp/rea-root-marker'],user='root')
 call('stop','stop_instances',InstanceIds=[i]);call('attach-stopped','attach_volume',VolumeId=v,InstanceId=i,Device='/dev/sdf')
 call('start','start_instances',InstanceIds=[i]);time.sleep(1);inspect('after-restart')
 call('grow-volume','modify_volume',VolumeId=v,Size=2)
 s=call('snapshot','create_snapshot',VolumeId=v).get('SnapshotId')
 if s:
  cleanup.append(('delete_snapshot',{'SnapshotId':s}));call('describe-snapshot','describe_snapshots',SnapshotIds=[s])
 call('detach-running','detach_volume',VolumeId=v,InstanceId=i,Device='/dev/sdf',Force=True);inspect('after-detach')
 call('describe-volumes','describe_volumes',VolumeIds=[v])
except Exception as e:rows.append({'case':'scenario-error','error':{'type':type(e).__name__,'message':str(e)}})
finally:
 # Terminate first so volume cleanup is independent of attachment behavior.
 for op,kw in sorted(cleanup,key=lambda x:0 if x[0]=='terminate_instances' else 1):call('cleanup-'+op,op,**kw)
print(json.dumps(rows,default=str))

'''


def main():
    name = 'localcloud-rea-storage-runtime-' + uuid.uuid4().hex[:10]
    env = dict(os.environ, LOCALSTACK_AUTH_TOKEN=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
    baseline_networks = json.loads(subprocess.check_output(['docker', 'network', 'ls', '--filter', 'name=^localstack-ec2-link-local$', '--format', '{{json .}}']).decode().strip() or 'null')
    report = {'reference_image': IMAGE, 'guest_image': GUEST_IMAGE, 'reference_container': name,
              'baseline_link_local_network': baseline_networks,
              'scope_limits': ['One normally licensed Docker guest, two EBS API volumes and one snapshot.',
                               'A successful API encryption flag does not establish at-rest encryption.',
                               'Snapshot byte restoration, root mappings at launch, crash consistency and raw block I/O were not tested.',
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
            if row['case'] == 'launch-no-ingress' and row.get('result', {}).get('Instances'):
                instance_id = row['result']['Instances'][0]['InstanceId']
                report['captured_guest_containers_remaining'] = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=localstack-ec2.'+instance_id]).decode().split()
        # The link-local network is globally shared by LocalStack. Preserve a preexisting one.
        report['link_local_network_cleanup'] = 'preexisting shared network preserved' if baseline_networks else 'not removed without exclusive ownership label'
    (ROOT/'docs/research/rea/ec2-storage-runtime-reference.json').write_text(json.dumps(report, indent=2)+'\n')
    rows = {row['case']: row for row in report.get('records', [])}
    baseline = rows.get('before-attach', {})
    comparisons = {key: {
        'same_container': rows[key]['container_id'] == baseline.get('container_id'),
        'same_mounts': rows[key]['mounts'] == baseline.get('mounts'),
        'same_guest_output': rows[key]['guest_output'] == baseline.get('guest_output'),
        'root_marker_present': rows[key]['guest_output'].endswith('root-survives\n'),
    } for key in ('after-attach', 'after-restart', 'after-detach') if key in rows}
    errors = [row for row in rows.values() if row['case'] == 'scenario-error' or row.get('status', 200) != 200]
    print(json.dumps({'exit': report.get('exit'), 'comparisons': comparisons,
                      'errors': errors, 'reference_removed': report.get('reference_container_removed'),
                      'guest_containers_remaining': report.get('captured_guest_containers_remaining')}))
    return 0 if report.get('exit') == 0 and not errors and not report.get('captured_guest_containers_remaining') else 1



if __name__ == '__main__':
    raise SystemExit(main())
