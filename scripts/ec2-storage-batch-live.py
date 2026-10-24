"""Real Docker batch-copy pause window and interrupted-batch recovery checks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit',ROOT/'scripts/rea-runtime-audit.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
IMAGES={'ami-web':'localcloud-ec2-web:dev'}
HELPER='localcloud:ec2-runtime'

def main():
    report={'passed':False,'checks':[]};snapshots=[];volumes=[];instance=None;owner=None
    with tempfile.TemporaryDirectory(prefix='localcloud-batch-live-') as temporary:
        path=Path(temporary)/'state.db'
        try:
            with audit.backend(path,images=IMAGES) as (engine,client,token):
                engine.ec2.runtime.helper_image=HELPER
                def call(op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                launched=call('RunInstances',ImageId='ami-web',BlockDeviceMappings=[{'DeviceName':device,'Ebs':{'VolumeSize':1,'DeleteOnTermination':False}} for device in ('/data','/mnt/second')])['Instances'][0]
                instance=launched['InstanceId'];volumes=[x['Ebs']['VolumeId'] for x in launched['BlockDeviceMappings']];owner=engine.ec2.runtime.owner
                subprocess.run(['docker','exec','localcloud-'+instance,'python3','-c',"open('/data/marker','w').write('first');open('/mnt/second/marker','w').write('second')"],check=True,capture_output=True,timeout=15)
                events=[];original=engine.ec2.docker
                def observed(*args):
                    if args[0] in ('pause','unpause'):events.append(args[0])
                    if args[0]=='run' and any('target=/source,readonly' in a for a in args):events.append('copy')
                    return original(*args)
                engine.ec2.docker=observed
                try:batch=call('CreateSnapshots',InstanceSpecification={'InstanceId':instance})['Snapshots']
                finally:engine.ec2.docker=original
                snapshots.extend(x['SnapshotId'] for x in batch)
                assert events==['pause','copy','copy','unpause'],events
                report['checks'].append('one-pause-window-across-both-volume-copies')
                for entry,expected in zip(batch,('first','second')):
                    name='localcloud-batch-reader-'+uuid.uuid4().hex[:10]
                    output=subprocess.check_output(['docker','run','--rm','--name',name,'--label','localcloud.audit='+name,'--network','none','--read-only','--user','65534:65534','--cap-drop','ALL','--mount','type=volume,source='+engine.ec2.runtime.volume_name(entry['SnapshotId'])+',target=/source,readonly','--entrypoint','python3',HELPER,'-c',"print(open('/source/marker').read())"],text=True,timeout=20)
                    assert output.strip()==expected
                report['checks'].append('both-snapshots-contain-real-volume-file-bytes')
                subprocess.run(['docker','pause','localcloud-'+instance],capture_output=True,check=True,timeout=10)
                try:
                    batch=call('CreateSnapshots',InstanceSpecification={'InstanceId':instance})['Snapshots'];snapshots.extend(x['SnapshotId'] for x in batch)
                    assert subprocess.check_output(['docker','inspect','--format','{{.State.Paused}}','localcloud-'+instance],text=True).strip()=='true'
                finally:subprocess.run(['docker','unpause','localcloud-'+instance],capture_output=True,check=True,timeout=10)
                report['checks'].append('batch-preserves-caller-owned-pause')
            worker=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            thread=None
            try:
                connection=json.loads(worker.stdout.readline());remote=audit.Client(connection['endpoint'],token=connection['token'],timeout=40)
                def request():
                    try:remote.request('POST','/_local/ec2',{'Action':'CreateSnapshots','InstanceSpecification':{'InstanceId':instance}})
                    except Exception:pass
                thread=threading.Thread(target=request,daemon=True);thread.start()
                for _ in range(100):
                    helpers=subprocess.check_output(['docker','ps','-q','--filter','label=localcloud.runtime='+owner,'--filter','name=localcloud-storage-'],text=True).split()
                    if helpers:break
                    time.sleep(.1)
                else:raise RuntimeError('Delayed child copy helper did not start')
                assert subprocess.check_output(['docker','inspect','--format','{{.State.Paused}}','localcloud-'+instance],text=True).strip()=='true'
                worker.kill();worker.wait(timeout=10)
            finally:
                if worker.poll() is None:worker.kill();worker.wait(timeout=10)
                if thread:thread.join(timeout=5)
                for stream in (worker.stdin,worker.stdout,worker.stderr):stream.close()
            with audit.backend(path,images=IMAGES) as (engine,client,token):
                engine.ec2.runtime.helper_image=HELPER
                def call(op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                assert not engine.ec2.runtime.storage_journal.jobs()
                assert subprocess.check_output(['docker','inspect','--format','{{.State.Paused}}','localcloud-'+instance],text=True).strip()=='false'
                for helper in helpers:assert subprocess.run(['docker','inspect',helper],capture_output=True).returncode!=0
                report['checks'].append('interrupted-batch-removes-child-helper-and-resumes-parent-writer')
                actual={x['SnapshotId'] for x in call('DescribeSnapshots')['Snapshots']}
                assert actual==set(snapshots),actual
                report['checks'].append('interrupted-batch-rolls-back-new-snapshots-and-preserves-committed-copies')
                report['passed']=True
        except Exception as error:report['error']=type(error).__name__+': '+str(error)
        finally:
            with audit.backend(path,images=IMAGES) as (engine,client,token):
                engine.ec2.runtime.helper_image=HELPER
                def call(op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                errors=[]
                if instance:
                    try:call('TerminateInstances',InstanceIds=[instance])
                    except Exception as error:errors.append(str(error))
                for snapshot in snapshots:
                    try:call('DeleteSnapshot',SnapshotId=snapshot)
                    except Exception as error:errors.append(str(error))
                for volume in volumes:
                    try:call('DeleteVolume',VolumeId=volume)
                    except Exception as error:errors.append(str(error))
                report['cleanup_completed']=not errors
                if errors:report['cleanup_errors']=errors
                report['pending_storage_operations']=len(engine.ec2.runtime.storage_journal.jobs())
                report['owned_volumes_remaining']=subprocess.check_output(['docker','volume','ls','-q','--filter','label=localcloud.runtime='+engine.ec2.runtime.owner],text=True).split()
                report['owned_helpers_remaining']=subprocess.check_output(['docker','ps','-aq','--filter','label=localcloud.runtime='+engine.ec2.runtime.owner,'--filter','name=localcloud-storage-'],text=True).split()
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] and report['cleanup_completed'] and not report['pending_storage_operations'] and not report['owned_volumes_remaining'] and not report['owned_helpers_remaining'] else 1


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':
        with audit.backend(Path(sys.argv[2]),images=IMAGES) as (engine,client,token):
            engine.ec2.runtime.helper_image=HELPER
            original=engine.ec2.docker
            def delayed(*args):
                args=list(args)
                if args[0]=='run' and '--name' in args and args[args.index('--name')+1].startswith('localcloud-storage-'):
                    i=args.index('-c')+1;args[i]='import time;time.sleep(60);'+args[i]
                return original(*args)
            engine.ec2.docker=delayed
            print(json.dumps({'endpoint':engine.endpoint,'token':token}),flush=True);sys.stdin.read()
    else:raise SystemExit(main())
