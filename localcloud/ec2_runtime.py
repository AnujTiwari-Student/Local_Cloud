"""Owned Docker runtime resources for EC2 networking, storage and IMDS."""
import copy
from contextlib import contextmanager
import json
import threading
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
        from .ec2_storage_journal import StorageJournal
        self.storage_journal = StorageJournal(self)
        self.storage_context = threading.local()

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
        if self.ec2.kubernetes is not None:
            return
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

    def create_volume(self, key, source=None, size=1):
        if self.ec2.kubernetes is not None:
            return self.ec2.kubernetes.create_volume(key, source, size=size)
        if not self.ec2.images:
            return
        self.require_helper()
        name = self.volume_name(key)
        # Docker volume create is idempotent: never initialize or delete an
        # already existing destination, even if its runtime owner matches.
        existing = self.ec2.docker('volume', 'ls', '--filter', 'name=' + name, '--format', '{{.Name}}')
        if name in existing.splitlines():
            raise Error('RuntimeFailure', 'Destination storage volume already exists', 409)
        job = getattr(self.storage_context, 'job', None) or self.storage_journal.begin(key, parent=getattr(self.storage_context, 'group', None))
        operation = job['operation']
        operation_label = 'localcloud.storage-operation=' + operation
        helper = job['helper']

        def matches(kind, resource):
            output = self.ec2.docker(kind, 'inspect', '--format',
                                    '{{json .Labels}}' if kind == 'volume' else '{{json .Config.Labels}}', resource)
            labels = json.loads(output)
            return (isinstance(labels, dict) and labels.get('localcloud.runtime') == self.owner
                    and labels.get('localcloud.storage-operation') == operation)

        try:
            self.ec2.docker('volume', 'create', '--label', self.label(), '--label', operation_label, name)
            if not matches('volume', name):
                raise Error('RuntimeFailure', 'Destination storage ownership mismatch', 409)
            mounts = ['--mount', 'type=volume,source=' + name + ',target=/target']
            script = 'import os'
            if source:
                source_name = self.volume_name(source)
                self.owned('volume', source_name)
                mounts += ['--mount', 'type=volume,source=' + source_name + ',target=/source,readonly']
                script += '; import shutil; shutil.copytree("/source","/target",dirs_exist_ok=True,symlinks=True); os.chown("/target",65534,65534); [(os.chown(os.path.join(p,n),65534,65534,follow_symlinks=False)) for p,ds,fs in os.walk("/target") for n in ds+fs]'
            else:
                script += '; os.chown("/target",65534,65534)'
            self.ec2.docker('run', '--rm', '--name', helper, '--label', self.label(), '--label', operation_label,
                            '--network', 'none', '--read-only', '--pids-limit', '64', '--memory', '128m', '--cpus', '1',
                            '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
                            '--user', '0:0', *mounts, '--entrypoint', 'python3', self.helper_image, '-c', script)
        except (Error, ValueError) as failure:
            # Killing a timed-out Docker CLI does not stop its daemon-side
            # container. Remove that helper before trying to remove its mount.
            # A nonce binds cleanup to this operation, not merely the database.
            for kind, resource in (('container', helper), ('volume', name)):
                try:
                    if matches(kind, resource):
                        if kind == 'container':
                            self.ec2.docker('container', 'rm', '-f', resource)
                        else:
                            self.ec2.docker('volume', 'rm', resource)
                except (Error, ValueError) as cleanup_error:
                    failure.add_note('Storage cleanup could not verify or remove ' + kind + ': ' + str(cleanup_error))
            raise

    def snapshot_volume(self, key, source, instances=()):
        """Copy files while attached guests are quiescent, restoring pause state.

        Pausing writers gives a stable file tree, not application transactions
        or an atomic raw-block snapshot. Preexisting pauses belong to callers.
        """
        if self.ec2.kubernetes is not None:
            raise Error('UnsupportedOperation', 'Kubernetes PVC snapshots require a supported CSI snapshot backend', 501)
        if not self.ec2.images:
            return self.create_volume(key, source)
        job = self.storage_journal.begin(key, parent=getattr(self.storage_context, 'group', None))
        previous_job = getattr(self.storage_context, 'job', None)
        self.storage_context.job = job
        paused = []
        created = False
        failure = None
        try:
            self._pause_snapshot_writers(job, instances, paused)
            self.create_volume(key, source)
            created = True
        except (Error, ValueError) as error:
            failure = error
        finally:
            self.storage_context.job = previous_job
            failure = self._resume_snapshot_writers(job, paused, failure)
        if failure is not None:
            if created:
                try:
                    self.delete_volume(key)
                except Error as error:
                    failure.add_note('Failed snapshot cleanup: ' + str(error))
            raise failure

    def _pause_snapshot_writers(self, job, instances, paused):
        seen = set()
        for instance in instances:
            if instance.get('Backend') != 'docker':
                continue
            name = self.ec2.container(instance)
            if name in seen:
                continue
            seen.add(name)
            state = json.loads(self.ec2.docker('inspect', '--format', '{{json .State}}', name))
            if state.get('Running') and not state.get('Paused'):
                actual_id = self.ec2.docker('inspect', '--format', '{{.Id}}', name)
                if instance.get('ContainerId') and instance['ContainerId'] != actual_id:
                    raise Error('RuntimeFailure', 'Guest identity changed before snapshot pause', 409)
                entry = self.storage_journal.guest(job, {**instance, 'ContainerId': actual_id}, name)
                # A timed-out CLI may have applied the pause already.
                paused.append((instance, name, entry))
                self.ec2.docker('pause', name)

    def _resume_snapshot_writers(self, job, paused, failure):
        for instance, name, entry in reversed(paused):
            try:
                self.ec2.container(instance)
                actual_id = self.ec2.docker('inspect', '--format', '{{.Id}}', name)
                if actual_id != entry['container_id']:
                    raise Error('RuntimeFailure', 'Guest identity changed before snapshot resume', 409)
                self.ec2.docker('unpause', name)
                self.storage_journal.resumed(job, entry)
            except (Error, ValueError) as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note('Guest resume failed: ' + str(error))
        return failure

    @contextmanager
    def freeze_snapshot_writers(self, instances):
        """Hold one journaled guest pause across a batch of filesystem copies.

        This prevents guest writes between volume copies. It does not flush
        application buffers or coordinate host/external writers.
        """
        if self.ec2.kubernetes is not None:
            raise Error('UnsupportedOperation', 'Kubernetes batch snapshots are not implemented', 501)
        if not self.ec2.images:
            yield
            return
        job = self.storage_journal.begin(None, kind='quiesce')
        previous_group = getattr(self.storage_context, 'group', None)
        self.storage_context.group = job['operation']
        paused = []
        failure = None
        try:
            self._pause_snapshot_writers(job, instances, paused)
            yield
        except BaseException as error:
            failure = error
            raise
        finally:
            self.storage_context.group = previous_group
            resumed_failure = self._resume_snapshot_writers(job, paused, failure)
            if failure is None and resumed_failure is not None:
                raise resumed_failure

    def delete_volume(self, key):
        if self.ec2.kubernetes is not None:
            return self.ec2.kubernetes.delete_volume(key)
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
        if instance['Backend'] == 'kubernetes':
            return self.ec2.kube_runtime(instance).check_reconfigure(instance)
        if instance['Backend'] == 'docker':
            name = self.ec2.container(instance)
            if instance['State']['Name'] != 'stopped' or self.ec2.docker('inspect', '--format', '{{.State.Running}}', name) == 'true':
                raise Error('IncorrectInstanceState', 'Stop the Docker instance before changing volume attachments')

    def reconfigure(self, instance, mappings):
        if instance['Backend'] == 'kubernetes':
            return self.ec2.kube_runtime(instance).reconfigure(instance, mappings)
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

    def start_imds(self, instance, seed=None):
        if not instance.get('ImdsEnabled'):
            return
        self.require_helper()
        name = self.imds_name(instance)
        if self.owned('container', name, absent=True):
            self.ec2.docker('start', name)
            self.wait_imds(name)
            return
        payload = {k: copy.deepcopy(instance.get(k)) for k in ('InstanceId', 'ImageId', 'InstanceType', 'PrivateIpAddress', 'PrivateDnsName', 'PublicIpAddress', 'Placement', 'UserData', 'MetadataOptions', 'ReservationId', 'AmiLaunchIndex')}
        from .ec2_profiles import broker_environment
        environment = broker_environment(self.ec2, instance, seed)
        private_env = [part for key in environment for part in ('-e', key)]
        self.ec2.docker('run', '-d', '--name', name, '--label', self.label(), '--label', 'localcloud.instance=' + instance['InstanceId'],
                        '--network', 'container:localcloud-' + instance['InstanceId'], '--read-only', '--user', '0:0', '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
                        '--security-opt', 'no-new-privileges', '--pids-limit', '32', '--memory', '64m', '--cpus', '0.25',
                        '-e', 'LOCALCLOUD_IMDS_DATA=' + json.dumps(payload), *private_env, '--entrypoint', 'python3', self.helper_image, '-m', 'localcloud.ec2_metadata', environment=environment)
        self.wait_imds(name)

    def wait_imds(self, name):
        script = "import socket,time\nfor _ in range(50):\n try:\n  socket.create_connection(('169.254.169.254',80),timeout=.1).close();break\n except OSError:time.sleep(.1)\nelse:raise SystemExit(1)"
        self.ec2.docker('exec', name, 'python3', '-c', script)
