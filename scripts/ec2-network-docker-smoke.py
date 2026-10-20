"""Exercise network records alongside a real, isolated Docker web workload."""
import importlib.util,json,pathlib,subprocess,tempfile,time,urllib.request
root=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit',root/'scripts/rea-runtime-audit.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
report={'passed':False,'checks':[]};cleanup=[]
with tempfile.TemporaryDirectory() as d:
 with a.backend(pathlib.Path(d)/'db',images={'ami-web':'localcloud-ec2-web:dev'}) as (engine,client,token):
  def call(action,**args):return client.request('POST','/_local/ec2',{'Action':action,**args})
  try:
   vpc=call('CreateVpc',CidrBlock='10.83.0.0/16')['Vpc']['VpcId'];cleanup.append(('DeleteVpc',{'VpcId':vpc}))
   subnet=call('CreateSubnet',VpcId=vpc,CidrBlock='10.83.1.0/24')['Subnet']['SubnetId'];cleanup.append(('DeleteSubnet',{'SubnetId':subnet}))
   group=call('CreateSecurityGroup',VpcId=vpc,GroupName='network-live',Description='Temporary test')['GroupId'];cleanup.append(('DeleteSecurityGroup',{'GroupId':group}))
   rule={'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'IpRanges':[{'CidrIp':'127.0.0.1/32'}]}
   call('AuthorizeSecurityGroupIngress',GroupId=group,IpPermissions=[rule])
   instance=call('RunInstances',ImageId='ami-web',SubnetId=subnet,SecurityGroupIds=[group])['Instances'][0];key=instance['InstanceId'];cleanup.append(('TerminateInstances',{'InstanceIds':[key]}))
   name='localcloud-'+key
   def network():return json.loads(subprocess.check_output(['docker','inspect','--format','{{json .NetworkSettings.Networks}}',name],text=True))
   baseline=network();port=instance['PortMappings'][0]['HostPort']
   for _ in range(30):
    try:
     assert b'Hello' in urllib.request.urlopen('http://127.0.0.1:'+str(port),timeout=1).read();break
    except OSError:time.sleep(.1)
   else:raise AssertionError('Web endpoint did not start')
   report['checks'].append('real-published-web-endpoint')
   eni=call('CreateNetworkInterface',SubnetId=subnet,Groups=[group])['NetworkInterface']['NetworkInterfaceId'];cleanup.append(('DeleteNetworkInterface',{'NetworkInterfaceId':eni}))
   call('AssignPrivateIpAddresses',NetworkInterfaceId=eni,PrivateIpAddresses=['10.83.1.50'])
   attachment=call('AttachNetworkInterface',NetworkInterfaceId=eni,InstanceId=key,DeviceIndex=1)['AttachmentId'];cleanup.append(('DetachNetworkInterface',{'AttachmentId':attachment}))
   allocation=call('AllocateAddress')['AllocationId'];cleanup.append(('ReleaseAddress',{'AllocationId':allocation}))
   association=call('AssociateAddress',AllocationId=allocation,NetworkInterfaceId=eni,PrivateIpAddress='10.83.1.50')['AssociationId'];cleanup.append(('DisassociateAddress',{'AssociationId':association}))
   assert call('DescribeAddresses',AllocationIds=[allocation])['Addresses'][0]['InstanceId']==key
   assert network()==baseline
   report['checks'].append('docker-eni-secondary-ip-eip-control-plane-with-unchanged-guest-networks')
   call('RevokeSecurityGroupIngress',GroupId=group,IpPermissions=[rule])
   assert b'Hello' in urllib.request.urlopen('http://127.0.0.1:'+str(port),timeout=2).read()
   report['checks'].append('sg-revoke-does-not-pretend-to-install-live-firewall')
   report['passed']=True
  except Exception as error:report['error']=type(error).__name__+': '+str(error)
  finally:
   errors=[]
   for action,args in reversed(cleanup):
    try:call(action,**args)
    except Exception as error:errors.append(action+': '+str(error))
   report['cleanup_completed']=not errors
   if errors:report['cleanup_errors']=errors
(root/'docs/research/rea/ec2-network-docker-live.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report));raise SystemExit(0 if report['passed'] and report['cleanup_completed'] else 1)
