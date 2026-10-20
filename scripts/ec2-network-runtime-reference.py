"""Observe networking on isolated, normally licensed LocalStack Docker EC2 guests.

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
import boto3, docker, ipaddress, json, socket, time, urllib.request, uuid
from botocore.config import Config
client=boto3.client('ec2',endpoint_url='http://127.0.0.1:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(retries={'max_attempts':0},read_timeout=60))
dc=docker.from_env()
rows=[]; cleanup=[]; guest=None
def call(label,operation,**args):
    try:
        result=getattr(client,operation)(**args);meta=result.pop('ResponseMetadata',{})
        rows.append({'case':label,'operation':operation,'parameters':args,'status':meta.get('HTTPStatusCode'),'result':result})
        return result
    except Exception as error:
        response=getattr(error,'response',{})
        rows.append({'case':label,'operation':operation,'parameters':args,'status':response.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':response.get('Error',{'type':type(error).__name__,'message':str(error)})})
        return {}
def exec_guest(command):
    r=guest.exec_run(command,user='root')
    return {'exit':r.exit_code,'output':r.output.decode(errors='replace')[:16000]}
def inspect_guest(label):
    guest.reload();net=guest.attrs['NetworkSettings']
    rows.append({'case':label,'docker':{'id':guest.id,'name':guest.name,'state':guest.attrs['State']['Status'],'ports':net.get('Ports'),'networks':{k:{x:v.get(x) for x in ('IPAddress','Gateway','MacAddress','IPPrefixLen','Aliases')} for k,v in net.get('Networks',{}).items()},'host_port_bindings':guest.attrs['HostConfig'].get('PortBindings')},'guest_addresses':exec_guest(['sh','-c','ip -j address 2>/dev/null || hostname -I; ip route 2>/dev/null || true'])})
def connect(label):
    guest.reload()
    probes=[]
    for net in guest.attrs['NetworkSettings'].get('Networks',{}).values():
        ip=net.get('IPAddress')
        if ip:
            try:
                with urllib.request.urlopen('http://'+ip+':8080',timeout=2) as r: probes.append({'address':ip,'reachable':True,'status':r.status,'body':r.read(100).decode(errors='replace')})
            except Exception as e:probes.append({'address':ip,'reachable':False,'error':type(e).__name__+': '+str(e)})
    rows.append({'case':label,'probes':probes})
try:
    vpc=call('create-vpc','create_vpc',CidrBlock='10.97.0.0/16').get('Vpc',{}).get('VpcId')
    if not vpc:raise RuntimeError('No VPC')
    cleanup.append(('delete_vpc',{'VpcId':vpc}))
    subnet=call('create-subnet','create_subnet',VpcId=vpc,CidrBlock='10.97.1.0/24').get('Subnet',{}).get('SubnetId')
    cleanup.append(('delete_subnet',{'SubnetId':subnet}))
    group=call('create-sg','create_security_group',VpcId=vpc,GroupName='rea-dataplane-'+vpc,Description='Isolated runtime probe').get('GroupId')
    cleanup.append(('delete_security_group',{'GroupId':group}))
    before={c.id for c in dc.containers.list(all=True)}
    result=call('launch-no-ingress','run_instances',ImageId='ami-61ad6e59d7b0',MinCount=1,MaxCount=1,InstanceType='t3.micro',SubnetId=subnet,SecurityGroupIds=[group])
    instances=result.get('Instances',[])
    if not instances:raise RuntimeError('No guest launched')
    identifier=instances[0]['InstanceId'];cleanup.append(('terminate_instances',{'InstanceIds':[identifier]}))
    for _ in range(60):
        candidates=[c for c in dc.containers.list(all=True) if c.id not in before and identifier in c.name]
        if candidates:guest=candidates[0];break
        time.sleep(.5)
    if guest is None:
        rows.append({'case':'guest-identification','new_containers':[{'id':c.id,'name':c.name} for c in dc.containers.list(all=True) if c.id not in before]})
        raise RuntimeError('Cannot safely identify created guest')
    rows.append({'case':'guest-tools','tools':exec_guest(['sh','-c','command -v python3; command -v ip; command -v iptables; command -v busybox'])})
    rows.append({'case':'start-guest-web','result':exec_guest(['sh','-c','mkdir -p /tmp/rea-network-web; printf rea-network-ok > /tmp/rea-network-web/index.html; nohup python3 -m http.server 8080 --bind 0.0.0.0 --directory /tmp/rea-network-web >/tmp/rea-network-web.log 2>&1 &'])})
    time.sleep(1)
    inspect_guest('no-ingress-network');connect('no-ingress-reachability')
    permission={'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'IpRanges':[{'CidrIp':'0.0.0.0/0'}]}
    call('allow-ingress','authorize_security_group_ingress',GroupId=group,IpPermissions=[permission])
    inspect_guest('allowed-network');connect('allowed-reachability')
    call('revoke-ingress','revoke_security_group_ingress',GroupId=group,IpPermissions=[permission])
    inspect_guest('revoked-network');connect('revoked-reachability')
    allocation=call('allocate-eip','allocate_address',Domain='vpc')
    if allocation.get('AllocationId'):
        cleanup.append(('release_address',{'AllocationId':allocation['AllocationId']}))
        association=call('associate-eip','associate_address',AllocationId=allocation['AllocationId'],InstanceId=identifier)
        if association.get('AssociationId'):cleanup.append(('disassociate_address',{'AssociationId':association['AssociationId']}))
        call('describe-eip-instance','describe_instances',InstanceIds=[identifier])
        inspect_guest('eip-network');connect('eip-existing-reachability')
        rows.append({'case':'eip-vs-docker-address','allocated_public_ip':allocation.get('PublicIp'),'guest_addresses':exec_guest(['sh','-c','ip -j address 2>/dev/null || hostname -I'])})
        public_ip=allocation.get('PublicIp')
        if public_ip and ipaddress.ip_address(public_ip).is_loopback:
            # A disposable, unprivileged host-network probe makes no route/firewall changes.
            probe_code="""import json,socket,urllib.request
