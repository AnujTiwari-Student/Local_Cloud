import pathlib,json,tempfile,importlib.util,subprocess,urllib.request,time,base64
root=pathlib.Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('audit',root/'scripts/rea-runtime-audit.py');a=importlib.util.module_from_spec(s);s.loader.exec_module(a)
report={'passed':False,'checks':[]}
with tempfile.TemporaryDirectory() as d:
 with a.backend(pathlib.Path(d)/'db',images={'ami-web':'localcloud-ec2-web:dev'}) as (engine,client,token):
  engine.ec2.runtime.helper_image='localcloud:ec2-runtime';engine.ec2.runtime.imds=True
  ids={};snap=None;extra=None
  def call(action,**kw): return client.request('POST','/_local/ec2',{'Action':action,**kw})
  try:
   ids['vpc']=call('CreateVpc',CidrBlock='10.91.0.0/16')['Vpc']['VpcId']
   ids['subnet']=call('CreateSubnet',VpcId=ids['vpc'],CidrBlock='10.91.1.0/24')['Subnet']['SubnetId']
   ids['group']=call('CreateSecurityGroup',VpcId=ids['vpc'],GroupName='web',Description='REA isolated web')['GroupId']
   call('AuthorizeSecurityGroupIngress',GroupId=ids['group'],IpPermissions=[{'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'IpRanges':[{'CidrIp':'127.0.0.1/32'}]}])
   launched=call('RunInstances',ImageId='ami-web',SubnetId=ids['subnet'],SecurityGroupIds=[ids['group']],BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1,'DeleteOnTermination':False}}])['Instances'][0]
   ids['instance']=launched['InstanceId'];ids['volume']=launched['BlockDeviceMappings'][0]['Ebs']['VolumeId']
   name='localcloud-'+ids['instance']
   def guest(code): return subprocess.run(['docker','exec',name,'python3','-c',code],capture_output=True,text=True,check=True,timeout=15).stdout.strip()
   got=guest("import urllib.request;print(urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://169.254.169.254/latest/meta-data/instance-id').read().decode())")
   assert got==ids['instance']
   report['checks'].append('standard-link-local-imdsv1-from-guest')
   port=launched['PortMappings'][0]['HostPort']
   for _ in range(30):
    try:
     body=urllib.request.urlopen('http://127.0.0.1:'+str(port),timeout=1).read();break
    except OSError:time.sleep(.1)
   assert b'Hello' in body
   report['checks'].append('vpc-bridge-sg-ingress-loopback-web-endpoint')
   guest("open('/data/marker','w').write('persistent-volume')")
   call('StopInstances',InstanceIds=[ids['instance']])
   snap=call('CreateSnapshot',VolumeId=ids['volume'])['SnapshotId']
   extra=call('CreateVolume',AvailabilityZone='us-east-1a',SnapshotId=snap)['VolumeId']
   call('DetachVolume',VolumeId=ids['volume'])
   call('AttachVolume',VolumeId=extra,InstanceId=ids['instance'],Device='/data')
   call('StartInstances',InstanceIds=[ids['instance']])
   assert guest("print(open('/data/marker').read())")=='persistent-volume'
   report['checks'].append('real-volume-snapshot-copy-stopped-detach-attach-recreate')
   call('ModifyInstanceMetadataOptions',InstanceIds=[ids['instance']],HttpTokens='required')
   result=guest("import urllib.request,urllib.error;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));u='http://169.254.169.254/latest/';t=op.open(urllib.request.Request(u+'api/token',data=b'',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})).read().decode();print(op.open(urllib.request.Request(u+'meta-data/instance-id',headers={'X-aws-ec2-metadata-token':t})).read().decode())")
   assert result==ids['instance']
   report['checks'].append('imdsv2-token-after-metadata-options-update')
   report['passed']=True
  except Exception as e:
   report['error']=type(e).__name__+': '+str(e)
  finally:
   try:
    if ids.get('instance'):call('TerminateInstances',InstanceIds=[ids['instance']])
    for key in filter(None,[ids.get('volume'),extra]):
     call('DeleteVolume',VolumeId=key)
    if snap:call('DeleteSnapshot',SnapshotId=snap)
    if ids.get('group'):call('DeleteSecurityGroup',GroupId=ids['group'])
    if ids.get('subnet'):call('DeleteSubnet',SubnetId=ids['subnet'])
    if ids.get('vpc'):call('DeleteVpc',VpcId=ids['vpc'])
    report['cleanup_completed']=True
   except Exception as e:report['cleanup_error']=str(e)

print(json.dumps(report,indent=2))
raise SystemExit(0 if report['passed'] and report.get('cleanup_completed') else 1)
