"""Owned Docker namespace firewall and backend-local EIP TCP forwarding.

All kernel filtering occurs in a dedicated helper namespace shared only by its
EC2 guest. Host routes/firewall tables are never modified by this module.
"""
import base64
import copy
import ipaddress
import json
import socket
import socketserver
import subprocess
import threading
import uuid
from urllib.parse import urlsplit

from .engine import Error
from .ec2_packet_policy import snapshot, render, allows, route_allows


class ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Reassociation can close established streams into TIME_WAIT. Reuseaddr
    # permits rebinding those owned endpoints, never stealing a live listener
    # (SO_REUSEPORT is deliberately not enabled).
    allow_reuse_address = True

    def server_close(self):
        for connection, process in list(getattr(self, 'connections', {}).values()):
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            if process.poll() is None: process.terminate()
        super().server_close()


class ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        runtime, spec = self.server.packet_runtime, self.server.packet_spec
        if not runtime.allow_forward(spec, self.client_address[0]):
            return
        try:
            runtime.inspect(spec['instance'])
            process = subprocess.Popen(['docker', 'exec', '-i', runtime.holder(spec['instance']),
                'python3', '-m', 'localcloud.ec2_packet_helper', '--forward', str(spec['port'])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except (OSError, Error):
            return
        self.server.connections[id(self)] = (self.request, process)
        self.request.settimeout(60)
        def incoming():
            try:
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk or not runtime.route_forward(spec, self.client_address[0]): break
                    process.stdin.write(chunk); process.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                try: process.stdin.close()
                except OSError: pass
        worker = threading.Thread(target=incoming, daemon=True); worker.start()
        try:
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk or not runtime.route_forward(spec, self.client_address[0]): break
                self.request.sendall(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try: self.request.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            process.stdout.close()
            self.server.connections.pop(id(self), None)


class PacketRuntime:
    def __init__(self, ec2):
        self.ec2 = ec2
        self.enabled = False
        self._lock = threading.RLock()
        self.policies = {}
        self.forwarders = {}
        self.prepared = {}
        self.ec2.engine.db.execute('CREATE TABLE IF NOT EXISTS ec2_packet_holders (instanceid TEXT PRIMARY KEY, containerid TEXT NOT NULL, owner TEXT NOT NULL)')
        self.ec2.engine.db.commit()

    def holder(self, item):
        return 'localcloud-net-' + item['InstanceId']

    def labels(self, item):
        return {'localcloud.runtime': self.ec2.runtime.owner,
                'localcloud.packet-owner': item['Owner'], 'localcloud.packet-instance': item['InstanceId']}

    def inspect(self, item, absent=False):
        if absent:
            names = self.ec2.docker('ps', '-a', '--filter', 'name=^/' + self.holder(item) + '$', '--format', '{{.Names}}')
            if self.holder(item) not in names.splitlines(): return None
        try:
            obj = json.loads(self.ec2.docker('inspect', self.holder(item)))[0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise Error('RuntimeFailure', 'Packet namespace inspection failed', 503) from None
        labels = obj.get('Config', {}).get('Labels') or {}
        if any(labels.get(key) != value for key, value in self.labels(item).items()):
            raise Error('RuntimeFailure', 'Packet namespace ownership mismatch', 409)
        row = self.ec2.engine.db.execute('SELECT containerid,owner FROM ec2_packet_holders WHERE instanceid=?', (item['InstanceId'],)).fetchone()
        if row is None or row[0] != obj.get('Id') or row[1] != item['Owner']:
            raise Error('RuntimeFailure', 'Packet namespace identity changed', 409)
        if obj.get('HostConfig', {}).get('NetworkMode') == 'host':
            raise Error('RuntimeFailure', 'Host network namespaces are forbidden', 409)
        return obj

    def network(self, policy):
        name = 'localcloud-packet-' + self.ec2.runtime.owner[:8] + '-' + policy['vpc_id']
        try:
            obj = json.loads(self.ec2.docker('network', 'inspect', name))[0]
        except Error:
            self.ec2.docker('network', 'create', '--driver', 'bridge', '--subnet', policy['vpc_cidr'],
                '--label', 'localcloud.runtime=' + self.ec2.runtime.owner,
                '--label', 'localcloud.packet-vpc=' + policy['vpc_id'], name)
            obj = json.loads(self.ec2.docker('network', 'inspect', name))[0]
        labels = obj.get('Labels') or {}
        if labels.get('localcloud.runtime') != self.ec2.runtime.owner or labels.get('localcloud.packet-vpc') != policy['vpc_id'] or not any(c.get('Subnet') == policy['vpc_cidr'] for c in obj.get('IPAM', {}).get('Config', [])):
            raise Error('RuntimeFailure', 'Packet VPC network ownership or CIDR mismatch', 409)
        return name

    def exemptions(self):
        """Only the explicit credential broker destination, not general egress."""
        if not self.ec2.runtime.imds:
            return []
        parsed = urlsplit(self.ec2.engine.endpoint)
        if not parsed.hostname:
            return []
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), family=socket.AF_INET, type=socket.SOCK_STREAM)})
        except OSError:
            raise Error('RuntimeUnavailable', 'Cannot resolve the metadata broker for packet networking', 503) from None
        return [{'address': ip, 'protocol': 'tcp', 'port': parsed.port or (443 if parsed.scheme == 'https' else 80)} for ip in addresses]

    def policy(self, data, item):
        return snapshot(data, item, self.exemptions())

    def prepare(self, data, item):
        if not self.enabled: return None
        if self.ec2.backend != 'docker':
            raise Error('UnsupportedOperation', 'Packet networking currently requires the Docker EC2 executor', 501)
        self.ec2.runtime.require_helper()
        policy = self.policy(data, item)
        name = self.holder(item)
        existing = self.ec2.docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}')
        if name in existing.splitlines():
            raise Error('RuntimeFailure', 'Packet namespace already exists; no existing container was changed', 409)
        nonce = uuid.uuid4().hex
        launch_labels = {**self.labels(item), 'localcloud.packet-launch': nonce}
        labels = [part for key, value in launch_labels.items() for part in ('--label', key + '=' + value)]
        ports = [part for entry in item.get('PortMappings', []) for part in ('--publish', '127.0.0.1:' + str(entry['HostPort']) + ':' + str(entry['ContainerPort']) + '/tcp')]
        identifier = None
        try:
            # Docker may create a stopped container and then report a port-bind
            # failure without returning its ID. The per-launch nonce lets us
            # identify that creation without trusting its reusable name alone.
            identifier = self.ec2.docker('run', '-d', '--name', name, *labels,
                '--network', self.network(policy), '--ip', policy['private_ip'], *ports,
                '--read-only', '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
                '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '96m', '--cpus', '0.25',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=4m,mode=1777',
                '-e', 'LOCALCLOUD_PACKET_RULES', '--entrypoint', 'python3', self.ec2.runtime.helper_image,
                '-m', 'localcloud.ec2_packet_helper', '--hold', environment={'LOCALCLOUD_PACKET_RULES': render(policy)})
            identifier = identifier.strip()
            self.ec2.engine.db.execute('INSERT INTO ec2_packet_holders VALUES (?,?,?)', (item['InstanceId'], identifier, item['Owner']))
            self.prepared[item['InstanceId']] = copy.deepcopy(item)
            self.ec2.docker('exec', name, 'python3', '-c',
                "import pathlib,time\nfor _ in range(100):\n if pathlib.Path('/tmp/localcloud-packet-ready').exists():break\n time.sleep(.05)\nelse:raise SystemExit(1)")
        except Exception as failure:
            try:
                self.cleanup_prepare(item, nonce, identifier)
            except Exception as cleanup_error:
                failure.add_note('Packet namespace launch cleanup failed: ' + str(cleanup_error))
            raise
        item['PacketNetworking'] = True
        with self._lock:
            self.policies[item['InstanceId']] = copy.deepcopy(policy)
        return name

    def cleanup_prepare(self, item, nonce, identifier=None):
        """Remove only this launch's creation, including an unreturned ID."""
        name = self.holder(item)
        names = self.ec2.docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}')
        if name not in names.splitlines():
            return
        obj = json.loads(self.ec2.docker('inspect', name))[0]
        labels = obj.get('Config', {}).get('Labels') or {}
        expected = {**self.labels(item), 'localcloud.packet-launch': nonce}
        actual_id = obj.get('Id')
        if any(labels.get(key) != value for key, value in expected.items()) or not isinstance(actual_id, str) or not actual_id or (identifier is not None and actual_id != identifier):
            raise Error('RuntimeFailure', 'Packet launch cleanup identity changed; replacement was preserved', 409)
        # Remove by immutable Docker ID after inspecting the nonce. A concurrent
        # same-name replacement can therefore never be deleted by this cleanup.
        self.ec2.docker('rm', '-f', actual_id)
        self.ec2.engine.db.execute('DELETE FROM ec2_packet_holders WHERE instanceid=? AND containerid=? AND owner=?',
                                  (item['InstanceId'], actual_id, item['Owner']))
        self.prepared.pop(item['InstanceId'], None)
        with self._lock:
            self.policies.pop(item['InstanceId'], None)

    def apply(self, item, policy):
        obj = self.inspect(item)
        if not obj.get('State', {}).get('Running'):
            if item.get('State', {}).get('Name') in ('pending', 'running'):
                raise Error('RuntimeUnavailable', 'Packet namespace helper is not running; cannot apply policy', 503)
            return
        payload = base64.b64encode(render(policy).encode()).decode()
        self.ec2.docker('exec', self.holder(item), 'python3', '-m', 'localcloud.ec2_packet_helper', '--apply', payload)

    def deny(self, item):
        try:
            obj = self.inspect(item, absent=True)
            if obj and obj.get('State', {}).get('Running'):
                self.ec2.docker('exec', self.holder(item), 'python3', '-m', 'localcloud.ec2_packet_helper', '--deny')
        except Error:
            pass

    def sync_state(self, data):
        if not self.enabled:
            if any(item.get('PacketNetworking') and item['State']['Name'] not in ('terminated', 'shutting-down') for item in data.get('instances', {}).values()):
                raise Error('RuntimeUnavailable', 'Restart with --ec2-networking enforce for packet-enforced EC2 instances', 503)
            return
        for key, item in list(self.prepared.items()):
            if key not in data.get('instances', {}): self.remove(item)
        items = [item for item in data.get('instances', {}).values() if item.get('PacketNetworking') and item.get('State', {}).get('Name') not in ('terminated', 'shutting-down')]
        policies = {item['InstanceId']: self.policy(data, item) for item in items}
        try:
            for item in items:
                self.prepared[item['InstanceId']] = copy.deepcopy(item)
                if self.policies.get(item['InstanceId']) != policies[item['InstanceId']]:
                    self.apply(item, policies[item['InstanceId']])
            with self._lock:
                self.policies = copy.deepcopy(policies)
            self.sync_forwarders(data)
        except Exception:
            for item in items: self.deny(item)
            self.close_forwarders()
            with self._lock: self.policies = {}
            raise

    def forward_specs(self, data):
        wanted = {}
        for address in data.get('ec2', {}).get('addresses', {}).values():
            if not address.get('AssociationId'): continue
            item = data.get('instances', {}).get(address.get('InstanceId'))
            if not item or not item.get('PacketNetworking') or item['State']['Name'] not in ('pending', 'running'):
                continue
            if address.get('PrivateIpAddress') != item['PrivateIpAddress']:
                raise Error('UnsupportedOperation', 'Packet EIP forwarding supports the primary private address only', 501)
            ip = ipaddress.ip_address(address['PublicIp'])
            if ip.version != 4 or not ip.is_loopback:
                raise Error('UnsupportedOperation', 'Packet EIP forwarding only binds local IPv4 loopback addresses', 501)
            for entry in item.get('PortMappings', []):
                key = (str(ip), entry['ContainerPort'])
                wanted[key] = {'instance': {'InstanceId': item['InstanceId'], 'Owner': item['Owner']}, 'port': entry['ContainerPort'], 'association': address['AssociationId']}
        return wanted

    def allow_forward(self, spec, source):
        with self._lock:
            policy = self.policies.get(spec['instance']['InstanceId'])
            return bool(policy and allows(policy, 'ingress', source, protocol='tcp', port=spec['port']))

    def route_forward(self, spec, source):
        with self._lock:
            policy = self.policies.get(spec['instance']['InstanceId'])
            current = any(server.packet_spec.get('association') == spec['association'] for server, _ in self.forwarders.values())
            return bool(current and policy and route_allows(policy, source))

    def sync_forwarders(self, data):
        wanted = self.forward_specs(data)
        for key, (server, thread) in list(self.forwarders.items()):
            if key not in wanted or wanted[key] != server.packet_spec:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                del self.forwarders[key]
        for key, spec in wanted.items():
            if key in self.forwarders: continue
            try:
                server = ForwardServer(key, ForwardHandler)
            except OSError:
                raise Error('RuntimeFailure', 'Local EIP TCP port is already bound or unavailable; no foreign listener was changed', 409) from None
            server.packet_runtime, server.packet_spec = self, spec
            server.connections = {}
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.forwarders[key] = (server, thread)

    def close_forwarders(self):
        for server, thread in self.forwarders.values():
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.forwarders.clear()

    def stop(self, item):
        # Keep the namespace alive while Docker retains a stopped guest's
        # container: reference. No application runs; remove EIP listeners.
        with self._lock:
            self.policies.pop(item['InstanceId'], None)
        for key, (server, thread) in list(self.forwarders.items()):
            if server.packet_spec['instance']['InstanceId'] == item['InstanceId']:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                del self.forwarders[key]

    def start(self, data, item):
        obj = self.inspect(item)
        if not obj.get('State', {}).get('Running'):
            self.ec2.docker('start', self.holder(item))
            self.ec2.docker('exec', self.holder(item), 'python3', '-c',
                "import pathlib,time\nfor _ in range(100):\n if pathlib.Path('/tmp/localcloud-packet-ready').exists():break\n time.sleep(.05)\nelse:raise SystemExit(1)")
        self.apply(item, self.policy(data, item))

    def remove(self, item):
        self.stop(item)
        obj = self.inspect(item, absent=True)
        if obj:
            self.ec2.docker('rm', '-f', self.holder(item))
        self.ec2.engine.db.execute('DELETE FROM ec2_packet_holders WHERE instanceid=?', (item['InstanceId'],))
        self.prepared.pop(item['InstanceId'], None)

    def delete_network(self, vpc_id):
        name = 'localcloud-packet-' + self.ec2.runtime.owner[:8] + '-' + vpc_id
        try: obj = json.loads(self.ec2.docker('network', 'inspect', name))[0]
        except Error: return
        labels = obj.get('Labels') or {}
        if labels.get('localcloud.runtime') != self.ec2.runtime.owner or labels.get('localcloud.packet-vpc') != vpc_id:
            raise Error('RuntimeFailure', 'Packet VPC ownership mismatch', 409)
        self.ec2.docker('network', 'rm', name)

    def close(self):
        self.close_forwarders()
