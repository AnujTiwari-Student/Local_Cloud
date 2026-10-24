"""Storage SDK shapes, complete modification history and portable validation."""
import json
import tempfile
import unittest
from pathlib import Path
from localcloud.engine import Engine, Error


class StorageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'db')
        self.engine = Engine(self.path)
    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()
    def call(self, op, **args):
        return self.engine.ec2.call(op,args,'test')
    def test_history_retains_every_change_and_paginated_reads_survive_restart(self):
        volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)['VolumeId']
        first=self.call('ModifyVolume',VolumeId=volume,Size=2)['VolumeModification']
        self.assertEqual(first['ModificationState'],'modifying');self.assertNotIn('EndTime',first)
        self.call('ModifyVolume',VolumeId=volume,VolumeType='gp3')
        self.call('ModifyVolume',VolumeId=volume,Iops=4000,Throughput=250)
        for rate in (300,350,400):self.call('ModifyVolume',VolumeId=volume,Throughput=rate)
        self.engine.close();self.engine=Engine(self.path)
        page=self.call('DescribeVolumesModifications',VolumeIds=[volume],MaxResults=5)
        self.assertEqual(len(page['VolumesModifications']),5)
        second=self.call('DescribeVolumesModifications',VolumeIds=[volume],NextToken=page['NextToken'],MaxResults=5)
        rows=page['VolumesModifications']+second['VolumesModifications']
        self.assertEqual(len(rows),6)
        self.assertEqual((rows[0]['OriginalSize'],rows[0]['TargetSize']),(1,2))
        self.assertEqual((rows[1]['TargetVolumeType'],rows[1]['TargetIops'],rows[1]['TargetThroughput']),('gp3',3000,125))
        self.assertEqual((rows[2]['OriginalIops'],rows[2]['TargetIops']),(3000,4000))
        self.assertTrue(all(r['ModificationState']=='completed' and 'EndTime' in r for r in rows))
        self.engine.tools.import_state(self.engine.tools.export_state())
        self.assertEqual(len(self.call('DescribeVolumesModifications',VolumeIds=[volume])['VolumesModifications']),6)
    def test_failed_resize_does_not_add_history_or_change_volume(self):
        volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=2)['VolumeId']
        for args in ({'Size':1},{'Iops':1},{'VolumeType':'not-real'}):
            with self.assertRaises(Error):self.call('ModifyVolume',VolumeId=volume,**args)
        self.assertEqual(self.call('DescribeVolumesModifications',VolumeIds=[volume])['VolumesModifications'],[])
        self.assertEqual(self.call('DescribeVolumes',VolumeIds=[volume])['Volumes'][0]['Size'],2)
    def test_snapshot_tag_copy_uses_sdk_string_and_explicit_tags_override(self):
        instance=self.call('RunInstances',ImageId='ami-local',BlockDeviceMappings=[{'DeviceName':'/data','Ebs':{'VolumeSize':1}}])['Instances'][0]
        volume=instance['BlockDeviceMappings'][0]['Ebs']['VolumeId']
        self.call('CreateTags',Resources=[volume],Tags=[{'Key':'Origin','Value':'volume'},{'Key':'Shared','Value':'volume'}])
        tags=[{'ResourceType':'snapshot','Tags':[{'Key':'Shared','Value':'snapshot'}]}]
        result=self.call('CreateSnapshots',InstanceSpecification={'InstanceId':instance['InstanceId']},CopyTagsFromSource='volume',TagSpecifications=tags)
        self.assertEqual({t['Key']:t['Value'] for t in result['Snapshots'][0]['Tags']},{'Origin':'volume','Shared':'snapshot'})
        count=len(self.call('DescribeSnapshots')['Snapshots'])
        with self.assertRaises(Error):self.call('CreateSnapshots',InstanceSpecification={'InstanceId':instance['InstanceId']},CopyTagsFromSource='invalid')
        self.assertEqual(len(self.call('DescribeSnapshots')['Snapshots']),count)
    def test_import_rejects_history_for_another_volume_and_nonboolean_default(self):
        volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)['VolumeId']
        self.call('ModifyVolume',VolumeId=volume,Size=2)
        # Validator must check private metadata rather than accepting arbitrary fixture data.
        from localcloud.ec2_infrastructure import validate_infrastructure
        state=json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])['ec2']
        state['volumes'][volume]['_modifications'][0]['VolumeId']='vol-00000000000000000'
        with self.assertRaises(ValueError):validate_infrastructure(state)
        state['volumes'][volume]['_modifications'][0]['VolumeId']=volume
        state['_ebs_encryption_default']='true'
        with self.assertRaises(ValueError):validate_infrastructure(state)


if __name__=='__main__':unittest.main()
