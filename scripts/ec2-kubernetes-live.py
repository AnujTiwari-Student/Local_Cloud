"""Isolated EC2 Kubernetes executor integration checks, including real PVC bytes.

Uses explicit kind-localcloud context and an owned namespace. Does not mutate
installed CRDs, user instances, saved profiles, credentials or shared tags.
"""
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
CONTEXT='kind-localcloud'
NODE='localcloud-control-plane'
SOURCE_IMAGE='localcloud-ec2-web:dev'


def command(*args, timeout=30, input=None, check=True):
    p=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:raise RuntimeError((p.stderr or p.stdout)[-2000:])
    return p.stdout if check else p


def kubectl(*args,**kwargs):return command('kubectl','--context',CONTEXT,*args,**kwargs)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def wait(callback,seconds=90):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            last=callback()
            if last:return last
        except Exception as error:last=error
        time.sleep(.3)
    raise RuntimeError('Readiness deadline exceeded: '+str(last)[-1500:])


@contextlib.contextmanager
def backend(path,proxy,namespace,image,helper_image=None):
    from localcloud.engine import Engine
    from localcloud.kube import KubeClient
    from localcloud.client import Client
    from localcloud.__main__ import Handler
    from http.server import ThreadingHTTPServer
    engine=Engine(str(path),ec2_images={'ami-web':image,'ami-missing':'invalid.localcloud.test/missing:never'},ec2_kubernetes={'client':KubeClient(proxy),'namespace':namespace,'helper_image':helper_image,'image_pull_policy':'Never'})
    token=secrets.token_urlsafe(32)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    engine.endpoint='http://127.0.0.1:'+str(server.server_port)
    server.engine,server.management_token=engine,token
    server.executor=None;server.instance_name='ec2-kube-live'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield engine,Client(engine.endpoint,token=token,timeout=45)
    finally:server.shutdown();server.server_close();thread.join(timeout=5);engine.close()


