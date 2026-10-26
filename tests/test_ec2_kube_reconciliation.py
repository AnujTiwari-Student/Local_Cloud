"""Public EC2 transactions reconcile owned objects and commit before deletion."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from localcloud.engine import Engine,Error
from test_ec2_kubernetes import API


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=API()
        self.engine=Engine(str(Path(self.tmp.name)/'state.db'),ec2_images={'ami-web':'web:dev'},ec2_kubernetes={'client':self.api,'namespace':'test'})
        self.kube=self.engine.ec2.kubernetes
    def tearDown(self):self.engine.close();self.tmp.cleanup()
    def call(self,op,**args):return self.engine.ec2.call(op,args,'test')
    def launch(self,volume=False):
        args={'ImageId':'ami-web','UserData':base64.b64encode(b'echo initialized >/tmp/marker').decode(),'PortMappings':[{'HostPort':0,'ContainerPort':8080}]}
        if volume:args['BlockDeviceMappings']=[{'DeviceName':'/data','Ebs':{'VolumeSize':1}}]
        item=self.call('RunInstances',**args)['Instances'][0];self.id=item['InstanceId'];self.name=item['KubernetesName'];self.dep=self.kube.apps+'/deployments/'+self.name
        self.cm=self.kube.base+'/configmaps/'+self.name;self.service=self.kube.base+'/services/'+self.name
        return item
    def describe(self):return self.call('DescribeInstances',InstanceIds=[self.id])['Reservations'][0]['Instances'][0]
    def ready(self):
        dep=self.api.objects[self.dep];labels=dep['metadata']['labels'];rs=self.name+'-rs';pod=self.name+'-pod'
        dep['status']={'observedGeneration':dep['metadata']['generation']}
        self.api.objects[self.kube.apps+'/replicasets/'+rs]={'metadata':{'name':rs,'uid':'rs-id','labels':labels,'ownerReferences':[{'kind':'Deployment','uid':dep['metadata']['uid']}]}}
        self.api.objects[self.kube.base+'/pods/'+pod]={'metadata':{'name':pod,'uid':'pod-id','labels':labels,'ownerReferences':[{'kind':'ReplicaSet','name':rs,'uid':'rs-id'}]},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}
    def no_pods(self):
        for path in list(self.api.objects):
            if '/pods/' in path or '/replicasets/' in path:self.api.objects.pop(path)
    def test_missing_service_repaired_without_adopting_old_uid(self):
        item=self.launch();self.ready();self.assertEqual(self.describe()['State']['Name'],'running')
        dep_uid=self.api.objects[self.dep]['metadata']['uid'];old=item['KubernetesServiceUid'];del self.api.objects[self.service]
        current=self.describe()
        self.assertNotEqual(current['KubernetesServiceUid'],old)
        self.assertEqual(self.api.objects[self.dep]['metadata']['uid'],dep_uid)
    def test_missing_deployment_and_config_restore_bootstrap_but_keep_pvc_identity(self):
        item=self.launch(volume=True);volume=item['BlockDeviceMappings'][0]['Ebs']['VolumeId'];uid=self.kube._volume_uid(volume)
        del self.api.objects[self.dep];del self.api.objects[self.cm]
        current=self.describe()
        self.assertNotEqual(current['KubernetesUid'],item['KubernetesUid'])
        self.assertEqual(self.api.objects[self.cm]['data']['user-data'],'echo initialized >/tmp/marker')
        self.assertEqual(self.kube._volume_uid(volume),uid)
        self.assertEqual(current['State']['Name'],'pending')
    def test_manual_scale_drift_is_repaired_and_stopped_instances_stay_stopped(self):
        self.launch();self.ready();self.describe()
        self.api.objects[self.dep]['spec']['replicas']=0
        self.describe();self.assertEqual(self.api.objects[self.dep]['spec']['replicas'],1)
        self.call('StopInstances',InstanceIds=[self.id]);self.no_pods();self.assertEqual(self.describe()['State']['Name'],'stopped')
        del self.api.objects[self.dep]
        self.assertEqual(self.describe()['State']['Name'],'stopped')
        self.assertEqual(self.api.objects[self.dep]['spec']['replicas'],0)
    def test_foreign_replacement_refused_in_describe_and_dashboard_view(self):
        self.launch();self.ready();self.describe()
        self.api.objects[self.service]['metadata']['uid']='replaced-resource'
        with self.assertRaises(Error) as caught:self.describe()
        self.assertEqual(caught.exception.status,409)
        overview=self.engine.overview()
        self.assertEqual(overview['instances'][0]['State']['Name'],'pending')
        self.assertIn('identity changed',overview['instances'][0]['RuntimeStatusReason'])
        self.assertEqual(self.api.objects[self.service]['metadata']['uid'],'replaced-resource')
    def test_missing_data_volume_is_not_silently_recreated_empty(self):
        item=self.launch(volume=True);volume=item['BlockDeviceMappings'][0]['Ebs']['VolumeId']
        path=self.kube.base+'/persistentvolumeclaims/'+self.kube.volume_name(volume)
        del self.api.objects[path];calls=len(self.api.calls)
        current=self.describe()
        self.assertEqual(current['State']['Name'],'pending');self.assertIn('Persistent volume',current['RuntimeStatusReason'])
        self.assertNotIn(path,self.api.objects)
        self.assertFalse(any(method=='POST' for method,_,_ in self.api.calls[calls:]))
    def test_partial_launch_rollback_removes_external_resources_and_uid_records(self):
        self.api.fail='/services'
        with self.assertRaises(Error):self.launch(volume=True)
        self.assertEqual(self.api.objects,{})
        self.assertEqual(self.call('DescribeInstances')['Reservations'],[])
        self.assertEqual(self.call('DescribeVolumes')['Volumes'],[])
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM ec2_kubernetes_pvcs').fetchone()[0],0)
        self.assertEqual(self.kube.journal.jobs(),[])
    def test_termination_deletes_only_after_the_state_commit_and_waits_for_confirmation(self):
        self.launch();original=self.api.request;observed=[]
        def inspect(method,path,body=None,text=False):
            if method=='DELETE':
                self.assertFalse(self.engine.db.in_transaction)
                stored=json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])['instances'][self.id]
                self.assertEqual(stored['KubernetesDesiredState'],'terminated');observed.append(path)
            return original(method,path,body,text)
        self.api.request=inspect
        changed=self.call('TerminateInstances',InstanceIds=[self.id])['TerminatingInstances'][0]
        self.assertEqual(changed['CurrentState']['Name'],'shutting-down')
        self.assertTrue(observed)
        self.assertEqual(self.describe()['State']['Name'],'terminated')
        self.assertEqual(self.kube.journal.jobs(),[])


if __name__=='__main__':unittest.main()
