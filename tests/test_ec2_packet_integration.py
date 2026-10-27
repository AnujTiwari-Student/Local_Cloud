"""Control-plane mutations must reach packet policy or roll back cleanly."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from localcloud.engine import Engine, Error
from localcloud.ec2_packet_policy import snapshot


class PacketIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.tmp.name + '/db')
    def tearDown(self):
        self.engine.close(); self.tmp.cleanup()
    def call(self, op, **args): return self.engine.ec2.call(op, args, 'test')
    def state(self): return json.loads(self.engine.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def test_group_reference_is_scoped_and_never_defaults_to_allow_all(self):
        vpc = self.call('CreateVpc', CidrBlock='10.249.0.0/16')['Vpc']['VpcId']
        subnet = self.call('CreateSubnet', VpcId=vpc, CidrBlock='10.249.1.0/24')['Subnet']['SubnetId']
        app = self.call('CreateSecurityGroup', VpcId=vpc, GroupName='app', Description='app')['GroupId']
        client = self.call('CreateSecurityGroup', VpcId=vpc, GroupName='client', Description='client')['GroupId']
        request = {'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'UserIdGroupPairs':[{'GroupId':client}]}
        self.call('AuthorizeSecurityGroupIngress', GroupId=app, IpPermissions=[request])
        a = self.call('RunInstances', ImageId='ami-local', SubnetId=subnet, SecurityGroupIds=[app])['Instances'][0]
        c = self.call('RunInstances', ImageId='ami-local', SubnetId=subnet, SecurityGroupIds=[client])['Instances'][0]
        state = self.state()
        self.assertEqual(state['ec2']['groups'][app]['IpPermissions'][0]['IpRanges'], [])
        self.assertEqual(snapshot(state, state['instances'][a['InstanceId']])['ingress'][0]['cidr'], c['PrivateIpAddress'] + '/32')
        self.call('RevokeSecurityGroupIngress', GroupId=app, IpPermissions=[request])
        self.assertEqual(self.state()['ec2']['groups'][app]['IpPermissions'], [])

    def test_cross_vpc_reference_rejected_without_changing_policy(self):
        vpc = self.call('CreateVpc', CidrBlock='10.249.0.0/16')['Vpc']['VpcId']
        first = self.call('CreateSecurityGroup', VpcId=vpc, GroupName='first', Description='first')['GroupId']
        second = self.call('CreateSecurityGroup', GroupName='second', Description='second')['GroupId']
        with self.assertRaises(Error):
            self.call('AuthorizeSecurityGroupIngress', GroupId=first, IpPermissions=[{'IpProtocol':'-1','UserIdGroupPairs':[{'GroupId':second}]}])
        self.assertEqual(self.state()['ec2']['groups'][first]['IpPermissions'], [])

    def test_packet_apply_error_rolls_back_state_and_restores_previous_policy(self):
        before = self.state(); seen = []
        def sync(state):
            seen.append(copy.deepcopy(state))
            if len(seen) == 1: raise Error('RuntimeFailure', 'simulated firewall install failure', 503)
        with patch.object(self.engine.ec2.packet_runtime, 'sync_state', side_effect=sync):
            with self.assertRaises(Error):
                with self.engine.transaction() as state: state['queues']['new'] = {'test':'value'}
        self.assertEqual(self.state(), before)
        self.assertIn('new', seen[0]['queues'])
        self.assertEqual(seen[1], before)

    def test_secondary_addresses_explicitly_unsupported_in_packet_policy(self):
        item = self.call('RunInstances', ImageId='ami-local')['Instances'][0]
        data = self.state(); interface = next(iter(data['ec2']['interfaces'].values()))
        interface['PrivateIpAddresses'].append({'PrivateIpAddress':'172.31.0.9','Primary':False})
        with self.assertRaises(Error) as error: snapshot(data, data['instances'][item['InstanceId']])
        self.assertEqual(error.exception.status, 501)
