"""Isolated real-packet validation of LocalCloud's opt-in Docker EC2 runtime.

No host firewall/route is modified. Guest namespace helpers own enforcement.
Only fixture containers/private IPs, a fixture host HTTP server and allocated
loopback EIP endpoints are contacted. Existing networks/images are preserved.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def command(*args, input=None, timeout=40, check=True):
    result=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
    if check and result.returncode:raise RuntimeError((result.stderr or result.stdout)[-2000:])
    return result.stdout if check else result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def wait(callback,seconds=20):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            last=callback()
            if last:return last
        except Exception as error:last=error
        time.sleep(.2)
    raise RuntimeError('Readiness deadline exceeded: '+str(last)[-2000:])


WEB=r'''
import http.server,pathlib,socket,threading
PORT=__PORT__
def name():
 try:return pathlib.Path('/tmp/packet-name').read_text().strip()
 except OSError:return 'starting'
class H(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data=name().encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
def udp():
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',PORT+1))
 while True:
  data,peer=s.recvfrom(1000);s.sendto(('udp-'+name()).encode(),peer)
threading.Thread(target=udp,daemon=True).start()
http.server.ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
'''


class External(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        data=b'owned-external-server';self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def main():
    from localcloud.engine import Engine,Error
    suffix=uuid.uuid4().hex[:10]
    guest_image='localcloud-packet-guest:rea-'+suffix
    helper_image='localcloud-packet-helper:rea-'+suffix
    dependency=os.environ.get('LOCALCLOUD_PACKET_DEPENDENCIES') or (json.loads(Path('/tmp/rea-packet-deps.json').read_text())['tag'] if Path('/tmp/rea-packet-deps.json').exists() else 'localcloud:ec2-network')
    app_port=free_port()
    while app_port>65000:app_port=free_port()
    report={'passed':False,'checks':[],'guest_image':guest_image,'helper_image':helper_image,'dependency_image':dependency,'app_port':app_port,'scope':'Owned Docker namespace filters, TCP/UDP, owned external HTTP destination and loopback EIP proxy; no host firewall/routes'}
    report['source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('localcloud/ec2_packet_runtime.py','localcloud/ec2_packet_policy.py','localcloud/ec2_packet_helper.py','localcloud/ec2.py','localcloud/engine.py')}
    engine=None;server=None;thread=None;images=[];instances=[];resources=[]
    def checked(label):report['checks'].append(label)
    with tempfile.TemporaryDirectory(prefix='localcloud-packet-live-') as directory:
        root=Path(directory);state=root/'state.db'
        try:
            (root/'web.py').write_text(WEB.replace('__PORT__',str(app_port)))
            (root/'Dockerfile.guest').write_text('FROM localcloud-ec2-web:dev\nCOPY web.py /fixture-web.py\nCMD ["python3","/fixture-web.py"]\n')
            command('docker','build','--network','none','--pull=false','-f',str(root/'Dockerfile.guest'),'-t',guest_image,str(root),timeout=90);images.append(guest_image)
            (root/'Dockerfile.helper').write_text('FROM '+dependency+'\nUSER root\nCOPY localcloud /app/localcloud\nUSER 10001:10001\n')
            command('docker','build','--network','none','--pull=false','-f',str(root/'Dockerfile.helper'),'-t',helper_image,str(ROOT),timeout=90);images.append(helper_image)
            report['helper_identity']=json.loads(command('docker','image','inspect',helper_image))[0]['Id']
            external_gateway=next(item['Gateway'] for item in json.loads(command('docker','network','inspect','bridge'))[0]['IPAM']['Config'] if ':' not in item.get('Gateway',''))
            server=ThreadingHTTPServer(('0.0.0.0',0),External);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            external_port=server.server_port
            def open_engine():
                value=Engine(str(state),ec2_images={'ami-web':guest_image})
                value.ec2.runtime.helper_image=helper_image
                value.ec2.packet_runtime.enabled=True
                return value
            engine=open_engine()
            def ec2(op,**args):return engine.ec2.call(op,args,'test')
            # Random /24 in a reserved fixture prefix; Docker refuses overlap.
            octet=32+int(suffix[:2],16)%190
            vpc=ec2('CreateVpc',CidrBlock='10.253.'+str(octet)+'.0/24')['Vpc']['VpcId'];resources.append(('DeleteVpc',{'VpcId':vpc}))
            subnet=ec2('CreateSubnet',VpcId=vpc,CidrBlock='10.253.'+str(octet)+'.0/25')['Subnet']['SubnetId'];resources.append(('DeleteSubnet',{'SubnetId':subnet}))
            groups={}
            for label in ('source','target'):
                groups[label]=ec2('CreateSecurityGroup',VpcId=vpc,GroupName='packet-'+label,Description='Isolated packet fixture')['GroupId'];resources.append(('DeleteSecurityGroup',{'GroupId':groups[label]}))
            # Docker can leave a stopped container behind when its publish
            # operation fails after create. Only the nonce-owned launch helper
            # may be removed during rollback; preexisting runtime objects stay.
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1',0));occupied.listen(1)
                occupied_port=occupied.getsockname()[1]
                before_helpers=command('docker','ps','-aq','--filter','label=localcloud.runtime='+engine.ec2.runtime.owner).split()
                try:
                    ec2('RunInstances',ImageId='ami-web',Name='packet-port-conflict',SubnetId=subnet,SecurityGroupIds=[groups['target']],PortMappings=[{'HostPort':occupied_port,'ContainerPort':app_port}])
                except Error as failure:
                    assert failure.code in ('RuntimeFailure','RuntimeUnavailable'),failure.code
                else:
                    raise AssertionError('Occupied host port unexpectedly launched')
                after_helpers=command('docker','ps','-aq','--filter','label=localcloud.runtime='+engine.ec2.runtime.owner).split()
                assert sorted(after_helpers)==sorted(before_helpers),after_helpers
                assert not ec2('DescribeInstances')['Reservations']
            checked('occupied-host-port-launch-fails-without-orphan-namespace-helper')
            def launch(label,subnet_id=subnet):
                item=ec2('RunInstances',ImageId='ami-web',Name='packet-'+label,SubnetId=subnet_id,SecurityGroupIds=[groups[label]],PortMappings=[{'HostPort':free_port(),'ContainerPort':app_port}],UserData=base64.b64encode(('printf '+label+' >/tmp/packet-name').encode()).decode())['Instances'][0]
                instances.append(item['InstanceId']);return item
            source=launch('source');target=launch('target')
            source_id,target_id=source['InstanceId'],target['InstanceId']
            report['instances']=[{'InstanceId':i['InstanceId'],'PrivateIpAddress':i['PrivateIpAddress']} for i in (source,target)]
            def peer(address,port=app_port,udp=False):
                if udp:
                    code="import socket,json;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(.6)\ntry:\n s.sendto(b'probe',(%r,%d));print(json.dumps({'reachable':True,'body':s.recv(100).decode()}))\nexcept Exception as e:print(json.dumps({'reachable':False,'error':type(e).__name__}))"%(address,port)
                else:
                    code="import urllib.request,json;op=urllib.request.build_opener(urllib.request.ProxyHandler({}))\ntry:\n r=op.open('http://%s:%d',timeout=.6);print(json.dumps({'reachable':True,'status':r.status,'body':r.read(100).decode()}))\nexcept Exception as e:print(json.dumps({'reachable':False,'error':type(e).__name__}))"%(address,port)
                return json.loads(command('docker','exec','localcloud-'+source_id,'python3','-c',code,timeout=8))
            def host_eip(ip):
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with opener.open('http://'+ip+':'+str(app_port),timeout=.8) as response:return {'reachable':True,'body':response.read(100).decode()}
                except (OSError,urllib.error.URLError):return {'reachable':False}
            def ingress(permission,allow=True):return ec2('AuthorizeSecurityGroupIngress' if allow else 'RevokeSecurityGroupIngress',GroupId=groups['target'],IpPermissions=[permission])
            def rule(protocol='tcp',port=app_port,cidr=None,group=None):
                return {'IpProtocol':protocol,'FromPort':port,'ToPort':port,**({'IpRanges':[{'CidrIp':cidr}]} if cidr else {'UserIdGroupPairs':[{'GroupId':group}]})}
            cidr=rule(cidr=source['PrivateIpAddress']+'/32')
            assert not peer(target['PrivateIpAddress'])['reachable'];checked('default-security-group-ingress-denies-real-peer-tcp')
            ingress(cidr);assert wait(lambda:peer(target['PrivateIpAddress']) if peer(target['PrivateIpAddress']).get('body')=='target' else None)['body']=='target';checked('cidr-ingress-permit-enables-real-peer-tcp')
            ingress(cidr,False);assert not peer(target['PrivateIpAddress'])['reachable'];checked('live-ingress-revoke-blocks-new-tcp-connections')
            group_rule=rule(group=groups['source']);ingress(group_rule);assert peer(target['PrivateIpAddress']).get('body')=='target';ingress(group_rule,False);assert not peer(target['PrivateIpAddress'])['reachable'];checked('security-group-peer-reference-controls-real-packets')
            udp_rule=rule('udp',app_port+1,cidr=source['PrivateIpAddress']+'/32')
            assert not peer(target['PrivateIpAddress'],app_port+1,True)['reachable'];ingress(udp_rule);assert peer(target['PrivateIpAddress'],app_port+1,True).get('body')=='udp-target';ingress(udp_rule,False);assert not peer(target['PrivateIpAddress'],app_port+1,True)['reachable'];checked('udp-ingress-allow-and-revoke-affect-real-datagrams')
            ingress(cidr)
            all_egress={'IpProtocol':'-1','IpRanges':[{'CidrIp':'0.0.0.0/0'}]}
            ec2('RevokeSecurityGroupEgress',GroupId=groups['source'],IpPermissions=[all_egress]);assert not peer(target['PrivateIpAddress'])['reachable'];checked('empty-egress-rules-block-new-peer-connections')
            egress=rule(cidr=target['PrivateIpAddress']+'/32');ec2('AuthorizeSecurityGroupEgress',GroupId=groups['source'],IpPermissions=[egress]);assert peer(target['PrivateIpAddress']).get('body')=='target';checked('specific-egress-rule-restores-peer-access')
            ec2('AuthorizeSecurityGroupEgress',GroupId=groups['source'],IpPermissions=[all_egress])
            table=ec2('CreateRouteTable',VpcId=vpc)['RouteTable']['RouteTableId'];resources.append(('DeleteRouteTable',{'RouteTableId':table}))
            assoc=ec2('AssociateRouteTable',RouteTableId=table,SubnetId=subnet)['AssociationId'];resources.append(('DisassociateRouteTable',{'AssociationId':assoc}))
            assert not peer(external_gateway,external_port)['reachable'];checked('local-only-subnet-route-blocks-owned-external-destination')
            gateway=ec2('CreateInternetGateway')['InternetGateway']['InternetGatewayId'];resources.append(('DeleteInternetGateway',{'InternetGatewayId':gateway}));ec2('AttachInternetGateway',InternetGatewayId=gateway,VpcId=vpc);resources.append(('DetachInternetGateway',{'InternetGatewayId':gateway,'VpcId':vpc}))
            ec2('CreateRoute',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0',GatewayId=gateway);assert peer(external_gateway,external_port).get('body')=='owned-external-server';checked('attached-igw-default-route-enables-owned-external-destination')
            ec2('DeleteRoute',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0');assert not peer(external_gateway,external_port)['reachable'];checked('default-route-deletion-blocks-new-external-connections')
            ec2('CreateRoute',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0',GatewayId=gateway)
            host_rule=rule(cidr='127.0.0.1/32');ingress(host_rule)
            allocation=ec2('AllocateAddress',Domain='vpc');resources.append(('ReleaseAddress',{'AllocationId':allocation['AllocationId']}))
            association=ec2('AssociateAddress',AllocationId=allocation['AllocationId'],InstanceId=target_id)['AssociationId']
            assert wait(lambda:host_eip(allocation['PublicIp']) if host_eip(allocation['PublicIp']).get('body')=='target' else None).get('body')=='target';checked('elastic-ip-forwards-real-http-to-associated-instance')
            ingress(host_rule,False);assert not host_eip(allocation['PublicIp'])['reachable'];ingress(host_rule);assert host_eip(allocation['PublicIp']).get('body')=='target';checked('elastic-ip-forwarding-checks-original-client-security-group')
            ec2('DeleteRoute',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0');assert not host_eip(allocation['PublicIp'])['reachable'];ec2('CreateRoute',RouteTableId=table,DestinationCidrBlock='0.0.0.0/0',GatewayId=gateway);assert host_eip(allocation['PublicIp']).get('body')=='target';checked('elastic-ip-forwarding-requires-current-internet-gateway-route')
            ec2('AuthorizeSecurityGroupIngress',GroupId=groups['source'],IpPermissions=[host_rule])
            association=ec2('AssociateAddress',AllocationId=allocation['AllocationId'],InstanceId=source_id,AllowReassociation=True)['AssociationId'];assert host_eip(allocation['PublicIp']).get('body')=='source';checked('elastic-ip-reassociation-changes-real-forwarding-target')
            ec2('StopInstances',InstanceIds=[source_id]);assert not host_eip(allocation['PublicIp'])['reachable'];ec2('StartInstances',InstanceIds=[source_id]);command('docker','exec','localcloud-'+source_id,'python3','-c',"from pathlib import Path;Path('/tmp/packet-name').write_text('source')");assert wait(lambda:host_eip(allocation['PublicIp']) if host_eip(allocation['PublicIp']).get('body')=='source' else None).get('body')=='source';checked('stop-start-restores-owned-network-enforcement-and-eip')
            engine.close();engine=None;engine=open_engine();ec2('DescribeInstances');assert host_eip(allocation['PublicIp']).get('body')=='source';checked('backend-restart-reconciles-existing-network-state')
            # Simulate a same-name foreign namespace holder. The runtime must
            # reject it without adopting or deleting the foreign object.
            holder='localcloud-net-'+source_id; saved=holder+'-saved-'+suffix
            command('docker','rename',holder,saved)
            foreign_id=None
            try:
                foreign_id=command('docker','run','-d','--name',holder,'--label','localcloud.audit=foreign-fixture-'+suffix,'--network','none','--read-only','--cap-drop','ALL','--entrypoint','python3',helper_image,'-c','import time;time.sleep(90)').strip()
                assert not host_eip(allocation['PublicIp'])['reachable']
                foreign=json.loads(command('docker','inspect',holder))[0]
                assert foreign['Id']==foreign_id and foreign['State']['Running']
            finally:
                if foreign_id:
                    foreign=json.loads(command('docker','inspect',holder))[0]
                    assert foreign['Id']==foreign_id and foreign['Config']['Labels'].get('localcloud.audit')=='foreign-fixture-'+suffix
                    command('docker','rm','-f',holder)
                command('docker','rename',saved,holder)
            assert host_eip(allocation['PublicIp']).get('body')=='source';checked('foreign-same-name-runtime-holder-is-not-adopted-or-deleted')
            ec2('DisassociateAddress',AssociationId=association);assert not host_eip(allocation['PublicIp'])['reachable'];checked('elastic-ip-disassociation-removes-real-forwarding')
            report['passed']=True
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)
            report['failure_runtime']=[]
            for identifier in instances:
                result=command('docker','inspect','--format','{{json .State}}','localcloud-'+identifier,check=False)
                report['failure_runtime'].append({'instance':identifier,'state':result.stdout[-1500:]})
        finally:
            errors=[]
            if engine:
                for identifier in reversed(instances):
                    try:engine.ec2.call('TerminateInstances',{'InstanceIds':[identifier]},'test')
                    except Exception as error:errors.append('TerminateInstances: '+str(error))
                for action,args in reversed(resources):
                    try:engine.ec2.call(action,args,'test')
                    except Exception as error:errors.append(action+': '+str(error))
                engine.close()
            if server:server.shutdown();server.server_close();thread.join(timeout=3)
            for image in reversed(images):
                result=command('docker','image','rm',image,check=False)
                if result.returncode:errors.append('ImageCleanup: '+result.stderr[-300:])
            report['cleanup_errors']=errors;report['cleanup_completed']=not errors
            report['guest_containers_remaining']=[identifier for identifier in instances if command('docker','container','inspect','localcloud-'+identifier,check=False).returncode==0]
    (ROOT/'docs/research/rea/ec2-packet-live-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report));return 0 if report['passed'] and report['cleanup_completed'] and not report['guest_containers_remaining'] else 1

if __name__=='__main__':raise SystemExit(main())
