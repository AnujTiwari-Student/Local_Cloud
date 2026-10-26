"""Owned EC2 Kubernetes manifests, readiness, lifecycle and PVC boundaries."""
import copy
import base64
import json
from pathlib import Path
import tempfile
import unittest

from localcloud.engine import Engine, Error
from localcloud.kube import KubeError
from localcloud.ec2_kubernetes import EC2Kubernetes


class API:
    def __init__(self):
        self.objects = {}; self.calls = []; self.fail = None; self.sequence = 0
    def get(self, path):
        if path not in self.objects:raise KubeError('NotFound',path,404)
        return copy.deepcopy(self.objects[path])
    def list(self, path, labels=None):
        pairs=[part.split('=',1) for part in labels.split(',')] if labels else []
        return [copy.deepcopy(obj) for key,obj in self.objects.items() if key.rsplit('/',1)[0]==path and all(obj['metadata'].get('labels',{}).get(k)==v for k,v in pairs)]
    def request(self, method, path, body=None, text=False):
        self.calls.append((method,path,copy.deepcopy(body)))
        if text:return 'user-data output\nguest output\n'
        if self.fail and method=='POST' and path.endswith(self.fail):raise KubeError('Failure','injected create failure',503)
        if method=='DELETE':
            self.assert_uid(path,body['preconditions']['uid']);del self.objects[path];return {'status':'Success'}
        obj=copy.deepcopy(body)
        if method=='POST':
            path+='/'+body['metadata']['name']
            if path in self.objects:raise KubeError('Conflict',path,409)
            self.sequence+=1;obj['metadata']['uid']='uid-'+str(self.sequence);obj['metadata']['generation']=1
        if method=='PUT':
            if path not in self.objects:raise KubeError('NotFound',path,404)
            self.assert_uid(path,obj['metadata']['uid'])
            obj['metadata']['generation']=self.objects[path]['metadata'].get('generation',1)+1
        obj['metadata']['resourceVersion']=str(len(self.calls));self.objects[path]=obj
        return copy.deepcopy(obj)
    def assert_uid(self,path,uid):
        if self.objects[path]['metadata']['uid']!=uid:raise KubeError('Conflict','UID precondition',409)


