"""EC2 provider routes Kubernetes work without touching Docker or faking readiness."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from localcloud.engine import Engine, Error


class KubernetesRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.adapter=MagicMock()
        self.phase='pending'
        def launch(item,script):
            item['State']={'Code':0,'Name':'pending'}
            item['Endpoints']=['http://guest.test.svc:8080']
        def refresh(item):item['State']={'Code':{'pending':0,'running':16,'stopped':80,'terminated':48}[self.phase],'Name':self.phase}
        def lifecycle(item,action):
            self.phase={'stop':'stopped','start':'pending','reboot':'pending','terminate':'terminated'}[action]
            refresh(item)
        self.adapter.launch.side_effect=launch
        self.adapter.refresh.side_effect=refresh
        self.adapter.lifecycle.side_effect=lifecycle
        self.patch=patch('localcloud.ec2_kubernetes.EC2Kubernetes',return_value=self.adapter);self.patch.start()
        self.engine=Engine(str(Path(self.tmp.name)/'db'),ec2_images={'ami-web':'web:dev'},ec2_kubernetes={'client':None,'namespace':'test'})
        self.docker=patch.object(self.engine.ec2,'docker',side_effect=AssertionError('Kubernetes operation invoked Docker'));self.docker.start()
    def tearDown(self):
        self.docker.stop();self.engine.close();self.patch.stop();self.tmp.cleanup()
    def call(self,op,**args):return self.engine.ec2.call(op,args,'test')
    def test_name_idempotence_readiness_and_lifecycle_use_the_kube_adapter(self):
        args={'ImageId':'ami-web','Name':'web','ClientToken':'once','UserData':base64.b64encode(b'echo ready').decode()}
        first=self.call('RunInstances',**args)['Instances'][0]
        self.assertEqual(first['Backend'],'kubernetes');self.assertEqual(first['State']['Name'],'pending')
        self.assertEqual(first['Endpoints'],['http://guest.test.svc:8080'])
        second=self.call('RunInstances',**args)['Instances'][0]
        self.assertEqual(first['InstanceId'],second['InstanceId']);self.adapter.launch.assert_called_once()
        self.adapter.launch.assert_called_with(unittest.mock.ANY,b'echo ready')
        self.assertEqual(self.call('DescribeInstances',InstanceName='web')['Reservations'][0]['Instances'][0]['State']['Name'],'pending')
        self.phase='running'
        self.assertEqual(self.call('DescribeInstances',InstanceName='web')['Reservations'][0]['Instances'][0]['State']['Name'],'running')
        self.call('StopInstances',InstanceName='web');self.call('StartInstances',InstanceName='web')
        self.assertEqual([c.args[1] for c in self.adapter.lifecycle.call_args_list],['stop','start'])
    def test_console_is_retained_when_the_guest_is_terminated(self):
        instance=self.call('RunInstances',ImageId='ami-web')['Instances'][0]
        self.phase='running';self.adapter.console.return_value='booted successfully'
        self.call('TerminateInstances',InstanceIds=[instance['InstanceId']])
        result=self.call('GetConsoleOutput',InstanceIds=[instance['InstanceId']])
        self.assertEqual(base64.b64decode(result['Output']).decode(),'booted successfully')
    def test_pvc_creation_routes_requested_size_and_termination_releases_volume(self):
        instance=self.call('RunInstances',ImageId='ami-web',BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':2}}])['Instances'][0]
        volume=instance['BlockDeviceMappings'][0]['Ebs']['VolumeId']
        self.adapter.create_volume.assert_called_once_with(volume,None,size=2)
        self.call('TerminateInstances',InstanceIds=[instance['InstanceId']])
        self.adapter.delete_volume.assert_called_once_with(volume)
        self.assertEqual(self.call('DescribeVolumes')['Volumes'],[])
    def test_kube_snapshots_and_resize_fail_before_fake_data_success(self):
        volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)['VolumeId']
        for op,args in [('CreateSnapshot',{'VolumeId':volume}),('ModifyVolume',{'VolumeId':volume,'Size':2})]:
            with self.assertRaises(Error) as caught:self.call(op,**args)
            self.assertEqual(caught.exception.status,501)
        self.assertEqual(self.call('DescribeSnapshots')['Snapshots'],[])
        self.assertEqual(self.call('DescribeVolumes',VolumeIds=[volume])['Volumes'][0]['Size'],1)
    def test_iam_denial_does_not_submit_guest_and_portable_export_rejects_runtime(self):
        self.engine.iam_mode='enforce'
        with self.assertRaises(Error) as caught:self.call('RunInstances',ImageId='ami-web')
        self.assertEqual(caught.exception.code,'AccessDenied');self.adapter.launch.assert_not_called()
        self.engine.iam_mode='off';self.call('RunInstances',ImageId='ami-web')
        with self.assertRaises(Error):self.engine.tools.export_state()


if __name__=='__main__':unittest.main()
