"""Owned Docker runtime resources for EC2 networking, storage and IMDS."""
import copy
import json
import uuid
from .engine import Error


class Runtime:
    def __init__(self, ec2):
        self.ec2 = ec2
        # Runtime ownership is private SQL metadata, never imported from fixtures.
        db = ec2.engine.db
        db.execute('CREATE TABLE IF NOT EXISTS ec2_runtime (id INTEGER PRIMARY KEY, owner TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO ec2_runtime VALUES (1, ?)', (uuid.uuid4().hex,))
        db.commit()
        self.owner = db.execute('SELECT owner FROM ec2_runtime WHERE id=1').fetchone()[0]
        self.helper_image = None
        self.imds = False
        self.guest_mode = False

    def start_guest(self, instance):
        if not instance.get('GuestMode') or not instance.get('SshPublicKey'):
            return
        name = self.ec2.container(instance)
        script = 'set -eu; test -x /usr/sbin/sshd; mkdir -p /root/.ssh /run/sshd; chmod 700 /root/.ssh; printf "%s\\n" "$1" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys; ssh-keygen -A; /usr/sbin/sshd -oPermitRootLogin=prohibit-password -oPasswordAuthentication=no -oKbdInteractiveAuthentication=no -oUseDNS=no'
        self.ec2.docker('exec', '--user', '0:0', name, '/bin/sh', '-c', script, 'localcloud-ssh', instance['SshPublicKey'])

    def label(self):
        return 'localcloud.runtime=' + self.owner

    def owned(self, kind, name, absent=False):
        try:
            labels = json.loads(self.ec2.docker(kind, 'inspect', '--format', '{{json .Labels}}' if kind in ('network', 'volume') else '{{json .Config.Labels}}', name))
        except Error:
            if absent:
                return False
            raise
        if not isinstance(labels, dict) or labels.get('localcloud.runtime') != self.owner:
            raise Error('RuntimeFailure', 'Runtime resource ownership mismatch', 409)
        return True

    def network(self, vpc):
        name = 'localcloud-' + vpc
        try:
            self.ec2.docker('network', 'inspect', name)
        except Error:
            self.ec2.docker('network', 'create', '--label', self.label(), name)
        self.owned('network', name)
        return name

    def delete_network(self, vpc):
        if not self.ec2.images:
            return
        name = 'localcloud-' + vpc
        if self.owned('network', name, absent=True):
            self.ec2.docker('network', 'rm', name)

    def volume_name(self, key):
        return 'localcloud-' + self.owner[:12] + '-' + key

    def require_helper(self):
        if not self.helper_image:
            raise Error('RuntimeUnavailable', 'Set --ec2-helper-image to a built LocalCloud image for Docker volumes/IMDS', 503)

    def create_volume(self, key, source=None):
        if not self.ec2.images:
            return
        self.require_helper()
        name = self.volume_name(key)
        self.ec2.docker('volume', 'create', '--label', self.label(), name)
        self.owned('volume', name)
        try:
            mounts = ['--mount', 'type=volume,source=' + name + ',target=/target']
            script = 'import os'
            if source:
                source_name = self.volume_name(source)
                self.owned('volume', source_name)
                mounts += ['--mount', 'type=volume,source=' + source_name + ',target=/source,readonly']
                script += '; import shutil; shutil.copytree("/source","/target",dirs_exist_ok=True,symlinks=True); os.chown("/target",65534,65534); [(os.chown(os.path.join(p,n),65534,65534,follow_symlinks=False)) for p,ds,fs in os.walk("/target") for n in ds+fs]'
            else:
                script += '; os.chown("/target",65534,65534)'
            self.ec2.docker('run', '--rm', '--network', 'none', '--read-only', '--pids-limit', '64', '--memory', '128m', '--cpus', '1',
                            '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
                            '--user', '0:0', *mounts, '--entrypoint', 'python3', self.helper_image, '-c', script)
        except Error:
            self.ec2.docker('volume', 'rm', name)
            raise

    def delete_volume(self, key):
        if not self.ec2.images:
            return
        name = self.volume_name(key)
        self.owned('volume', name)
        self.ec2.docker('volume', 'rm', name)

    def mount_args(self, mappings):
        args = []
        for mapping in mappings:
            key = mapping['Ebs']['VolumeId']
            name = self.volume_name(key)
            self.owned('volume', name)
            # /dev names represent filesystem mounts under /mnt/ebs, not fake raw devices.
            target = '/mnt/ebs/' + mapping['DeviceName'][5:] if mapping['DeviceName'].startswith('/dev/') else mapping['DeviceName']
            args += ['--mount', 'type=volume,source=' + name + ',target=' + target]
        return args

    def check_reconfigure(self, instance):
        if instance['Backend'] == 'docker':
            name = self.ec2.container(instance)
            if instance['State']['Name'] != 'stopped' or self.ec2.docker('inspect', '--format', '{{.State.Running}}', name) == 'true':
                raise Error('IncorrectInstanceState', 'Stop the Docker instance before changing volume attachments')

    def reconfigure(self, instance, mappings):
        if instance['Backend'] != 'docker':
            return
        self.check_reconfigure(instance)
        # Keep the old stopped container until replacement creation succeeds.
        name = self.ec2.container(instance)
        backup = name + '-backup-' + uuid.uuid4().hex[:8]
        previous_root = instance.get('RootImage')
        root_image = None
        if instance.get('GuestMode'):
            root_image = 'localcloud-ec2-root:' + instance['InstanceId'] + '-' + uuid.uuid4().hex[:8]
            self.ec2.docker('commit', '--change', 'LABEL ' + self.label(), name, root_image)
            self.owned('image', root_image)
            instance['RootImage'] = root_image
        self.imds_action(instance, 'rm')
        self.ec2.docker('rename', name, backup)
        try:
            new_id = self.ec2.docker('create', *self.ec2.container_args(instance, mappings))
        except Error:
            # A failed create may still leave a container. Remove only our owner label.
            try:
                self.ec2.container(instance)
                self.ec2.docker('rm', '-f', name)
            except Error:
                pass
            self.ec2.docker('rename', backup, name)
            if root_image:
                self.ec2.docker('image', 'rm', root_image)
                if previous_root:
                    instance['RootImage'] = previous_root
                else:
                    instance.pop('RootImage', None)
            raise
        self.ec2.docker('rm', backup)
        instance['ContainerId'] = new_id
        if previous_root:
            self.owned('image', previous_root)
            self.ec2.docker('image', 'rm', previous_root)

    def delete_root_image(self, instance):
        root = instance.get('RootImage')
        if root:
            self.owned('image', root)
            self.ec2.docker('image', 'rm', root)

    def imds_name(self, instance):
        return 'localcloud-imds-' + instance['InstanceId']

    def imds_action(self, instance, action):
        if not instance.get('ImdsEnabled'):
            return
        name = self.imds_name(instance)
        if self.owned('container', name, absent=True):
            self.ec2.docker(*({'rm': ['rm', '-f', name], 'stop': ['stop', '-t', '2', name]}[action]))

    def start_imds(self, instance):
        if not instance.get('ImdsEnabled'):
            return
        self.require_helper()
        name = self.imds_name(instance)
        if self.owned('container', name, absent=True):
            self.ec2.docker('start', name)
            self.wait_imds(name)
            return
        payload = {k: copy.deepcopy(instance.get(k)) for k in ('InstanceId', 'ImageId', 'InstanceType', 'PrivateIpAddress', 'PrivateDnsName', 'PublicIpAddress', 'Placement', 'UserData', 'MetadataOptions', 'ReservationId', 'AmiLaunchIndex')}
        self.ec2.docker('run', '-d', '--name', name, '--label', self.label(), '--label', 'localcloud.instance=' + instance['InstanceId'],
                        '--network', 'container:localcloud-' + instance['InstanceId'], '--read-only', '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
                        '--security-opt', 'no-new-privileges', '--pids-limit', '32', '--memory', '64m', '--cpus', '0.25',
                        '-e', 'LOCALCLOUD_IMDS_DATA=' + json.dumps(payload), '--entrypoint', 'python3', self.helper_image, '-m', 'localcloud.ec2_metadata')
        self.wait_imds(name)

    def wait_imds(self, name):
        script = "import socket,time\nfor _ in range(50):\n try:\n  socket.create_connection(('169.254.169.254',80),timeout=.1).close();break\n except OSError:time.sleep(.1)\nelse:raise SystemExit(1)"
        self.ec2.docker('exec', name, 'python3', '-c', script)
