"""Packet policy must constrain actual flow decisions, not only render names."""
import copy
import unittest

from localcloud.ec2_packet_policy import snapshot, render, render_ipv6_lockdown, route_allows, allows
from localcloud.engine import Error


def permission(protocol='tcp', low=8080, high=8080, cidr='0.0.0.0/0'):
    return {'IpProtocol': protocol, 'FromPort': low, 'ToPort': high,
            'IpRanges': [{'CidrIp': cidr}], 'Ipv6Ranges': [], 'UserIdGroupPairs': [], 'PrefixListIds': []}


class PacketPolicyTests(unittest.TestCase):
    def setUp(self):
        self.instance = {'InstanceId': 'i-one', 'VpcId': 'vpc-one', 'SubnetId': 'subnet-one',
                         'PrivateIpAddress': '10.80.1.10', 'State': {'Name': 'running'},
                         'SecurityGroups': [{'GroupId': 'sg-web'}]}
        self.table = {'RouteTableId': 'rtb-main', 'VpcId': 'vpc-one',
                      'Associations': [{'Main': True}],
                      'Routes': [{'DestinationCidrBlock': '10.80.0.0/16', 'GatewayId': 'local', 'State': 'active'}]}
        self.group = {'GroupId': 'sg-web', 'VpcId': 'vpc-one', 'IpPermissions': [],
                      'IpPermissionsEgress': [permission('-1')]}
        self.state = {'ec2': {'vpcs': {'vpc-one': {'CidrBlock': '10.80.0.0/16'}},
                             'subnets': {'subnet-one': {'CidrBlock': '10.80.1.0/24', 'VpcId': 'vpc-one'}},
                             'route_tables': {'rtb-main': self.table}, 'groups': {'sg-web': self.group},
                             'gateways': {}}, 'instances': {'i-one': self.instance}}

    def policy(self, **kwargs): return snapshot(self.state, self.instance, **kwargs)

    def internet(self):
        self.state['ec2']['gateways']['igw-one'] = {'Attachments': [{'VpcId': 'vpc-one', 'State': 'available'}]}
        self.table['Routes'].append({'DestinationCidrBlock': '0.0.0.0/0', 'GatewayId': 'igw-one', 'State': 'active'})

    def test_default_deny_ingress_and_local_egress_without_internet_route(self):
        policy = self.policy()
        self.assertFalse(allows(policy, 'ingress', '10.80.2.10', port=8080))
        self.assertTrue(allows(policy, 'egress', '10.80.2.10', port=8080))
        self.assertFalse(allows(policy, 'egress', '198.51.100.7', port=443))
        self.group['IpPermissions'] = [permission()]
        policy = self.policy()
        self.assertTrue(allows(policy, 'ingress', '10.80.2.10', port=8080))
        self.assertFalse(allows(policy, 'ingress', '198.51.100.7', port=8080))
        self.assertFalse(allows(policy, 'ingress', '10.80.2.10', port=8081))

    def test_internet_route_requires_attached_gateway_and_removal_blocks(self):
        self.internet(); self.group['IpPermissions'] = [permission()]
        self.assertTrue(allows(self.policy(), 'ingress', '198.51.100.7', port=8080))
        self.assertTrue(allows(self.policy(), 'egress', '198.51.100.7', port=443))
        self.state['ec2']['gateways']['igw-one']['Attachments'] = []
        self.assertFalse(allows(self.policy(), 'ingress', '198.51.100.7', port=8080))
        self.assertFalse(allows(self.policy(), 'egress', '198.51.100.7', port=443))

    def test_longest_prefix_blackhole_overrides_default_and_local(self):
        self.internet()
        self.table['Routes'].append({'DestinationCidrBlock': '198.51.100.0/24', 'GatewayId': 'igw-one', 'State': 'blackhole'})
        self.table['Routes'].append({'DestinationCidrBlock': '10.80.2.0/24', 'GatewayId': 'igw-one', 'State': 'blackhole'})
        policy = self.policy()
        self.assertFalse(route_allows(policy, '198.51.100.7'))
        self.assertFalse(route_allows(policy, '10.80.2.1'))
        self.assertTrue(route_allows(policy, '203.0.113.4'))
        self.assertTrue(route_allows(policy, '10.80.1.20'))
        text = render(policy)
        self.assertLess(text.index('-A LC_ROUTE_OUT -d 198.51.100.0/24 -j DROP'), text.index('-A LC_ROUTE_OUT -d 0.0.0.0/0 -j LC_SG_OUT'))

    def test_explicit_subnet_route_table_wins_over_main(self):
        self.internet()
        self.state['ec2']['route_tables']['rtb-private'] = {'VpcId': 'vpc-one', 'RouteTableId': 'rtb-private',
            'Associations': [{'SubnetId': 'subnet-one'}],
            'Routes': [{'DestinationCidrBlock': '10.80.0.0/16', 'GatewayId': 'local'}]}
        policy = self.policy()
        self.assertEqual(policy['route_table_id'], 'rtb-private')
        self.assertFalse(route_allows(policy, '203.0.113.4'))
        del self.state['ec2']['route_tables']['rtb-private']
        self.assertTrue(route_allows(self.policy(), '203.0.113.4'))

    def test_group_union_peer_group_expansion_and_active_peer_removal(self):
        self.state['ec2']['groups']['sg-clients'] = {'VpcId': 'vpc-one', 'IpPermissions': [], 'IpPermissionsEgress': []}
        self.state['instances']['i-client'] = {**self.instance, 'InstanceId': 'i-client', 'PrivateIpAddress': '10.80.1.11', 'SecurityGroups': [{'GroupId': 'sg-clients'}]}
        self.group['IpPermissions'] = [{'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080, 'UserIdGroupPairs': [{'GroupId': 'sg-clients'}]}]
        policy = self.policy()
        self.assertTrue(allows(policy, 'ingress', '10.80.1.11', port=8080))
        self.assertFalse(allows(policy, 'ingress', '10.80.1.12', port=8080))
        self.state['instances']['i-client']['State'] = {'Name': 'terminated'}
        self.assertFalse(allows(self.policy(), 'ingress', '10.80.1.11', port=8080))

    def test_tcp_udp_icmp_and_all_protocols_have_distinct_rules(self):
        self.group['IpPermissions'] = [permission('udp', 53, 54, '10.80.0.0/16'), permission('icmp', 8, 0, '10.80.0.0/16')]
        policy = self.policy()
        self.assertTrue(allows(policy, 'ingress', '10.80.1.11', protocol='udp', port=54))
        self.assertFalse(allows(policy, 'ingress', '10.80.1.11', protocol='tcp', port=54))
        self.assertTrue(allows(policy, 'ingress', '10.80.1.11', protocol='icmp', icmp_type=8, icmp_code=0))
        self.assertFalse(allows(policy, 'ingress', '10.80.1.11', protocol='icmp', icmp_type=3, icmp_code=0))
        self.group['IpPermissions'].append(permission('-1', cidr='10.80.1.20/32'))
        self.assertTrue(allows(self.policy(), 'ingress', '10.80.1.20', protocol='icmp', icmp_type=3, icmp_code=1))
        text = render(policy)
        self.assertIn('-p udp --dport 53:54', text); self.assertIn('-p icmp --icmp-type 8/0', text)

    def test_route_gates_precede_stateful_security_group_and_fail_closed(self):
        text = render(self.policy())
        self.assertIn(':INPUT DROP', text); self.assertIn(':OUTPUT DROP', text); self.assertIn(':FORWARD DROP', text)
        self.assertIn('-A INPUT -j LC_ROUTE_IN', text)
        self.assertIn('-A LC_SG_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT', text)
        self.assertNotIn('-A INPUT -m conntrack --ctstate ESTABLISHED', text)
        self.assertIn('-A LC_SG_IN -j DROP', text)
        self.assertIn('-A LC_SG_OUT -j DROP', text)
        self.assertEqual(text.count('COMMIT'), 1)

    def test_control_exemptions_are_exact_and_do_not_affect_new_flow_checks(self):
        policy = self.policy(exemptions=[{'address': '192.0.2.1', 'protocol': 'tcp', 'port': 4566}])
        text = render(policy)
        self.assertIn('-A OUTPUT -d 192.0.2.1/32 -p tcp --dport 4566 -j ACCEPT', text)
        self.assertIn('-A INPUT -s 192.0.2.1/32 -p tcp --sport 4566 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT', text)
        self.assertNotIn('-A INPUT -s 192.0.2.1/32 -j ACCEPT', text)
        self.assertFalse(allows(policy, 'ingress', '192.0.2.1', port=8080))
        self.assertFalse(allows(policy, 'egress', '192.0.2.1', port=4566))
        with self.assertRaises(Error): self.policy(exemptions=[{'address': '192.0.2.0/24', 'protocol': 'tcp', 'port': 4566}])

    def test_unsupported_targets_ipv6_prefix_lists_and_bad_ports_are_explicit(self):
        cases = [permission('sctp'), permission('icmp', 256, 0), permission('tcp', 90, 80),
                 {**permission(), 'Ipv6Ranges': [{'CidrIpv6': '::/0'}]},
                 {**permission(), 'PrefixListIds': [{'PrefixListId': 'pl-x'}]},
                 {**permission(), 'UserIdGroupPairs': [{'GroupId': 'sg-missing'}]}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(Error):
                self.group['IpPermissions'] = [value]; self.policy()
        self.group['IpPermissions'] = []
        self.table['Routes'].append({'DestinationCidrBlock': '0.0.0.0/0', 'NetworkInterfaceId': 'eni-router'})
        with self.assertRaises(Error) as error: self.policy()
        self.assertEqual(error.exception.status, 501)

    def test_snapshot_is_deterministic_and_does_not_mutate_input(self):
        self.group['IpPermissions'] = [permission(), permission(), permission('tcp', 80, 80, '10.80.0.0/16')]
        before = copy.deepcopy(self.state)
        first = self.policy()
        self.group['IpPermissions'].reverse()
        second = self.policy()
        self.assertEqual(first, second)
        self.group['IpPermissions'].reverse()
        self.assertEqual(self.state, before)
        self.assertEqual(len(first['ingress']), 2)

    def test_ipv6_is_locked_down_and_cannot_bypass_ipv4_policy(self):
        text = render_ipv6_lockdown()
        self.assertIn(':INPUT DROP', text); self.assertIn(':OUTPUT DROP', text)
        self.assertIn('-A INPUT -i lo -j ACCEPT', text)
        self.assertNotIn('eth0 -j ACCEPT', text)
        with self.assertRaises(Error): route_allows(self.policy(), '::1')

    def test_missing_route_table_or_foreign_group_fails_closed(self):
        self.state['ec2']['route_tables'].clear()
        with self.assertRaises(Error): self.policy()
        self.state['ec2']['route_tables']['rtb-main'] = self.table
        self.group['VpcId'] = 'vpc-foreign'
        with self.assertRaises(Error): self.policy()
