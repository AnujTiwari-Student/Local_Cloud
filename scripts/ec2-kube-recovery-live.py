"""Owned direct-TLS Kubernetes EC2 crash/reconciliation and minimal-RBAC checks.

Secrets stay in private temporary files/pipes. Only the fixture namespace and
unique image tag are modified; the caller's kubeconfig/contexts are not changed.
"""
import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
CONTEXT='kind-localcloud'
NODE='localcloud-control-plane'


def command(*args,input=None,timeout=40,check=True):
    p=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:raise RuntimeError((p.stderr or p.stdout)[-1800:])
    return p.stdout if check else p


def kubectl(*args,**kwargs):return command('kubectl','--context',CONTEXT,*args,**kwargs)


def wait(callback,seconds=100):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            last=callback()
            if last:return last
        except Exception as error:last=error
        time.sleep(.3)
    raise RuntimeError('Fixture deadline exceeded: '+str(last)[-1000:])


@contextlib.contextmanager
def worker(config,path,fault=None):
    from localcloud.client import Client
    p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(config),str(path),fault or 'none'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        line=p.stdout.readline()
        if not line:
            raise RuntimeError('Backend worker failed before ready: '+p.stderr.read()[-1800:])
        ready=json.loads(line)
        yield p,Client(ready['endpoint'],token=ready['token'],timeout=45)
    finally:
        if p.poll() is None:
            try:p.stdin.close()
            except OSError:pass
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
        for stream in (p.stdin,p.stdout,p.stderr):
            if not stream.closed:stream.close()


def run_worker(config_path,state,fault):
    from http.server import ThreadingHTTPServer
    from localcloud.engine import Engine
    from localcloud.kube import KubeClient
    from localcloud.__main__ import Handler
    config=json.loads(Path(config_path).read_text())
    class FaultClient(KubeClient):
        def request(self,method,path,body=None,text=False):
            result=super().request(method,path,body,text)
            if method=='POST' and ((fault=='pvc' and path.endswith('/persistentvolumeclaims')) or (fault=='deployment' and path.endswith('/deployments'))):
                # API success has occurred, but the adapter has not seen the UID.
                os._exit(91)
            return result
    api=FaultClient(config['server'],token_file=config['token_file'],ca_file=config['ca_file'])
    engine=Engine(state,ec2_images={'ami-web':config['image']},ec2_kubernetes={'client':api,'namespace':config['namespace'],'image_pull_policy':'Never'})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    token=secrets.token_urlsafe(32);engine.endpoint='http://127.0.0.1:'+str(server.server_port)
    server.engine,server.management_token=engine,token;server.executor=None;server.instance_name='rea-recovery'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    # This is a private pipe consumed by the fixture parent, not retained output.
    print(json.dumps({'endpoint':engine.endpoint,'token':token}),flush=True)
    try:sys.stdin.read()
    finally:server.shutdown();server.server_close();thread.join(timeout=5);engine.close()