def main():
    namespace='ec2-kube-live-'+uuid.uuid4().hex[:8]
    image='localcloud-ec2-web:rea-'+uuid.uuid4().hex[:10]
    helper_image='localcloud-ec2-helper:rea-'+uuid.uuid4().hex[:10] if '--with-metadata' in sys.argv else None
    helper_host=False;helper_node=False
    report={'passed':False,'namespace':namespace,'context':CONTEXT,'checks':[],'guest_image':image}
    proxy=None;forward=None;namespace_created=False;node_tag=False;host_tag=False
    instance=None;bad=None;volumes=[]
    with tempfile.TemporaryDirectory(prefix='localcloud-ec2-kube-') as temporary:
        root=Path(temporary);state=root/'state.db'
        def check(name):report['checks'].append(name)
        try:
            # Unique aliases preserve all existing host and node image tags.
            command('docker','image','inspect',SOURCE_IMAGE)
            command('docker','tag',SOURCE_IMAGE,image);host_tag=True
            with tempfile.TemporaryFile() as archive:
                p=subprocess.run(['docker','image','save','--platform','linux/amd64',image],stdout=archive,stderr=subprocess.PIPE,timeout=60)
                if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-1000:])
                archive.seek(0)
                p=subprocess.run(['docker','exec','-i',NODE,'ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=archive,capture_output=True,timeout=90)
                if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-1000:])
                node_tag=True
            if helper_image:
                # Reuse cached system dependencies and copy current source. This
                # tests the new module without package downloads or stale code.
                helper_base='localcloud:ec2-runtime'
                identity=json.loads(command('docker','image','inspect',helper_base))[0]['Id']
                dockerfile=root/'Dockerfile.helper'
                dockerfile.write_text('FROM '+helper_base+'\nUSER root\nCOPY localcloud /app/localcloud\nUSER 10001:10001\n')
                command('docker','build','--network','none','--pull=false','-f',str(dockerfile),'-t',helper_image,str(ROOT),timeout=120);helper_host=True
                report['helper_base_image_id']=identity
                report['helper_build']='cached runtime dependencies plus current localcloud source; no package downloads'
                with tempfile.TemporaryFile() as archive:
                    p=subprocess.run(['docker','image','save','--platform','linux/amd64',helper_image],stdout=archive,stderr=subprocess.PIPE,timeout=60)
                    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-1000:])
                    archive.seek(0)
                    p=subprocess.run(['docker','exec','-i',NODE,'ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=archive,capture_output=True,timeout=90)
                    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-1000:])
                    helper_node=True
                report['metadata_helper_image']=helper_image
            kubectl('create','-f','-',input=json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,'labels':{'localcloud.audit':namespace}}}))
            namespace_created=True
            proxy_port=free_port();proxy_log=(root/'proxy.log').open('w')
            proxy=subprocess.Popen(['kubectl','--context',CONTEXT,'proxy','--address=127.0.0.1','--port='+str(proxy_port)],stdout=proxy_log,stderr=subprocess.STDOUT)
            proxy_url='http://127.0.0.1:'+str(proxy_port)
            def proxy_ready():
                with urllib.request.urlopen(proxy_url+'/version',timeout=1) as response:return response.status==200
            wait(proxy_ready,20)
            with backend(state,proxy_url,namespace,image,helper_image) as (engine,client):
                def call(op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                user_data="#!/bin/sh\necho userdata-ran >/tmp/rea-user-data\ntest -f /data/marker || printf pvc-survives >/data/marker\necho user-data-console-ok\n"
                launched=call('RunInstances',ImageId='ami-web',Name='kube-web',PortMappings=[{'HostPort':0,'ContainerPort':8080}],UserData=base64.b64encode(user_data.encode()).decode(),BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1,'DeleteOnTermination':True}}])['Instances'][0]
                instance=launched['InstanceId'];volumes=[m['Ebs']['VolumeId'] for m in launched.get('BlockDeviceMappings',[])]
                def describe():return call('DescribeInstances',InstanceIds=[instance])['Reservations'][0]['Instances'][0]
                ready=wait(lambda:(x if (x:=describe())['State']['Name']=='running' else None))
                report['launched']={'InstanceId':instance,'Backend':ready['Backend'],'State':ready['State'],'Endpoints':ready.get('Endpoints',[])}
                check('real-ready-pod-drives-running-ec2-state')
                pods=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items']
                candidates=[p for p in pods if instance in p['metadata']['name']]
                assert len(candidates)==1,[p['metadata']['name'] for p in pods]
                pod=candidates[0]['metadata']['name'];uid=candidates[0]['metadata']['uid']
                containers=[c['name'] for c in candidates[0]['spec']['containers']]
                guest='guest' if 'guest' in containers else containers[0]
                def guest_exec(code):return kubectl('exec','-n',namespace,pod,'-c',guest,'--','python3','-c',code,timeout=20).strip()
                assert guest_exec("print(open('/tmp/rea-user-data').read().strip());print(open('/data/marker').read())")=='userdata-ran\npvc-survives'
                check('user-data-init-and-real-pvc-file-bytes')
                if helper_image:
                    got=guest_exec("import os,urllib.request;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));print(op.open(os.environ['AWS_EC2_METADATA_SERVICE_ENDPOINT']+'/latest/meta-data/instance-id').read().decode())")
                    assert got==instance,got
                    check('pod-local-imdsv1-guest-instance-id')
                    previous_uid=uid
                    call('ModifyInstanceMetadataOptions',InstanceIds=[instance],HttpTokens='required')
                    ready=wait(lambda:(x if (x:=describe())['State']['Name']=='running' and x.get('PodUid')!=previous_uid else None),60)
                    pod=ready['PodName'];uid=ready['PodUid']
                    code="""import os,urllib.request,urllib.error
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base=os.environ['AWS_EC2_METADATA_SERVICE_ENDPOINT']+'/latest/'
try:op.open(base+'meta-data/instance-id');print('unexpected-allow')
except urllib.error.HTTPError as error:print(error.code)
token=op.open(urllib.request.Request(base+'api/token',data=b'',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})).read().decode()
print(op.open(urllib.request.Request(base+'meta-data/instance-id',headers={'X-aws-ec2-metadata-token':token})).read().decode())"""
                    assert guest_exec(code)=='401\n'+instance
                    check('metadata-option-rollout-and-imdsv2-token-enforcement')
                services=json.loads(kubectl('get','services','-n',namespace,'-o','json'))['items']
                service=next(s['metadata']['name'] for s in services if instance in s['metadata']['name'])
                forward_log=(root/'forward.log').open('w')
                forward=subprocess.Popen(['kubectl','--context',CONTEXT,'-n',namespace,'port-forward','service/'+service,':8080','--address=127.0.0.1'],stdout=forward_log,stderr=subprocess.STDOUT)
                def forwarded():
                    forward_log.flush();match=re.search(r'Forwarding from 127\.0\.0\.1:(\d+)',(root/'forward.log').read_text())
                    if not match:return None
                    with urllib.request.urlopen('http://127.0.0.1:'+match[1],timeout=2) as response:return response.read(1000)
                assert b'Hello' in wait(forwarded,30)
                check('cluster-service-and-owned-port-forward-web-reachability')
                forward.terminate();forward.wait(timeout=10);forward=None;forward_log.close()
                from localcloud.engine import Error
                try:call('CreateSnapshot',VolumeId=volumes[0])
                except Error as error:assert error.status==501,error
                else:raise AssertionError('Kubernetes snapshot request incorrectly reported success')
                check('unsupported-kubernetes-snapshot-fails-explicitly')
                output=call('GetConsoleOutput',InstanceIds=[instance])
                text=base64.b64decode(output.get('Output','')).decode(errors='replace')
                assert 'user-data-console-ok' in text,text
                check('instance-console-contains-user-data-log')
                call('StopInstances',InstanceIds=[instance])
                wait(lambda:not json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items'],45)
                assert describe()['State']['Name']=='stopped'
                call('StartInstances',InstanceIds=[instance])
                ready=wait(lambda:(x if (x:=describe())['State']['Name']=='running' else None))
                pods=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items'];pod=pods[0]['metadata']['name']
                assert pods[0]['metadata']['uid']!=uid
                assert guest_exec("print(open('/data/marker').read())")=='pvc-survives'
                check('stop-start-recreates-pod-and-preserves-pvc-bytes')
                uid=pods[0]['metadata']['uid'];call('RebootInstances',InstanceIds=[instance])
                def reboot_ready():
                    x=describe();ps=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items']
                    current=[p for p in ps if p['metadata']['uid']!=uid and p.get('metadata',{}).get('deletionTimestamp') is None]
                    return current if current and x['State']['Name']=='running' else None
                wait(reboot_ready,60);check('reboot-replaces-real-pod')
                missing=call('RunInstances',ImageId='ami-missing',Name='bad-image')['Instances'][0];bad=missing['InstanceId']
                def missing_report():
                    x=call('DescribeInstances',InstanceIds=[bad])['Reservations'][0]['Instances'][0]
                    ps=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items']
                    failed=[s for p in ps if bad in p['metadata']['name'] for s in p.get('status',{}).get('containerStatuses',[]) if s.get('state',{}).get('waiting',{}).get('reason') in ('ErrImageNeverPull','ImagePullBackOff','ErrImagePull','InvalidImageName')]
                    return (x,failed) if failed and x.get('RuntimeStatusReason') else None
                failed,details=wait(missing_report,40)
                assert failed['State']['Name']!='running'
                report['bad_image']={'State':failed['State'],'RuntimeStatusReason':failed.get('RuntimeStatusReason'),'pod_reason':details[0]['state']['waiting']['reason']}
                check('missing-image-remains-pending-instead-of-false-running')
            with backend(state,proxy_url,namespace,image,helper_image) as (engine,client):
                def call(op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
                x=call('DescribeInstances',InstanceIds=[instance])['Reservations'][0]['Instances'][0]
                assert x['State']['Name']=='running'
                check('backend-restart-reconciles-existing-owned-deployment')
                for key in (bad,instance):
                    if key:call('TerminateInstances',InstanceIds=[key])
                wait(lambda:not json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items'],45)
                remaining=json.loads(kubectl('get','deployments,services,configmaps,pvc','-n',namespace,'-o','json'))['items']
                # Namespace-created kube-root-ca ConfigMap is system-owned.
                ours=[x for x in remaining if x['metadata']['name']!='kube-root-ca.crt']
                assert not ours,[(x['kind'],x['metadata']['name']) for x in ours]
                check('terminate-removes-owned-workloads-service-config-and-delete-on-termination-pvc')
                report['passed']=True
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)
            if namespace_created:
                try:
                    ps=json.loads(kubectl('get','pods','-n',namespace,'-o','json'))['items']
                    report['failure_pods']=[{'name':p['metadata']['name'],'phase':p.get('status',{}).get('phase'),'containerStatuses':p.get('status',{}).get('containerStatuses',[]),'initContainerStatuses':p.get('status',{}).get('initContainerStatuses',[])} for p in ps]
                except Exception:pass
        finally:
            if forward:forward.terminate();forward.wait(timeout=10)
            if proxy:proxy.terminate();proxy.wait(timeout=10)
            if namespace_created:
                try:
                    ns=json.loads(kubectl('get','namespace',namespace,'-o','json'))
                    assert ns['metadata'].get('labels',{}).get('localcloud.audit')==namespace
                    kubectl('delete','namespace',namespace,'--wait=true','--timeout=90s',timeout=100)
                    report['namespace_removed']=True
                except Exception as error:report['namespace_cleanup_error']=str(error)
            if helper_node:
                p=command('docker','exec',NODE,'ctr','--namespace=k8s.io','images','rm','docker.io/library/'+helper_image,check=False)
                report['helper_node_tag_removed']=p.returncode==0
            if helper_host:
                p=command('docker','image','rm',helper_image,check=False);report['helper_host_tag_removed']=p.returncode==0
            if node_tag:
                p=command('docker','exec',NODE,'ctr','--namespace=k8s.io','images','rm','docker.io/library/'+image,check=False)
                report['node_tag_removed']=p.returncode==0
            if host_tag:
                p=command('docker','image','rm',image,check=False);report['host_tag_removed']=p.returncode==0
    target=ROOT/'docs/research/rea/ec2-kubernetes-live-report.json';target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['passed'] and report.get('namespace_removed') and report.get('node_tag_removed') and report.get('host_tag_removed') and (not helper_image or report.get('helper_node_tag_removed') and report.get('helper_host_tag_removed')) else 1


if __name__=='__main__':raise SystemExit(main())
