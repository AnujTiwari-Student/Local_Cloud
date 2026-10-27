"""Owned packet namespace lifecycle and fail-closed rollback contracts."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from localcloud.engine import Engine, Error
from localcloud.ec2_packet_runtime import ForwardServer
import test_ec2_packet_policy as policy_tests
from test_ec2_packet_policy import permission


class Docker:
    def __init__(self):
        self.objects, self.networks, self.calls = {}, {}, []
        self.fail_apply = False
    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == 'ps': return '\n'.join(self.objects)
        if args[:2] == ('network', 'inspect'):
            if args[2] not in self.networks: raise Error('RuntimeFailure', 'missing', 503)
            return json.dumps([self.networks[args[2]]])
        if args[:2] == ('network', 'create'):
            name = args[-1]
            labels = dict(args[i+1].split('=',1) for i,v in enumerate(args) if v == '--label')
            self.networks[name] = {'Labels': labels, 'IPAM': {'Config': [{'Subnet': args[args.index('--subnet')+1]}]}}
            return 'network-id'
        if args[:2] == ('network','rm'):
            self.networks.pop(args[2]); return ''
        if args[0] == 'run':
            name = args[args.index('--name')+1]
            labels = dict(args[i+1].split('=',1) for i,v in enumerate(args) if v == '--label')
            self.objects[name] = {'Id':'container-'+name, 'Config': {'Labels':labels},
                'HostConfig': {'NetworkMode': args[args.index('--network')+1]}, 'State': {'Running':True}}
            return 'container-'+name
        if args[0] == 'inspect':
            if args[1] not in self.objects: raise Error('RuntimeFailure','missing',503)
            return json.dumps([self.objects[args[1]]])
        if args[:2] == ('rm','-f'):
            key = args[2] if args[2] in self.objects else next(key for key,obj in self.objects.items() if obj['Id']==args[2])
            self.objects.pop(key); return ''
        if args[0] == 'exec':
            if '--apply' in args and self.fail_apply:
                self.fail_apply = False
                raise Error('RuntimeFailure','apply-failed',503)
            return ''
        if args[0] == 'start': self.objects[args[1]]['State']['Running'] = True; return ''
        raise AssertionError(args)


class PacketRuntimeTests(unittest.TestCase):
    def setUp(self):
        base = policy_tests.PacketPolicyTests(); base.setUp()
        self.item, self.state = base.instance, base.state
        self.item.update(Owner='test-owner', Backend='docker', PortMappings=[{'HostPort':18080,'ContainerPort':8080}])
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name)/'db'), ec2_images={'ami':'guest'})
        self.runtime = self.engine.ec2.packet_runtime
        self.runtime.enabled = True
        self.engine.ec2.runtime.helper_image = 'helper:packet'
        self.docker = Docker(); self.engine.ec2.docker = self.docker
    def tearDown(self): self.engine.close(); self.tmp.cleanup()

    def test_prepare_installs_before_guest_and_keeps_filter_out_of_host_namespace(self):
        holder = self.runtime.prepare(self.state,self.item)
        run, kwargs = next((args,kw) for args,kw in self.docker.calls if args[0]=='run')
        self.assertEqual(run[run.index('--ip')+1], self.item['PrivateIpAddress'])
        self.assertEqual(run[run.index('--cap-add')+1], 'NET_ADMIN')
        self.assertNotIn('host',run)
        self.assertEqual(run[run.index('--publish')+1], '127.0.0.1:18080:8080/tcp')
        self.assertIn(':INPUT DROP',kwargs['environment']['LOCALCLOUD_PACKET_RULES'])
        self.assertTrue(self.item['PacketNetworking'])
        self.assertEqual(holder,self.runtime.holder(self.item))
        self.assertTrue(any('localcloud-packet-ready' in str(args) for args,_ in self.docker.calls))

    def test_failed_update_invalidates_cache_and_rollback_reinstalls_old_rules(self):
        self.runtime.prepare(self.state,self.item)
        before = copy.deepcopy(self.state)
        self.state['ec2']['groups']['sg-web']['IpPermissions']=[permission()]
        self.docker.fail_apply = True
        with self.assertRaises(Error): self.runtime.sync_state(self.state)
        self.assertEqual(self.runtime.policies,{})
        self.assertTrue(any('--deny' in args for args,_ in self.docker.calls))
        applies = sum('--apply' in args for args,_ in self.docker.calls)
        self.runtime.sync_state(before)
        self.assertGreater(sum('--apply' in args for args,_ in self.docker.calls),applies)
        self.assertEqual(self.runtime.policies[self.item['InstanceId']]['ingress'],[])

    def test_rollback_of_new_instance_removes_only_its_prepared_holder(self):
        self.runtime.prepare(self.state,self.item)
        self.runtime.sync_state({'instances':{}})
        self.assertFalse(self.docker.objects)
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM ec2_packet_holders').fetchone()[0],0)

    def test_foreign_identity_is_never_deleted(self):
        holder = self.runtime.prepare(self.state,self.item)
        self.docker.objects[holder]['Id']='foreign-replacement'
        with self.assertRaises(Error) as caught: self.runtime.remove(self.item)
        self.assertEqual(caught.exception.status,409)
        self.assertIn(holder,self.docker.objects)

    def test_packet_records_require_explicit_enforcement_after_restart(self):
        self.runtime.prepare(self.state,self.item); self.runtime.enabled=False
        with self.assertRaises(Error) as caught: self.runtime.sync_state(self.state)
        self.assertEqual(caught.exception.code,'RuntimeUnavailable')

    def test_eip_binding_only_explicit_primary_tcp_ports_and_original_client_policy(self):
        self.runtime.prepare(self.state,self.item)
        self.state['ec2']['addresses']={'eip-one':{'PublicIp':'127.240.0.1','AssociationId':'assoc-one',
            'InstanceId':self.item['InstanceId'],'PrivateIpAddress':self.item['PrivateIpAddress']}}
        specs=self.runtime.forward_specs(self.state)
        self.assertEqual(set(specs),{('127.240.0.1',8080)})
        spec=specs[('127.240.0.1',8080)]
        self.assertFalse(self.runtime.allow_forward(spec,'127.0.0.1'))
        policy=self.runtime.policies[self.item['InstanceId']]
        policy['routes'].append({'cidr':'0.0.0.0/0','allow':True,'target':'igw-one'})
        policy['ingress']=[{'protocol':'tcp','cidr':'127.0.0.0/8','from_port':8080,'to_port':8080}]
        self.assertTrue(self.runtime.allow_forward(spec,'127.0.0.1'))
        self.assertFalse(self.runtime.allow_forward(spec,'198.51.100.1'))
        self.state['ec2']['addresses']['eip-one']['PrivateIpAddress']='10.80.1.11'
        with self.assertRaises(Error): self.runtime.forward_specs(self.state)

    def test_listener_retirement_closes_existing_stream_and_owned_relay(self):
        server=object.__new__(ForwardServer)
        connection=Mock(); process=Mock(); process.poll.return_value=None
        server.connections={1:(connection,process)}; server.socket=Mock()
        server.server_close()
        connection.shutdown.assert_called_once(); process.terminate.assert_called_once()
        server.socket.close.assert_called_once()

    def test_instance_display_changes_do_not_replace_eip_listener_identity(self):
        self.runtime.prepare(self.state,self.item)
        self.state['ec2']['addresses']={'one':{'PublicIp':'127.240.0.1','AssociationId':'assoc-one',
            'InstanceId':self.item['InstanceId'],'PrivateIpAddress':self.item['PrivateIpAddress']}}
        original=self.runtime.forward_specs(self.state)
        self.item['Tags']=[{'Key':'Name','Value':'renamed'}]
        self.item['ConsoleOutput']='changed'
        self.assertEqual(self.runtime.forward_specs(self.state),original)

    def test_withdrawn_route_blocks_an_existing_eip_relay(self):
        self.runtime.prepare(self.state,self.item)
        policy=self.runtime.policies[self.item['InstanceId']]
        policy['routes'].append({'cidr':'0.0.0.0/0','allow':True,'target':'igw-one'})
        spec={'instance':self.item,'association':'assoc-one','port':8080}
        self.runtime.forwarders[('127.240.0.1',8080)]=(SimpleNamespace(packet_spec=spec),None)
        self.assertTrue(self.runtime.route_forward(spec,'127.0.0.1'))
        policy['routes']=[r for r in policy['routes'] if r['cidr']!='0.0.0.0/0']
        self.assertFalse(self.runtime.route_forward(spec,'127.0.0.1'))
        self.runtime.forwarders.clear()

    def test_daemon_inspection_error_does_not_erase_holder_identity(self):
        self.runtime.prepare(self.state,self.item)
        delegate=self.engine.ec2.docker
        def broken(*args,**kwargs):
            if args[0]=='inspect':raise Error('RuntimeUnavailable','daemon unavailable',503)
            return delegate(*args,**kwargs)
        self.engine.ec2.docker=broken
        with self.assertRaises(Error):self.runtime.remove(self.item)
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM ec2_packet_holders').fetchone()[0],1)

    def test_foreign_eip_listener_bind_failure_is_reported_without_closing_it(self):
        self.runtime.prepare(self.state,self.item)
        self.state['ec2']['addresses']={'one':{'PublicIp':'127.240.0.1','AssociationId':'assoc-one',
            'InstanceId':self.item['InstanceId'],'PrivateIpAddress':self.item['PrivateIpAddress']}}
        with patch('localcloud.ec2_packet_runtime.ForwardServer',side_effect=OSError('address in use')):
            with self.assertRaises(Error) as caught:self.runtime.sync_forwarders(self.state)
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(self.runtime.forwarders,{})

    def test_dead_helper_cannot_report_successful_live_policy_update(self):
        holder=self.runtime.prepare(self.state,self.item)
        self.docker.objects[holder]['State']['Running']=False
        self.state['ec2']['groups']['sg-web']['IpPermissions']=[permission()]
        with self.assertRaises(Error) as caught:self.runtime.sync_state(self.state)
        self.assertEqual(caught.exception.code,'RuntimeUnavailable')
        self.assertEqual(self.runtime.policies,{})

    def test_restart_waits_for_holder_initialization_before_restoring_current_policy(self):
        holder=self.runtime.prepare(self.state,self.item)
        self.docker.objects[holder]['State']['Running']=False
        self.item['State']={'Name':'stopped'}
        self.runtime.start(self.state,self.item)
        calls=[args for args,_ in self.docker.calls]
        start_index=next(i for i,args in enumerate(calls) if args[0]=='start')
        self.assertIn('localcloud-packet-ready',str(calls[start_index+1]))
        self.assertIn('--apply',calls[start_index+3])

    def test_docker_run_failure_cleans_stopped_holder_without_returned_id(self):
        delegate=self.docker
        def failed_run(*args,**kwargs):
            result=delegate(*args,**kwargs)
            if args[0]=='run':
                delegate.objects[self.runtime.holder(self.item)]['State']['Running']=False
                raise Error('RuntimeFailure','occupied publish port',503)
            return result
        self.engine.ec2.docker=failed_run
        with self.assertRaises(Error):self.runtime.prepare(self.state,self.item)
        self.assertFalse(self.docker.objects)
        self.assertNotIn('PacketNetworking',self.item)
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM ec2_packet_holders').fetchone()[0],0)
        removals=[args for args,_ in self.docker.calls if args[:2]==('rm','-f')]
        self.assertEqual(removals,[('rm','-f','container-'+self.runtime.holder(self.item))])

    def test_prepare_failure_preserves_same_name_nonce_mismatched_replacement(self):
        delegate=self.docker
        def replaced_run(*args,**kwargs):
            result=delegate(*args,**kwargs)
            if args[0]=='run':
                obj=delegate.objects[self.runtime.holder(self.item)]
                obj['Config']['Labels']['localcloud.packet-launch']='foreign-launch'
                obj['Id']='foreign-replacement'
                raise Error('RuntimeFailure','launch failed',503)
            return result
        self.engine.ec2.docker=replaced_run
        with self.assertRaises(Error) as caught:self.runtime.prepare(self.state,self.item)
        self.assertEqual(self.docker.objects[self.runtime.holder(self.item)]['Id'],'foreign-replacement')
        self.assertFalse(any(args[:2]==('rm','-f') for args,_ in self.docker.calls))
        self.assertIn('replacement was preserved',str(caught.exception.__notes__))

    def test_sql_recording_failure_removes_only_newly_created_holder(self):
        database=self.engine.db
        class FailedInsert:
            def execute(self,query,*args):
                if query.startswith('INSERT INTO ec2_packet_holders'):raise RuntimeError('database write failed')
                return database.execute(query,*args)
        self.engine.db=FailedInsert()
        try:
            with self.assertRaises(RuntimeError):self.runtime.prepare(self.state,self.item)
            self.assertFalse(self.docker.objects)
            self.assertEqual(database.execute('SELECT count(*) FROM ec2_packet_holders').fetchone()[0],0)
        finally:self.engine.db=database

    def test_external_backend_exemption_is_exact_host_port(self):
        self.engine.ec2.runtime.imds=True; self.engine.endpoint='http://backend.test:4567'
        with patch('socket.getaddrinfo',return_value=[(None,None,None,None,('192.0.2.4',4567))]):
            exemptions=self.runtime.exemptions()
        self.assertEqual(exemptions,[{'address':'192.0.2.4','protocol':'tcp','port':4567}])
