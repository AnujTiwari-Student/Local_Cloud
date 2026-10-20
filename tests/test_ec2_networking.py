"""Network contract consistency, rollback, state portability and Query regression tests."""
import json,tempfile,unittest
from pathlib import Path
from localcloud.engine import Engine, Error
from localcloud.ec2 import query_request,query_response
from localcloud.cli import parser


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=Engine(str(Path(self.tmp.name)/'db'))
        self.vpc=self.call('CreateVpc',CidrBlock='10.84.0.0/16')['Vpc']['VpcId']
        self.subnet=self.call('CreateSubnet',VpcId=self.vpc,CidrBlock='10.84.1.0/24')['Subnet']['SubnetId']
    def tearDown(self):self.engine.close();self.tmp.cleanup()
    def call(self,op,**args):return self.engine.ec2.call(op,args,'test')
    def eni(self,**args):return self.call('CreateNetworkInterface',SubnetId=self.subnet,**args)['NetworkInterface']
    def describe(self,key):return self.call('DescribeNetworkInterfaces',NetworkInterfaceIds=[key])['NetworkInterfaces'][0]
    def instance(self):return self.call('RunInstances',ImageId='ami-local',SubnetId=self.subnet)['Instances'][0]

    def test_default_groups_and_eni_idempotence(self):
        first=self.eni(ClientToken='network-create')
        self.assertEqual(first['Groups'][0]['GroupName'],'default')
        self.assertRegex(first['MacAddress'],r'^02(:[0-9a-f]{2}){5}$')
        self.assertEqual(self.eni(ClientToken='network-create')['NetworkInterfaceId'],first['NetworkInterfaceId'])
        with self.assertRaises(Error):self.eni(ClientToken='network-create',Description='changed')

    def test_private_addresses_reservation_count_and_atomic_rollback(self):
        key=self.eni(PrivateIpAddress='10.84.1.10')['NetworkInterfaceId']
        before=self.call('DescribeSubnets',SubnetIds=[self.subnet])['Subnets'][0]['AvailableIpAddressCount']
        self.call('AssignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.11','10.84.1.12'])
        after=self.call('DescribeSubnets',SubnetIds=[self.subnet])['Subnets'][0]['AvailableIpAddressCount']
        self.assertEqual(before-after,2)
        with self.assertRaises(Error):self.eni(PrivateIpAddress='10.84.1.11')
        with self.assertRaises(Error):self.call('AssignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.13','10.99.0.4'])
        self.assertNotIn('10.84.1.13',[a['PrivateIpAddress'] for a in self.describe(key)['PrivateIpAddresses']])
        self.call('UnassignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.11'])
        self.assertEqual(self.eni(PrivateIpAddress='10.84.1.11')['PrivateIpAddress'],'10.84.1.11')
        with self.assertRaises(Error):self.call('UnassignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.10'])

    def test_private_ip_transfer_and_primary_protection(self):
        first=self.eni();second=self.eni()
        a,b=first['NetworkInterfaceId'],second['NetworkInterfaceId']
        self.call('AssignPrivateIpAddresses',NetworkInterfaceId=a,SecondaryPrivateIpAddressCount=2)
        ip=self.describe(a)['PrivateIpAddresses'][1]['PrivateIpAddress']
        self.call('AssignPrivateIpAddresses',NetworkInterfaceId=b,PrivateIpAddresses=[ip],AllowReassignment=True)
        self.assertNotIn(ip,[v['PrivateIpAddress'] for v in self.describe(a)['PrivateIpAddresses']])
        with self.assertRaises(Error):self.call('AssignPrivateIpAddresses',NetworkInterfaceId=b,PrivateIpAddresses=[first['PrivateIpAddress']],AllowReassignment=True)

    def test_interface_attributes_attachment_and_portable_state(self):
        instance=self.instance();key=self.eni()['NetworkInterfaceId']
        attachment=self.call('AttachNetworkInterface',NetworkInterfaceId=key,InstanceId=instance['InstanceId'],DeviceIndex=1)['AttachmentId']
        self.call('ModifyNetworkInterfaceAttribute',NetworkInterfaceId=key,Description={'Value':'updated'})
        self.call('ModifyNetworkInterfaceAttribute',NetworkInterfaceId=key,SourceDestCheck={'Value':False})
        self.assertFalse(self.call('DescribeNetworkInterfaceAttribute',NetworkInterfaceId=key,Attribute='sourceDestCheck')['SourceDestCheck']['Value'])
        found=self.call('DescribeNetworkInterfaces',Filters=[{'Name':'attachment.instance-id','Values':[instance['InstanceId']]}])['NetworkInterfaces']
        self.assertEqual(len(found),2)
        self.call('ModifyNetworkInterfaceAttribute',NetworkInterfaceId=key,Attachment={'AttachmentId':attachment,'DeleteOnTermination':True})
        self.engine.tools.import_state(self.engine.tools.export_state())
        self.assertEqual(self.describe(key)['Description'],'updated')
        self.call('TerminateInstances',InstanceIds=[instance['InstanceId']])
        with self.assertRaises(Error):self.describe(key)

    def test_eip_secondary_association_views_transfer_and_detach(self):
        instance=self.instance();key=self.eni()['NetworkInterfaceId']
        attachment=self.call('AttachNetworkInterface',NetworkInterfaceId=key,InstanceId=instance['InstanceId'],DeviceIndex=1)['AttachmentId']
        self.call('AssignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.50'])
        address=self.call('AllocateAddress')['AllocationId']
        association=self.call('AssociateAddress',AllocationId=address,NetworkInterfaceId=key,PrivateIpAddress='10.84.1.50')['AssociationId']
        self.assertEqual(self.describe(key)['PrivateIpAddresses'][1]['Association']['AssociationId'],association)
        self.assertNotIn('PublicIpAddress',self.call('DescribeInstances',InstanceIds=[instance['InstanceId']])['Reservations'][0]['Instances'][0])
        with self.assertRaises(Error):self.call('UnassignPrivateIpAddresses',NetworkInterfaceId=key,PrivateIpAddresses=['10.84.1.50'])
        self.call('DetachNetworkInterface',AttachmentId=attachment)
        data=self.call('DescribeAddresses',AllocationIds=[address])['Addresses'][0]
        self.assertNotIn('InstanceId',data)
        self.call('DisassociateAddress',AssociationId=association)
        self.assertNotIn('Association',self.describe(key)['PrivateIpAddresses'][1])
        self.call('AssociateAddress',AllocationId=address,InstanceId=instance['InstanceId'])
        target=self.instance()
        with self.assertRaises(Error):self.call('AssociateAddress',AllocationId=address,InstanceId=target['InstanceId'],AllowReassociation=False)
        self.call('AssociateAddress',AllocationId=address,InstanceId=target['InstanceId'])
        self.assertNotIn('PublicIpAddress',self.call('DescribeInstances',InstanceIds=[instance['InstanceId']])['Reservations'][0]['Instances'][0])
        self.call('TerminateInstances',InstanceIds=[target['InstanceId']])
        self.assertNotIn('NetworkInterfaceId',self.call('DescribeAddresses',AllocationIds=[address])['Addresses'][0])

    def test_primary_detach_and_cross_zone_are_rejected(self):
        instance=self.instance();primary=instance['NetworkInterfaces'][0]
        with self.assertRaises(Error):self.call('DetachNetworkInterface',AttachmentId=primary['Attachment']['AttachmentId'])
        subnet=self.call('CreateSubnet',VpcId=self.vpc,CidrBlock='10.84.2.0/24',AvailabilityZone='us-east-1b')['Subnet']['SubnetId']
        key=self.call('CreateNetworkInterface',SubnetId=subnet)['NetworkInterface']['NetworkInterfaceId']
        with self.assertRaises(Error):self.call('AttachNetworkInterface',NetworkInterfaceId=key,InstanceId=instance['InstanceId'],DeviceIndex=1)

    def test_network_query_serialization_and_cli(self):
        op,args=query_request(b'Action=ModifyNetworkInterfaceAttribute&NetworkInterfaceId=eni-test&SourceDestCheck.Value=false')
        self.assertEqual(args,{'NetworkInterfaceId':'eni-test','SourceDestCheck':{'Value':False}})
        op,args=query_request(b'Action=AssignPrivateIpAddresses&NetworkInterfaceId=eni-test&PrivateIpAddress.1=10.84.1.20')
        self.assertEqual(args['PrivateIpAddresses'],['10.84.1.20'])
        xml=query_response('DescribeNetworkInterfaceAttribute',{'NetworkInterfaceId':'eni-test','SourceDestCheck':{'Value':False}},'request-test')
        self.assertIn('<sourceDestCheck><value>false</value></sourceDestCheck>',xml)
        cli=parser().parse_args(['ec2','assign-private-ip-addresses','--network-interface-id','eni-test','--private-ip-addresses','10.84.1.20'])
        self.assertEqual(cli.ec2_PrivateIpAddresses,['10.84.1.20'])
