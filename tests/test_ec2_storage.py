"""Storage extension behavior, state rollback and portability."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from localcloud.engine import Engine,Error

class StorageTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.engine=Engine(str(Path(self.tmp.name)/'db'))
 def tearDown(self):self.engine.close();self.tmp.cleanup()
 def call(self,op,**args):return self.engine.ec2.call(op,args,'test')
 def snapshot(self):
  volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=2)['VolumeId']
  return self.call('CreateSnapshot',VolumeId=volume)['SnapshotId']
 def test_copy_idempotence_source_validation_and_restore(self):
  source=self.snapshot();args={'SourceRegion':'us-east-1','SourceSnapshotId':source,'ClientToken':'copy-one','Description':'copy','DestinationRegion':'us-east-1','PresignedUrl':'https://must-not-fetch.invalid/first'}
  copied=self.call('CopySnapshot',**args)['SnapshotId']
  self.assertEqual(self.call('CopySnapshot',**{**args,'PresignedUrl':'https://must-not-fetch.invalid/changed-signature'})['SnapshotId'],copied)
  with self.assertRaises(Error):self.call('CopySnapshot',**{**args,'Description':'changed'})
  with self.assertRaises(Error):self.call('CopySnapshot',SourceRegion='us-west-2',SourceSnapshotId=source)
  volume=self.call('CreateVolume',SnapshotId=copied,AvailabilityZone='us-east-1a')
  self.assertEqual(volume['Size'],2)
  self.engine.tools.import_state(self.engine.tools.export_state())
 def test_permission_mutation_reset_and_atomic_invalid_batch(self):
  snap=self.snapshot();a={'UserId':'111111111111'}
  self.call('ModifySnapshotAttribute',SnapshotId=snap,CreateVolumePermission={'Add':[a]})
  self.assertEqual(self.call('DescribeSnapshotAttribute',SnapshotId=snap,Attribute='createVolumePermission')['CreateVolumePermissions'],[a])
  with self.assertRaises(Error):self.call('ModifySnapshotAttribute',SnapshotId=snap,CreateVolumePermission={'Add':[{'Group':'all'},{'Group':'invalid'}]})
  self.assertEqual(self.call('DescribeSnapshotAttribute',SnapshotId=snap,Attribute='createVolumePermission')['CreateVolumePermissions'],[a])
  self.engine.tools.import_state(self.engine.tools.export_state())
  self.call('ResetSnapshotAttribute',SnapshotId=snap,Attribute='createVolumePermission')
  self.assertEqual(self.call('DescribeSnapshotAttribute',SnapshotId=snap,Attribute='createVolumePermission')['CreateVolumePermissions'],[])
 def test_batch_snapshots_tags_and_helper_failure_cleanup(self):
  instance=self.call('RunInstances',ImageId='ami-local',BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1}},{'DeviceName':'/mnt/second','Ebs':{'VolumeSize':2}}])['Instances'][0]
  result=self.call('CreateSnapshots',InstanceSpecification={'InstanceId':instance['InstanceId']},TagSpecifications=[{'ResourceType':'snapshot','Tags':[{'Key':'Purpose','Value':'fixture'}]}])['Snapshots']
  self.assertEqual(len(result),2);self.assertTrue(all(s['Tags']==[{'Key':'Purpose','Value':'fixture'}] for s in result))
  before=len(self.call('DescribeSnapshots')['Snapshots'])
  with patch.object(self.engine.ec2.runtime,'create_volume',side_effect=[None,Error('RuntimeFailure','copy failed',503)]),patch.object(self.engine.ec2.runtime,'delete_volume') as deleted:
   with self.assertRaises(Error):self.call('CreateSnapshots',InstanceSpecification={'InstanceId':instance['InstanceId']})
   self.assertEqual(deleted.call_count,1)
  self.assertEqual(len(self.call('DescribeSnapshots')['Snapshots']),before)
 def test_auto_io_attributes_and_regional_discovery(self):
  volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)['VolumeId']
  self.call('ModifyVolumeAttribute',VolumeId=volume,AutoEnableIO={'Value':True})
  self.assertTrue(self.call('DescribeVolumeAttribute',VolumeId=volume,Attribute='autoEnableIO')['AutoEnableIO']['Value'])
  self.engine.tools.import_state(self.engine.tools.export_state())
  self.assertEqual(self.call('DescribeRegions',RegionNames=['us-east-1'])['Regions'][0]['RegionName'],'us-east-1')
  with self.assertRaises(Error):self.call('DescribeRegions',RegionNames=['us-west-2'])