host=%r
result={}
try:
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 with opener.open('http://'+host+':8080',timeout=2) as r:result['http8080']={'reachable':True,'status':r.status,'body':r.read(100).decode(errors='replace')}
except Exception as e:result['http8080']={'reachable':False,'error':type(e).__name__+': '+str(e)}
try:
 with socket.create_connection((host,22),timeout=2) as s:result['ssh22']={'reachable':True,'banner':s.recv(100).decode(errors='replace')}
except Exception as e:result['ssh22']={'reachable':False,'error':type(e).__name__+': '+str(e)}
print(json.dumps(result))
""" % public_ip
            probe_name='localcloud-rea-net-probe-'+uuid.uuid4().hex[:10]
            output=dc.containers.run('localcloud:ec2-runtime',['-c',probe_code],entrypoint='python3',name=probe_name,labels={'localcloud.audit':probe_name},network_mode='host',read_only=True,cap_drop=['ALL'],pids_limit=32,mem_limit='64m',remove=True)
            rows.append({'case':'eip-host-reachability','public_ip':public_ip,'probe_container':probe_name,'result':json.loads(output)})
    interface=call('create-secondary-interface','create_network_interface',SubnetId=subnet,Groups=[group],PrivateIpAddress='10.97.1.50')
    interface_id=interface.get('NetworkInterface',{}).get('NetworkInterfaceId')
    if interface_id:
        cleanup.append(('delete_network_interface',{'NetworkInterfaceId':interface_id}))
        attachment=call('attach-secondary-interface','attach_network_interface',NetworkInterfaceId=interface_id,InstanceId=identifier,DeviceIndex=1)
        if attachment.get('AttachmentId'):cleanup.append(('detach_network_interface',{'AttachmentId':attachment['AttachmentId'],'Force':True}))
        call('describe-secondary-interface','describe_network_interfaces',NetworkInterfaceIds=[interface_id])
        inspect_guest('secondary-interface-network');connect('secondary-interface-existing-reachability')
except Exception as e:
    rows.append({'case':'scenario-error','error':{'type':type(e).__name__,'message':str(e)}})
finally:
    for operation,args in reversed(cleanup):call('cleanup-'+operation,operation,**args)
print(json.dumps(rows,default=str))
'''


def main():
    name = 'localcloud-rea-net-runtime-' + uuid.uuid4().hex[:10]
    env = dict(os.environ, LOCALSTACK_AUTH_TOKEN=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
    baseline_networks = json.loads(subprocess.check_output(['docker', 'network', 'ls', '--filter', 'name=^localstack-ec2-link-local$', '--format', '{{json .}}']).decode().strip() or 'null')
    report = {'reference_image': IMAGE, 'guest_image': GUEST_IMAGE, 'reference_container': name,
              'baseline_link_local_network': baseline_networks,
              'scope_limits': ['One normally licensed Docker guest and one secondary ENI in one VPC.',
                               'Direct bridge reachability does not establish every host publishing path.',
                               'IPv6, routing, egress filters, custom port mappings and multi-guest EIP uniqueness were not tested.',
                               'No encrypted provider code was inspected; no host firewall or route was modified.']}
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
    by_case = {row['case']: row for row in report.get('records', [])}
    baseline = by_case.get('no-ingress-network', {})
    report['observed_comparisons'] = {
        'allow_ingress_kept_docker_networking': by_case.get('allowed-network', {}).get('docker') == baseline.get('docker'),
        'revoke_ingress_kept_docker_networking': by_case.get('revoked-network', {}).get('docker') == baseline.get('docker'),
        'eip_association_kept_docker_networking': by_case.get('eip-network', {}).get('docker') == baseline.get('docker'),
        'secondary_eni_attachment_kept_docker_networking': by_case.get('secondary-interface-network', {}).get('docker') == baseline.get('docker'),
        'secondary_eni_attachment_kept_guest_addresses': by_case.get('secondary-interface-network', {}).get('guest_addresses') == baseline.get('guest_addresses'),
    } if baseline else {}
    (ROOT/'docs/research/rea/ec2-network-runtime-reference.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'exit': report.get('exit'), 'cases': [{'case': r['case'], 'status': r.get('status'), 'error': r.get('error'), **{k: r[k] for k in ('docker','probes','tools','result','guest_addresses','allocated_public_ip') if k in r and 'operation' not in r}} for r in report.get('records', [])], 'stderr': report.get('stderr'), 'reference_container_removed': report.get('reference_container_removed')}))


if __name__ == '__main__':
    main()