class KubernetesEC2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=Engine(str(Path(self.tmp.name)/'state.db'))
        self.engine.ec2.images={'ami-web':'web:dev'};self.api=API()
        self.kube=EC2Kubernetes(self.engine.ec2,self.api,'test',helper_image='localcloud:dev')
        self.item={'InstanceId':'i-0123456789abcdef0','ImageId':'ami-web','InstanceType':'t3.micro','Backend':'kubernetes','Owner':'a'*32,
                   'State':{'Name':'pending','Code':0},'PortMappings':[{'HostPort':0,'ContainerPort':8080}], 'MetadataOptions':{'HttpTokens':'required'}}
        self.name=self.kube.name(self.item);self.dep=self.kube.apps+'/deployments/'+self.name
    def tearDown(self):self.kube.journal.close();self.engine.close();self.tmp.cleanup()
    def launch(self):
        script=b'echo user-data output; echo marker >/tmp/marker'
        self.item['UserData']=base64.b64encode(script).decode()
        self.kube.launch(self.item,script)
    def ready(self):
        dep=self.api.objects[self.dep];labels=self.kube.labels(self.item)
        dep['status']={'observedGeneration':dep['metadata']['generation'],'readyReplicas':1}
        rs_name=self.name+'-replica';pod_name=self.name+'-pod'
        self.api.objects[self.kube.apps+'/replicasets/'+rs_name]={'metadata':{'name':rs_name,'uid':'replica-uid','labels':labels,'ownerReferences':[{'kind':'Deployment','uid':dep['metadata']['uid']}]}}
        self.api.objects[self.kube.base+'/pods/'+pod_name]={'metadata':{'name':pod_name,'uid':'pod-uid','labels':labels,'ownerReferences':[{'kind':'ReplicaSet','uid':'replica-uid','name':rs_name}]},
            'spec':copy.deepcopy(dep['spec']['template']['spec']), 'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}
        return self.api.objects[self.kube.base+'/pods/'+pod_name]
    def test_manifest_is_unprivileged_allowlisted_and_metadata_contains_no_bearer(self):
        self.launch();pod=self.api.objects[self.dep]['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken']);self.assertEqual(pod['securityContext']['runAsUser'],65534)
        self.assertEqual(pod['containers'][0]['image'],'web:dev');self.assertEqual(pod['containers'][0]['imagePullPolicy'],'IfNotPresent')
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(pod['containers'][0]['securityContext']['capabilities']['drop'],['ALL'])
        self.assertEqual([c['name'] for c in pod['initContainers']],['metadata','user-data'])
        self.assertEqual([c['name'] for c in pod['containers']],['guest'])
        raw=json.dumps(pod);self.assertNotIn('LOCALCLOUD_MANAGEMENT_TOKEN',raw);self.assertNotIn('hostNetwork',raw)
        self.assertEqual(self.item['State']['Name'],'pending')
        self.assertEqual(self.item['Endpoints'],['http://'+self.name+'.test.svc:8080'])
    def test_metadata_starts_before_user_data_and_shares_endpoint_with_guest(self):
        self.launch();pod=self.api.objects[self.dep]['spec']['template']['spec']
        helper,bootstrap=pod['initContainers']
        self.assertEqual(helper['restartPolicy'],'Always')
        self.assertIn('startupProbe',helper)
        self.assertIn('1338',helper['startupProbe']['exec']['command'][-1])
        self.assertEqual(bootstrap['env'],pod['containers'][0]['env'])
        self.assertEqual({v['name']:v['value'] for v in bootstrap['env']},
                         {'AWS_EC2_METADATA_SERVICE_ENDPOINT':'http://127.0.0.1:1338','LOCALCLOUD_METADATA_URL':'http://127.0.0.1:1338'})
        self.assertNotIn('volumeMounts',helper)
        self.assertEqual(helper['securityContext']['capabilities']['drop'],['ALL'])
    def test_metadata_without_user_data_stays_a_restartable_init_sidecar(self):
        self.kube.launch(self.item,b'');pod=self.api.objects[self.dep]['spec']['template']['spec']
        self.assertEqual([c['name'] for c in pod['initContainers']],['metadata'])
        self.assertEqual(pod['initContainers'][0]['restartPolicy'],'Always')
        self.assertNotIn('bootstrap',[v['name'] for v in pod['volumes']])
    def test_no_helper_leaves_user_data_without_metadata_configuration(self):
        self.kube.helper_image=None;self.launch();pod=self.api.objects[self.dep]['spec']['template']['spec']
        self.assertEqual([c['name'] for c in pod['initContainers']],['user-data'])
        self.assertNotIn('env',pod['initContainers'][0]);self.assertNotIn('env',pod['containers'][0])
    def test_running_requires_ready_pod_and_current_deployment_generation(self):
        self.launch();self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'pending')
        pod=self.ready();pod['status']['conditions'][0]['status']='False'
        pod['status']['containerStatuses']=[{'state':{'waiting':{'reason':'ImagePullBackOff'}}}]
        self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'pending');self.assertEqual(self.item['RuntimeStatusReason'],'ImagePullBackOff')
        pod['status']['conditions'][0]['status']='True';pod['status']['containerStatuses']=[]
        self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'running')
        self.kube.lifecycle(self.item,'reboot');self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'pending')
        self.ready();self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'running')
    def test_stop_waits_for_pods_then_start_and_termination_have_actual_effects(self):
        self.launch();self.ready();self.kube.refresh(self.item)
        self.kube.lifecycle(self.item,'stop');self.assertEqual(self.api.objects[self.dep]['spec']['replicas'],0)
        self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'stopping')
        del self.api.objects[self.kube.base+'/pods/'+self.name+'-pod']
        self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'stopped')
        self.kube.lifecycle(self.item,'start');self.assertEqual(self.api.objects[self.dep]['spec']['replicas'],1)
        self.ready();self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'running')
        self.assertIn('user-data output',self.kube.console(self.item))
        self.kube.lifecycle(self.item,'terminate');self.assertEqual(self.item['State']['Name'],'shutting-down');self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'terminated');self.assertNotIn(self.dep,self.api.objects)
        self.assertNotIn(self.kube.base+'/services/'+self.name,self.api.objects)
    def test_pod_owner_chain_and_replaced_deployment_never_adopted(self):
        self.launch();pod=self.ready();pod['metadata']['ownerReferences'][0]['uid']='unrelated-replica'
        with self.assertRaises(Error):self.kube.refresh(self.item)
        self.api.objects[self.dep]['metadata']['uid']='replacement'
        with self.assertRaises(Error):self.kube.lifecycle(self.item,'stop')
        self.assertEqual(self.api.objects[self.dep]['spec']['replicas'],1)
    def test_launch_conflict_or_failed_service_preserves_foreign_resource(self):
        path=self.kube.base+'/configmaps/'+self.name
        self.api.objects[path]={'metadata':{'name':self.name,'uid':'foreign','labels':{}}}
        with self.assertRaises(Error):self.launch()
        self.assertEqual(self.api.objects[path]['metadata']['uid'],'foreign');self.assertNotIn(self.dep,self.api.objects)
        self.api.objects.clear();self.api.fail='/services'
        with self.assertRaises(Error):self.launch()
        self.assertEqual(self.api.objects,{})
    def test_pvc_size_mount_reconfigure_and_replacement_uid_guard(self):
        key='vol-0123456789abcdef0'
        with self.engine.db:self.kube.create_volume(key,size=3)
        path=self.kube.base+'/persistentvolumeclaims/'+self.kube.volume_name(key)
        self.assertEqual(self.api.objects[path]['spec']['resources']['requests']['storage'],'3Gi')
        self.item['BlockDeviceMappings']=[{'DeviceName':'/dev/sdf','Ebs':{'VolumeId':key}}];self.launch()
        mounts=self.api.objects[self.dep]['spec']['template']['spec']['containers'][0]['volumeMounts']
        self.assertEqual(mounts[1]['mountPath'],'/mnt/ebs/sdf')
        with self.assertRaises(Error):self.kube.reconfigure(self.item,[])
        self.kube.lifecycle(self.item,'stop');self.kube.refresh(self.item)
        self.kube.reconfigure(self.item,[])
        self.assertEqual(len(self.api.objects[self.dep]['spec']['template']['spec']['containers'][0]['volumeMounts']),1)
        self.assertNotIn('volumeMounts',self.api.objects[self.dep]['spec']['template']['spec']['initContainers'][0])
        self.api.objects[path]['metadata']['uid']='replacement'
        with self.assertRaises(Error):self.kube.delete_volume(key)
        self.assertIn(path,self.api.objects)
    def test_unsupported_snapshots_published_host_ports_and_ssh_fail_explicitly(self):
        with self.assertRaises(Error):self.kube.create_volume('snap-copy','vol-source')
        self.item['PortMappings'][0]['HostPort']=8088
        with self.assertRaises(Error):self.launch()
        self.assertEqual(self.api.objects,{})
        self.item['PortMappings'][0]['HostPort']=0;self.item['KeyName']='ssh-key'
        with self.assertRaises(Error):self.launch()
        self.assertEqual(self.api.objects,{})
    def test_stopped_type_update_changes_memory_and_metadata_before_next_pod(self):
        self.launch();self.kube.lifecycle(self.item,'stop');self.kube.refresh(self.item)
        self.kube.modify_type(self.item,'t3.small')
        self.assertEqual(self.api.objects[self.dep]['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'],'256Mi')
        cm=self.api.objects[self.kube.base+'/configmaps/'+self.name]
        self.assertEqual(json.loads(cm['data']['metadata.json'])['InstanceType'],'t3.small')
        self.item['InstanceType']='t3.small'
        generation=self.api.objects[self.dep]['metadata']['generation']
        self.kube.refresh(self.item)
        self.assertEqual(self.api.objects[self.dep]['metadata']['generation'],generation)
    def test_failed_metadata_rollout_restores_the_previous_configuration(self):
        self.launch();self.ready()
        cm_path=self.kube.base+'/configmaps/'+self.name
        previous=self.api.objects[cm_path]['data']['metadata.json']
        original=self.api.request
        def failing(method,path,body=None,text=False):
            if method=='PUT' and path==self.dep:raise KubeError('Conflict','injected update conflict',409)
            return original(method,path,body,text)
        self.api.request=failing
        with self.assertRaises(Error):self.kube.update_metadata(self.item,{'HttpTokens':'optional'})
        self.assertEqual(self.api.objects[cm_path]['data']['metadata.json'],previous)
    def test_metadata_update_preserves_uid_and_restarts_template(self):
        self.launch();self.ready()
        self.kube.update_metadata(self.item,{'HttpTokens':'optional'})
        cm=self.api.objects[self.kube.base+'/configmaps/'+self.name]
        self.assertEqual(json.loads(cm['data']['metadata.json'])['MetadataOptions']['HttpTokens'],'optional')
        self.assertIn('localcloud.dev/metadata',self.api.objects[self.dep]['spec']['template']['metadata']['annotations'])
        self.kube.refresh(self.item);self.assertEqual(self.item['State']['Name'],'pending')


if __name__=='__main__':unittest.main()