def main():
    from localcloud.kube import KubeClient,KubeError
    from localcloud.kube_manifests import installation
    namespace='ec2-kube-recovery-'+uuid.uuid4().hex[:8]
    image='localcloud-ec2-web:recovery-'+uuid.uuid4().hex[:10]
    report={'passed':False,'namespace':namespace,'context':CONTEXT,'authorization':'direct TLS service-account token, generated executor Role','checks':[]}
    made_ns=False;host_tag=False;node_tag=False;sa_token=''
    with tempfile.TemporaryDirectory(prefix='localcloud-kube-recovery-') as temporary:
        root=Path(temporary)
        try:
            kubeconfig=json.loads(kubectl('config','view','--raw','--minify','-o','json'))
            cluster=kubeconfig['clusters'][0]['cluster'];server=cluster['server']
            ca=root/'ca.crt'
            if cluster.get('certificate-authority-data'):ca.write_bytes(base64.b64decode(cluster['certificate-authority-data']))
            else:ca.write_bytes(Path(cluster['certificate-authority']).read_bytes())
            os.chmod(ca,0o600)
            kubectl('create','-f','-',input=json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,'labels':{'localcloud.audit':namespace}}}));made_ns=True
            parts=installation(namespace,'unused:fixture')['items']
            selected=[obj for obj in parts if obj['kind'] in ('ServiceAccount','Role','RoleBinding') and obj['metadata']['name']=='localcloud-executor']
            assert len(selected)==3
            for obj in selected:kubectl('create','-f','-',input=json.dumps(obj))
            sa_token=kubectl('create','token','localcloud-executor','-n',namespace,'--duration=15m').strip()
            token_file=root/'sa-token';token_file.write_text(sa_token);os.chmod(token_file,0o600)
            api=KubeClient(server,token_file=str(token_file),ca_file=str(ca))
            base='/api/v1/namespaces/'+namespace
            wait(lambda:api.get(base+'/pods').get('kind')=='PodList',20)
            try:api.get(base+'/secrets')
            except KubeError as error:assert error.status==403
            else:raise AssertionError('Executor Role unexpectedly allowed Secrets access')
            report['checks'].append('direct-service-account-tls-and-secret-access-denied')
            sentinel={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'foreign-sentinel','namespace':namespace,'labels':{'localcloud.audit':namespace}},'data':{'keep':'unchanged'}}
            kubectl('create','-f','-',input=json.dumps(sentinel))
            command('docker','image','inspect','localcloud-ec2-web:dev')
            command('docker','tag','localcloud-ec2-web:dev',image);host_tag=True
            with tempfile.TemporaryFile() as archive:
                p=subprocess.run(['docker','image','save','--platform','linux/amd64',image],stdout=archive,stderr=subprocess.PIPE,timeout=60)
                if p.returncode:raise RuntimeError(p.stderr.decode()[-1200:])
                archive.seek(0)
                p=subprocess.run(['docker','exec','-i',NODE,'ctr','--namespace=k8s.io','images','import','--platform','linux/amd64','-'],stdin=archive,capture_output=True,timeout=90)
                if p.returncode:raise RuntimeError(p.stderr.decode()[-1200:])
                node_tag=True
            config=root/'worker.json';config.write_text(json.dumps({'server':server,'token_file':str(token_file),'ca_file':str(ca),'namespace':namespace,'image':image}));os.chmod(config,0o600)
            def objects():
                out=[]
                for resource in ('persistentvolumeclaims','services','configmaps'):
                    out.extend(api.get(base+'/'+resource).get('items',[]))
                out.extend(api.get('/apis/apps/v1/namespaces/'+namespace+'/deployments').get('items',[]))
                return [obj for obj in out if obj.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/managed-by')=='localcloud-ec2']
            def call(client,op,**args):return client.request('POST','/_local/ec2',{'Action':op,**args})
            for fault in ('pvc','deployment'):
                state=root/(fault+'.sqlite3')
                with worker(config,state,fault) as (process,client):
                    try:
                        if fault=='pvc':call(client,'CreateVolume',AvailabilityZone='us-east-1a',Size=1)
                        else:call(client,'RunInstances',ImageId='ami-web',Name='interrupted',PortMappings=[{'HostPort':0,'ContainerPort':8080}],BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1}}])
                    except Exception:pass
                    else:raise AssertionError('Injected process termination did not interrupt the request')
                    assert process.wait(timeout=10)==91
                with worker(config,state) as (process,client):
                    def recovered():
                        assert call(client,'DescribeInstances')['Reservations']==[]
                        assert call(client,'DescribeVolumes')['Volumes']==[]
                        return not objects()
                    wait(recovered,90)
                    assert api.get(base+'/configmaps/foreign-sentinel')['data']=={'keep':'unchanged'}
                report['checks'].append('restart-cleans-uncommitted-'+fault+'-response-loss-and-preserves-foreign-object')
            state=root/'committed.sqlite3'
            with worker(config,state) as (process,client):
                script="#!/bin/sh\ntest -f /data/marker || printf retained-bytes >/data/marker\necho minimal-rbac-userdata-ok\n"
                first=call(client,'RunInstances',ImageId='ami-web',Name='committed',UserData=base64.b64encode(script.encode()).decode(),PortMappings=[{'HostPort':0,'ContainerPort':8080}],BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1,'DeleteOnTermination':True}}])['Instances'][0]
                instance=first['InstanceId']
                def describe():return call(client,'DescribeInstances',InstanceIds=[instance])['Reservations'][0]['Instances'][0]
                ready=wait(lambda:(value if (value:=describe())['State']['Name']=='running' else None))
                initial_uid=ready['KubernetesUid'];pod_uid=ready['PodUid'];pod_name=ready['PodName']
                marker=kubectl('exec','-n',namespace,pod_name,'-c','guest','--','python3','-c',"print(open('/data/marker').read())").strip();assert marker=='retained-bytes'
                output=base64.b64decode(call(client,'GetConsoleOutput',InstanceIds=[instance])['Output']).decode();assert 'minimal-rbac-userdata-ok' in output
                report['checks'].append('minimal-role-runs-ready-guest-pvc-and-console')
            with worker(config,state) as (process,client):
                def describe():return call(client,'DescribeInstances',InstanceIds=[instance])['Reservations'][0]['Instances'][0]
                ready=wait(lambda:(value if (value:=describe())['State']['Name']=='running' else None))
                assert ready['KubernetesUid']==initial_uid and ready['PodUid']==pod_uid
                report['checks'].append('committed-guest-identities-survive-backend-restart')
                call(client,'StopInstances',InstanceIds=[instance]);wait(lambda:describe()['State']['Name']=='stopped',60)
                call(client,'StartInstances',InstanceIds=[instance]);ready=wait(lambda:(value if (value:=describe())['State']['Name']=='running' else None))
                assert ready['PodUid']!=pod_uid
                marker=kubectl('exec','-n',namespace,ready['PodName'],'-c','guest','--','python3','-c',"print(open('/data/marker').read())").strip();assert marker=='retained-bytes'
                report['checks'].append('journaled-stop-start-recreates-pod-with-original-pvc-bytes')
                # The production provider should repair missing non-data resources.
                service=ready['KubernetesName'];old_service=api.get(base+'/services/'+service)['metadata']['uid']
                api.request('DELETE',base+'/services/'+service,{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':old_service}})
                def service_repaired():
                    value=describe()
                    try:new=api.get(base+'/services/'+service)
                    except KubeError as error:
                        if error.status==404:return None
                        raise
                    return value if new['metadata']['uid']!=old_service else None
                wait(service_repaired,60);report['checks'].append('describe-reconciles-deleted-owned-service')
                cm_path=base+'/configmaps/'+service
                old_cm=api.get(cm_path)['metadata']['uid']
                api.request('DELETE',cm_path,{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':old_cm}})
                def config_repaired():
                    describe()
                    try:cm=api.get(cm_path)
                    except KubeError as error:
                        if error.status==404:return None
                        raise
                    return cm if cm['metadata']['uid']!=old_cm and cm.get('data',{}).get('user-data')==script else None
                wait(config_repaired,60);report['checks'].append('describe-rebuilds-deleted-userdata-configmap-from-committed-state')
                deployment_path='/apis/apps/v1/namespaces/'+namespace+'/deployments/'+service
                dep=api.get(deployment_path);old_dep=dep['metadata']['uid']
                api.request('DELETE',deployment_path,{'apiVersion':'v1','kind':'DeleteOptions','propagationPolicy':'Foreground','preconditions':{'uid':old_dep}})
                def absent(path):
                    try:api.get(path)
                    except KubeError as error:
                        if error.status==404:return True
                        raise
                    return False
                wait(lambda:absent(deployment_path),60)
                ready=wait(lambda:(value if (value:=describe())['State']['Name']=='running' and value['KubernetesUid']!=old_dep else None))
                marker=kubectl('exec','-n',namespace,ready['PodName'],'-c','guest','--','python3','-c',"print(open('/data/marker').read())").strip();assert marker=='retained-bytes'
                report['checks'].append('describe-rebuilds-deleted-deployment-with-original-pvc-data')
                dep=api.get(deployment_path);dep['spec']['replicas']=0;api.request('PUT',deployment_path,dep)
                ready=wait(lambda:(value if (value:=describe())['State']['Name']=='running' and api.get(deployment_path)['spec']['replicas']==1 else None))
                report['checks'].append('describe-restores-manual-scale-drift-to-committed-running-intent')
                svc_path=base+'/services/'+service;owned=api.get(svc_path)
                api.request('DELETE',svc_path,{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':owned['metadata']['uid']}})
                wait(lambda:absent(svc_path),20)
                foreign=api.request('POST',base+'/services',{'apiVersion':'v1','kind':'Service','metadata':{'name':service,'namespace':namespace,'labels':{'fixture.foreign':'true'}},'spec':{'selector':{'fixture.foreign':'true'},'ports':[{'port':8080,'targetPort':8080}]}})
                from localcloud.engine import Error
                try:describe()
                except Error as error:assert error.status==409,error
                else:raise AssertionError('Foreign replacement was silently adopted')
                assert api.get(svc_path)['metadata']['uid']==foreign['metadata']['uid']
                assert api.get(svc_path)['spec']['selector']=={'fixture.foreign':'true'}
                api.request('DELETE',svc_path,{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':foreign['metadata']['uid']}})
                wait(lambda:absent(svc_path),20);wait(lambda:describe()['State']['Name']=='running')
                report['checks'].append('foreign-service-replacement-is-preserved-and-rejected')
                call(client,'StopInstances',InstanceIds=[instance]);wait(lambda:describe()['State']['Name']=='stopped',60)
                claims=api.get(base+'/persistentvolumeclaims')['items'];assert len(claims)==1
                claim=claims[0];claim_path=base+'/persistentvolumeclaims/'+claim['metadata']['name']
                api.request('DELETE',claim_path,{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':claim['metadata']['uid']}})
                wait(lambda:absent(claim_path),45)
                missing=describe();assert missing['State']['Name']=='pending' and 'Persistent volume is missing' in missing.get('RuntimeStatusReason',''),missing
                assert api.get(base+'/persistentvolumeclaims')['items']==[]
                report['checks'].append('missing-persistent-data-is-reported-without-empty-replacement')
                call(client,'TerminateInstances',InstanceIds=[instance])
                wait(lambda:describe()['State']['Name']=='terminated',90)
                wait(lambda:not objects(),60)
                report['checks'].append('journaled-termination-finishes-only-after-owned-resources-disappear')
                assert api.get(base+'/configmaps/foreign-sentinel')['data']=={'keep':'unchanged'}
            report['passed']=True
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)
        finally:
            if made_ns:
                try:
                    ns=json.loads(kubectl('get','namespace',namespace,'-o','json'));assert ns['metadata'].get('labels',{}).get('localcloud.audit')==namespace
                    kubectl('delete','namespace',namespace,'--wait=true','--timeout=90s',timeout=100);report['namespace_removed']=True
                except Exception as error:report['namespace_cleanup_error']=str(error)
            if node_tag:
                p=command('docker','exec',NODE,'ctr','--namespace=k8s.io','images','rm','docker.io/library/'+image,check=False);report['node_tag_removed']=p.returncode==0
            if host_tag:
                p=command('docker','image','rm',image,check=False);report['host_tag_removed']=p.returncode==0
    output=json.dumps(report,indent=2)
    if sa_token:output=output.replace(sa_token,'[REDACTED]')
    (ROOT/'docs/research/rea/ec2-kube-recovery-report.json').write_text(output+'\n')
    print(output)
    return 0 if report['passed'] and report.get('namespace_removed') and report.get('node_tag_removed') and report.get('host_tag_removed') else 1


if __name__=='__main__':
    if len(sys.argv)==5 and sys.argv[1]=='--worker':run_worker(sys.argv[2],sys.argv[3],sys.argv[4])
    else:raise SystemExit(main())
