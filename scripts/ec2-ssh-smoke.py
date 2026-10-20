"""Live SSH and writable guest root preservation across volume reattachment."""
import importlib.util,json,pathlib,subprocess,tempfile,time,socket
root=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit',root/'scripts/rea-runtime-audit.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
report={'passed':False,'checks':[]}
with tempfile.TemporaryDirectory(prefix='localcloud-ssh-') as d:
 with a.backend(pathlib.Path(d)/'db',images={'ami-guest':'localstack-ec2/ubuntu-26.04-resolute-raccoon:ami-61ad6e59d7b0'}) as (engine,client,token):
  engine.ec2.runtime.guest_mode=True
  engine.ec2.runtime.helper_image='localcloud:ec2-runtime'
  instance=None;volume=None;key_created=False
  def call(action,**args):return client.request('POST','/_local/ec2',{'Action':action,**args})
  try:
   key=call('CreateKeyPair',KeyName='ssh-probe',KeyType='ed25519');key_created=True
   private=pathlib.Path(d)/'private';private.touch(mode=0o600);private.write_text(key['KeyMaterial'])
   with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
   instance=call('RunInstances',ImageId='ami-guest',KeyName='ssh-probe',PortMappings=[{'HostPort':port,'ContainerPort':22}])['Instances'][0]
   identifier=instance['InstanceId']
   def ssh(command):
    current=call('DescribeInstances',InstanceIds=[identifier])['Reservations'][0]['Instances'][0]
    port=current['PortMappings'][0]['HostPort']
    last=None
    for _ in range(30):
     last=subprocess.run(['ssh','-F','/dev/null','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null','-o','IdentitiesOnly=yes','-o','IdentityAgent=none','-o','ConnectTimeout=2','-i',str(private),'-p',str(port),'root@127.0.0.1',command],capture_output=True,text=True,timeout=10)
     if last.returncode==0:return last.stdout.strip()
     time.sleep(.2)
    raise AssertionError('SSH failed: '+last.stderr[-500:])
   assert ssh("printf localcloud-root-ok >/root/marker; cat /root/marker")=='localcloud-root-ok'
   report['checks'].append('actual-public-key-ssh')
   call('StopInstances',InstanceIds=[identifier])
   volume=call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)['VolumeId']
   call('AttachVolume',InstanceId=identifier,VolumeId=volume,Device='/data')
   call('StartInstances',InstanceIds=[identifier])
   assert ssh('cat /root/marker')=='localcloud-root-ok'
   assert ssh("printf volume-ok >/data/marker; cat /data/marker")=='volume-ok'
   report['checks'].append('writable-root-preserved-across-volume-attach')
   call('StopInstances',InstanceIds=[identifier])
   call('DetachVolume',VolumeId=volume)
   call('StartInstances',InstanceIds=[identifier])
   assert ssh('cat /root/marker')=='localcloud-root-ok'
   report['checks'].append('writable-root-preserved-across-volume-detach')
   report['passed']=True
  except Exception as error:report['error']=type(error).__name__+': '+str(error)
  finally:
   errors=[]
   for action,params in ([('TerminateInstances',{'InstanceIds':[instance['InstanceId']]})] if instance else [])+([('DeleteVolume',{'VolumeId':volume})] if volume else [])+([('DeleteKeyPair',{'KeyName':'ssh-probe'})] if key_created else []):
    try:call(action,**params)
    except Exception as error:errors.append(action+': '+str(error))
   report['cleanup_completed']=not errors
   if errors:report['cleanup_errors']=errors
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['passed'] and report['cleanup_completed'] else 1)
