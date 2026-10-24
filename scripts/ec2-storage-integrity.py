"""Exercise LocalCloud file-backed storage, restart persistence and copy isolation.

Uses temporary SQLite state and owned Docker resources, never the saved CLI profile.
Requires cached localcloud-ec2-web:dev and localcloud:ec2-runtime images.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audit', ROOT/'scripts/rea-runtime-audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def main():
    report = {'passed': False, 'checks': []}
    ids = {}; snapshots = []; volumes = []; instances = []
    payload = bytes(range(256))*1024
    expected = hashlib.sha256(payload).hexdigest()
    def passed(name): report['checks'].append(name)
    def guest(instance, code):
        p = subprocess.run(['docker','exec','localcloud-'+instance,'python3','-c',code],capture_output=True,text=True,timeout=20)
        if p.returncode: raise RuntimeError(p.stderr[-1500:])
        return p.stdout.strip()
    with tempfile.TemporaryDirectory(prefix='localcloud-storage-integrity-') as temporary:
        path = Path(temporary)/'state.sqlite3'
        def backend(): return audit.backend(path, images={'ami-web':'localcloud-ec2-web:dev'})
        try:
            with backend() as (engine, client, token):
                engine.ec2.runtime.helper_image = 'localcloud:ec2-runtime'
                def call(op, **args): return client.request('POST','/_local/ec2',{'Action':op,**args})
                mappings = [{'DeviceName':'/data','Ebs':{'VolumeSize':1,'DeleteOnTermination':False}},
                            {'DeviceName':'/mnt/ephemeral','Ebs':{'VolumeSize':1,'DeleteOnTermination':True}}]
                first = call('RunInstances',ImageId='ami-web',BlockDeviceMappings=mappings)['Instances'][0]
                ids['original'] = first['InstanceId']; instances.append(ids['original'])
                ids['retained'],ids['deleted'] = [m['Ebs']['VolumeId'] for m in first['BlockDeviceMappings']]
                volumes.extend([ids['retained'], ids['deleted']])
                encoded = base64.b64encode(payload).decode()
                # Send bytes through stdin rather than a large process argument.
                p = subprocess.run(['docker','exec','-i','localcloud-'+ids['original'],'python3','-c',"import sys,base64,pathlib;pathlib.Path('/data/bytes').write_bytes(base64.b64decode(sys.stdin.buffer.read()));pathlib.Path('/data/link').symlink_to('bytes')"],input=encoded,capture_output=True,text=True,timeout=20)
                if p.returncode: raise RuntimeError(p.stderr[-1500:])
                # Snapshot a live guest: snapshot_volume quiesces its writers during copying.
                ids['snapshot'] = call('CreateSnapshot',VolumeId=ids['retained'])['SnapshotId']; snapshots.append(ids['snapshot'])
                passed('live-guest-snapshot-resumes-instance')
                guest(ids['original'],"open('/data/bytes','wb').write(b'changed-after-snapshot')")
                ids['copy'] = call('CopySnapshot',SourceRegion='us-east-1',SourceSnapshotId=ids['snapshot'])['SnapshotId']; snapshots.append(ids['copy'])
                ids['restored'] = call('CreateVolume',AvailabilityZone='us-east-1a',SnapshotId=ids['copy'])['VolumeId']; volumes.append(ids['restored'])
                second = call('RunInstances',ImageId='ami-web')['Instances'][0]['InstanceId']; ids['restored_instance'] = second; instances.append(second)
                call('StopInstances',InstanceIds=[second])
                call('AttachVolume',VolumeId=ids['restored'],InstanceId=second,Device='/data')
                call('StartInstances',InstanceIds=[second])
                got = guest(second,"import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/data/bytes').read_bytes()).hexdigest());print(pathlib.Path('/data/link').is_symlink())")
                assert got == expected+'\nTrue', got
                passed('binary-snapshot-copy-restore-and-relative-symlink')
                assert guest(ids['original'],"print(open('/data/bytes','rb').read().decode())") == 'changed-after-snapshot'
                passed('snapshot-independent-of-later-source-writes')
                ids['owner'] = engine.ec2.runtime.owner
            # Kill a separate backend after acknowledged committed writes. This tests
            # abrupt server death, not a kill during a copy or an application transaction.
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(path)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                connection = json.loads(worker.stdout.readline())
                remote = audit.Client(connection['endpoint'], token=connection['token'], timeout=30)
                ids['crash_volume'] = remote.request('POST','/_local/ec2',{'Action':'CreateVolume','AvailabilityZone':'us-east-1a','SnapshotId':ids['snapshot']})['VolumeId']
                volumes.append(ids['crash_volume'])
                worker.kill()
                worker.wait(timeout=10)
            finally:
                if worker.poll() is None:worker.kill();worker.wait(timeout=10)
                for stream in (worker.stdin, worker.stdout, worker.stderr):stream.close()
            # Deliberately hold an owned copy helper, then kill its backend.
            # Startup recovery must stop the helper, discard the uncommitted copy
            # and resume the guest whose pause intent was durably recorded.
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(path), '--delay-copy'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            request_errors = []
            copy_thread = None
            try:
                connection = json.loads(worker.stdout.readline())
                remote = audit.Client(connection['endpoint'], token=connection['token'], timeout=45)
                def interrupted_request():
                    try:remote.request('POST','/_local/ec2',{'Action':'CreateSnapshot','VolumeId':ids['retained']})
                    except Exception as error:request_errors.append(type(error).__name__)
                copy_thread = threading.Thread(target=interrupted_request, daemon=True)
                copy_thread.start()
                for _ in range(100):
                    helpers = subprocess.check_output(['docker','ps','-q','--filter','label=localcloud.runtime='+ids['owner'],'--filter','name=localcloud-storage-'],text=True).split()
                    if helpers:break
                    time.sleep(.1)
                else:raise RuntimeError('Delayed copy helper did not start')
                ids['interrupted_helpers'] = helpers
                paused = subprocess.check_output(['docker','inspect','--format','{{.State.Paused}}','localcloud-'+ids['original']],text=True).strip()
                assert paused == 'true', paused
                worker.kill();worker.wait(timeout=10)
            finally:
                if worker.poll() is None:worker.kill();worker.wait(timeout=10)
                if copy_thread is not None:copy_thread.join(timeout=5)
                for stream in (worker.stdin, worker.stdout, worker.stderr):stream.close()
            # Reopen the actual server and SQLite engine, without stopping guest containers.
            with backend() as (engine, client, token):
                engine.ec2.runtime.helper_image = 'localcloud:ec2-runtime'
                def call(op, **args): return client.request('POST','/_local/ec2',{'Action':op,**args})
                assert engine.ec2.runtime.owner == ids['owner']
                for helper in ids['interrupted_helpers']:
                    assert subprocess.run(['docker','inspect',helper],capture_output=True).returncode != 0
                assert subprocess.check_output(['docker','inspect','--format','{{.State.Paused}}','localcloud-'+ids['original']],text=True).strip() == 'false'
                assert len(call('DescribeSnapshots')['Snapshots']) == 2
                passed('killed-mid-copy-backend-recovers-helper-paused-guest-and-uncommitted-copy')
                assert any(v['VolumeId']==ids['crash_volume'] for v in call('DescribeVolumes')['Volumes'])
                passed('acknowledged-storage-write-survives-backend-sigkill')
                described = call('DescribeInstances',InstanceIds=[ids['restored_instance']])['Reservations'][0]['Instances'][0]
                assert described['State']['Name'] == 'running'
                assert guest(ids['restored_instance'],"import hashlib;print(hashlib.sha256(open('/data/bytes','rb').read()).hexdigest())") == expected
                assert any(v['SnapshotId']==ids['copy'] for v in call('DescribeVolumes')['Volumes'])
                passed('server-restart-retains-owner-records-running-guest-and-bytes')
                call('StopInstances',InstanceIds=[ids['restored_instance']])
                call('ModifyVolume',VolumeId=ids['restored'],Size=2)
                call('StartInstances',InstanceIds=[ids['restored_instance']])
                assert guest(ids['restored_instance'],"import hashlib;print(hashlib.sha256(open('/data/bytes','rb').read()).hexdigest())") == expected
                passed('metadata-growth-preserves-files-without-claiming-disk-resize')
                call('TerminateInstances',InstanceIds=[ids['original']])
                remaining={v['VolumeId']:v for v in call('DescribeVolumes')['Volumes']}
                assert ids['deleted'] not in remaining
                assert remaining[ids['retained']]['State']=='available' and not remaining[ids['retained']]['Attachments']
                passed('delete-on-termination-and-retained-volume-detachment')
                # Deletion of snapshot records cannot destroy already restored volume bytes.
                for snap in snapshots[:]:call('DeleteSnapshot',SnapshotId=snap);snapshots.remove(snap)
                assert guest(ids['restored_instance'],"import hashlib;print(hashlib.sha256(open('/data/bytes','rb').read()).hexdigest())") == expected
                passed('restored-volume-independent-of-deleted-snapshots')
                report['passed']=True
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)
        finally:
            with backend() as (engine, client, token):
                engine.ec2.runtime.helper_image='localcloud:ec2-runtime'
                def call(op, **args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                errors=[]
                for instance in instances:
                    try:call('TerminateInstances',InstanceIds=[instance])
                    except Exception as error:errors.append(str(error))
                remaining={v['VolumeId'] for v in call('DescribeVolumes')['Volumes']}
                for volume in volumes:
                    if volume in remaining:
                        try:call('DeleteVolume',VolumeId=volume)
                        except Exception as error:errors.append(str(error))
                for snap in snapshots:
                    try:call('DeleteSnapshot',SnapshotId=snap)
                    except Exception as error:errors.append(str(error))
                report['cleanup_completed']=not errors
                if errors:report['cleanup_errors']=errors
                report['remaining_volumes']=len(call('DescribeVolumes')['Volumes'])
                report['remaining_snapshots']=len(call('DescribeSnapshots')['Snapshots'])
                report['pending_storage_operations']=len(engine.ec2.runtime.storage_journal.jobs())
                report['owned_docker_volumes_remaining']=subprocess.check_output(['docker','volume','ls','-q','--filter','label=localcloud.runtime='+ids.get('owner',engine.ec2.runtime.owner)],text=True).split()
                report['owned_storage_helpers_remaining']=subprocess.check_output(['docker','ps','-aq','--filter','label=localcloud.runtime='+ids.get('owner',engine.ec2.runtime.owner),'--filter','name=localcloud-storage-'],text=True).split()
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] and report.get('cleanup_completed') and not report.get('remaining_volumes') and not report.get('remaining_snapshots') and not report.get('pending_storage_operations') and not report.get('owned_docker_volumes_remaining') and not report.get('owned_storage_helpers_remaining') else 1


if __name__=='__main__':
    if len(sys.argv)>=3 and sys.argv[1]=='--worker':
        with audit.backend(Path(sys.argv[2]), images={'ami-web':'localcloud-ec2-web:dev'}) as (engine, client, token):
            engine.ec2.runtime.helper_image='localcloud:ec2-runtime'
            if '--delay-copy' in sys.argv:
                original_docker = engine.ec2.docker
                def delayed_docker(*args):
                    args = list(args)
                    if args and args[0]=='run' and '--name' in args and args[args.index('--name')+1].startswith('localcloud-storage-'):
                        offset=args.index('-c')+1
                        args[offset]='import time;time.sleep(60);'+args[offset]
                    return original_docker(*args)
                engine.ec2.docker=delayed_docker
            # Parent reads this private pipe; credentials never reach the report.
            print(json.dumps({'endpoint':engine.endpoint,'token':token}),flush=True)
            sys.stdin.read()
    else:raise SystemExit(main())
