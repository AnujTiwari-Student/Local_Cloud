"""Normally licensed packet-level EC2 reference, scoped to owned Docker guests.

Only private Docker addresses and allocated loopback EIPs are probed. No host
routes/firewalls are changed and no external network destination is contacted.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
INNER=r'''
import boto3,docker,json,time,urllib.request,ipaddress,uuid
from botocore.config import Config
c=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=40))
dc=docker.from_env();rows=[];cleanup=[];guests=[]
def call(case,op,**args):
 try:
  r=getattr(c,op)(**args);m=r.pop('ResponseMetadata',{});rows.append({'case':case,'operation':op,'parameters':args,'status':m.get('HTTPStatusCode'),'result':r});return r
 except Exception as e:
  r=getattr(e,'response',{});rows.append({'case':case,'operation':op,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})});return {}
def run(g,cmd):
 r=g.exec_run(cmd,user='root');return {'exit':r.exit_code,'output':r.output.decode(errors='replace')[:5000]}
def addresses(g):
 g.reload();return {'networks':{k:{a:n.get(a) for a in ('IPAddress','Gateway','MacAddress')} for k,n in g.attrs['NetworkSettings']['Networks'].items()},'ports':g.attrs['NetworkSettings'].get('Ports'),'routes':run(g,['sh','-c','ip -j route 2>/dev/null || ip route 2>/dev/null || cat /proc/net/route'])}
def probe(case):
 target=guests[1];target.reload();ips=[n['IPAddress'] for n in target.attrs['NetworkSettings']['Networks'].values() if n.get('IPAddress')]
 result=[]
 for ip in ips:
  assert ipaddress.ip_address(ip).is_private
  code="import urllib.request,json;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));\ntry:\n r=op.open('http://%s:%d',timeout=1);print(json.dumps({'reachable':True,'status':r.status,'body':r.read(100).decode()}))\nexcept Exception as e:print(json.dumps({'reachable':False,'error':type(e).__name__+': '+str(e)}))"%(ip,PORT)
  result.append({'target_docker_ip':ip,'from_peer':run(guests[0],['python3','-c',code])})
 rows.append({'case':case,'probes':result,'target_network':addresses(target),'source_network':addresses(guests[0])})
try:
 vpc=call('create-vpc','create_vpc',CidrBlock='10.98.0.0/16')['Vpc']['VpcId'];cleanup.append(('delete_vpc',{'VpcId':vpc}))
 subnet=call('create-subnet','create_subnet',VpcId=vpc,CidrBlock='10.98.1.0/24')['Subnet']['SubnetId'];cleanup.append(('delete_subnet',{'SubnetId':subnet}))
 group=call('create-security-group','create_security_group',VpcId=vpc,GroupName='packet-'+vpc,Description='Own packet reference')['GroupId'];cleanup.append(('delete_security_group',{'GroupId':group}))
 permission={'IpProtocol':'tcp','FromPort':PORT,'ToPort':PORT,'IpRanges':[{'CidrIp':'0.0.0.0/0'}]}
 for index in (0,1):
  before={g.id for g in dc.containers.list(all=True)}
  r=call('run-'+str(index),'run_instances',ImageId='ami-61ad6e59d7b0',MinCount=1,MaxCount=1,SubnetId=subnet,SecurityGroupIds=[group],InstanceType='t3.micro')['Instances'][0]
  cleanup.append(('terminate_instances',{'InstanceIds':[r['InstanceId']]}))
  for _ in range(60):
   candidates=[g for g in dc.containers.list(all=True) if g.id not in before and r['InstanceId'] in g.name]
   if candidates:break
   time.sleep(.2)
  assert len(candidates)==1,'Unable to identify own guest safely'
  g=candidates[0];guests.append(g)
  rows.append({'case':'guest-'+str(index),'instance_id':r['InstanceId'],'container_id':g.id,'container_name':g.name,'api_private_ip':r.get('PrivateIpAddress'),'actual':addresses(g)})
  if index==1:
   rows.append({'case':'start-http','result':run(g,['sh','-c','mkdir -p /tmp/rea-packet-web; printf own-packet-server >/tmp/rea-packet-web/index.html; nohup python3 -m http.server '+str(PORT)+' --bind 0.0.0.0 --directory /tmp/rea-packet-web >/tmp/rea-packet-web/log 2>&1 &'])})
 time.sleep(.5)
 probe('sg-closed')
 call('sg-ingress-allow','authorize_security_group_ingress',GroupId=group,IpPermissions=[permission]);probe('sg-open')
 call('sg-ingress-revoke','revoke_security_group_ingress',GroupId=group,IpPermissions=[permission]);probe('sg-revoked')
 call('sg-egress-revoke-default','revoke_security_group_egress',GroupId=group,IpPermissions=[{'IpProtocol':'-1','IpRanges':[{'CidrIp':'0.0.0.0/0'}]}]);probe('sg-egress-empty')
 call('sg-egress-allow-port','authorize_security_group_egress',GroupId=group,IpPermissions=[permission]);probe('sg-egress-port-allowed')
 table=call('create-route-table','create_route_table',VpcId=vpc)['RouteTable']['RouteTableId'];cleanup.append(('delete_route_table',{'RouteTableId':table}))
 association=call('associate-route-table','associate_route_table',RouteTableId=table,SubnetId=subnet)['AssociationId'];cleanup.append(('disassociate_route_table',{'AssociationId':association}))
 probe('route-local-only')
 gateway=call('create-igw','create_internet_gateway')['InternetGateway']['InternetGatewayId'];cleanup.append(('delete_internet_gateway',{'InternetGatewayId':gateway}))
 call('attach-igw','attach_internet_gateway',InternetGatewayId=gateway,VpcId=vpc);cleanup.append(('detach_internet_gateway',{'InternetGatewayId':gateway,'VpcId':vpc}))
 call('create-default-route','create_route',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0',GatewayId=gateway)
 probe('route-igw-default')
 call('delete-default-route','delete_route',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0');probe('route-default-deleted')
 alloc=call('allocate-eip','allocate_address',Domain='vpc');cleanup.append(('release_address',{'AllocationId':alloc['AllocationId']}))
 for index in (1,0):
  iid=next(x['instance_id'] for x in rows if x['case']=='guest-'+str(index))
  a=call('associate-eip-'+str(index),'associate_address',AllocationId=alloc['AllocationId'],InstanceId=iid,AllowReassociation=True)
  if a.get('AssociationId'):cleanup.append(('disassociate_address',{'AssociationId':a['AssociationId']}))
  probe('eip-associated-'+str(index))
  ip=alloc.get('PublicIp');rows.append({'case':'allocated-loopback-address','public_ip':ip,'target_instance':iid})
  if ip and ipaddress.ip_address(ip).is_loopback:
   code="import urllib.request,json;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));\ntry:\n r=op.open('http://%s:%d',timeout=1);print(json.dumps({'reachable':True,'status':r.status,'body':r.read(100).decode()}))\nexcept Exception as e:print(json.dumps({'reachable':False,'error':type(e).__name__+': '+str(e)}))"%(ip,PORT)
   name='localcloud-rea-packet-eip-'+uuid.uuid4().hex[:8]
   output=dc.containers.run('localcloud:ec2-runtime',['-c',code],entrypoint='python3',name=name,labels={'localcloud.audit':name},network_mode='host',read_only=True,cap_drop=['ALL'],pids_limit=32,mem_limit='64m',remove=True)
   rows.append({'case':'eip-host-probe-'+str(index),'result':json.loads(output)})
 call('describe-route-table','describe_route_tables',RouteTableIds=[table])
 call('describe-security-group','describe_security_groups',GroupIds=[group])
except Exception as e:rows.append({'case':'scenario-error','error':type(e).__name__+': '+str(e)})
finally:
 for op,args in reversed(cleanup):call('cleanup-'+op,op,**args)
print(json.dumps(rows,default=str))
'''


def main():
 name='localcloud-rea-packet-ref-'+uuid.uuid4().hex[:10]
 env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=(Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
 with socket.socket() as sock:
  sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 baseline_linklocal=subprocess.check_output(['docker','network','ls','--filter','name=^localstack-ec2-link-local$','--format','{{.ID}}']).decode().split()
 report={'baseline_linklocal_networks':baseline_linklocal,'reference_image':IMAGE,'container':name,'probe_port':port,'scope':['Two owned Docker guests in one emulated VPC','Only private Docker IPs and allocated loopback EIPs probed','No host firewall/routes changed; no external destinations contacted'],'records':[]}
 created=False
 try:
  subprocess.run(['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-v','/var/run/docker.sock:/var/run/docker.sock','-e','LOCALSTACK_AUTH_TOKEN','-e','EC2_VM_MANAGER=docker',IMAGE],env=env,capture_output=True,check=True)
  created=True
  info=json.loads(subprocess.check_output(['docker','inspect',name]))[0];public_port=info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
  for _ in range(120):
   try:
    with urllib.request.urlopen('http://127.0.0.1:'+public_port+'/_localstack/health',timeout=1) as response:
     if response.status==200:break
   except OSError:time.sleep(.5)
  else:raise RuntimeError('Reference health deadline exceeded')
  p=subprocess.run(['docker','exec','-i',name,'/opt/code/localstack/.venv/bin/python','-'],input='PORT='+str(port)+'\n'+INNER,capture_output=True,text=True,timeout=260)
  report.update(exit=p.returncode,records=json.loads(p.stdout) if p.returncode==0 else [],stderr=p.stderr[-2000:])
 finally:
  if created:
   info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
   assert info['Config']['Labels']['localcloud.audit']==name
   subprocess.run(['docker','rm','-f','-v',name],capture_output=True,check=True);report['reference_container_removed']=True
  report['guest_containers_remaining']=[]
  for row in report['records']:
   if row['case'].startswith('guest-') and 'container_id' in row:
    remaining=subprocess.run(['docker','container','inspect',row['container_id']],capture_output=True)
    if remaining.returncode==0:report['guest_containers_remaining'].append(row['container_id'])
 by_case={r['case']:r for r in report['records']}
 baseline=by_case.get('sg-closed',{})
 report['network_comparisons']={key:by_case.get(key,{}).get('target_network')==baseline.get('target_network') and by_case.get(key,{}).get('source_network')==baseline.get('source_network') for key in ('sg-open','sg-revoked','sg-egress-empty','sg-egress-port-allowed','route-local-only','route-igw-default','route-default-deleted','eip-associated-1','eip-associated-0')}
 report['shared_linklocal_network']='preexisting preserved' if baseline_linklocal else 'shared reference network not removed without exclusive ownership'
 target=ROOT/'docs/research/rea/ec2-packet-reference-report.json';target.write_text(json.dumps(report,indent=2)+'\n')
 summary={'exit':report.get('exit'),'reference_container_removed':report.get('reference_container_removed'),'guest_containers_remaining':report['guest_containers_remaining'],'cases':[{'case':r['case'],**{k:r[k] for k in ('status','error','probes','result') if k in r and (k!='result' or not r.get('operation'))}} for r in report['records']]}
 print(json.dumps(summary))
 return 0 if report.get('exit')==0 and not report['guest_containers_remaining'] and not any(r['case']=='scenario-error' for r in report['records']) else 1

if __name__=='__main__':raise SystemExit(main())
