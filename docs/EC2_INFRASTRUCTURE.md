# EC2 networking, storage and guest execution

Current checkpoint: [EC2 release and verification](EC2_RELEASE.md). The later
checkpoint expands the operation set described below to 97 selected AWS operations.

20 October 2026. This expands the earlier [lifecycle reference report](EC2_REFERENCE.md).
LocalCloud implements **74 selected AWS EC2 operations plus `NameInstance`**.
The implementation was informed by REA captures of the normally activated
LocalStack 2026.10.0 image, followed by independent implementation and replay.
It is a usable local EC2 subset, not a complete EC2 or LocalStack replacement.
The [networking REA follow-up](EC2_NETWORKING.md) documents ENI/private-IP/EIP
operations and what the reference actually does with traffic.
CI/CD and EKS are separate work; the existing Kubernetes operator is not EKS.

## What works

| Area | Available behavior |
| --- | --- |
| Instances | Run, describe, start, stop, reboot, terminate, status, console output, stopped-only type modification, tags, filters, pagination, launch tokens, names and managed container selectors. |
| Networks | Persistent VPCs, subnets, DNS attributes, security groups and IPv4 CIDR permissions, gateways, route tables, routes and associations. Explicit subnets get an owned Docker bridge per VPC. |
| Storage | Volume creation, grow metadata, attach/detach, delete, snapshots and restore. Docker mode stores actual files in owned named volumes and copies files for snapshots. |
| Guest access | RSA/ed25519 key creation, public-key import and optional SSH into an allowlisted image containing `sshd`. Writable guest roots survive stopped volume reconfiguration. |
| Metadata | Optional guest-local `169.254.169.254` endpoint, IMDSv1/v2, token expiry, user data and identity document; metadata options can be changed. |
| Discovery | Local allowlisted AMIs, six `us-east-1` zones, three bounded instance types. |
| Network control plane | ENI attributes, secondary private IPs, attachment and elastic-IP associations work in mock and Docker modes; they do not add guest NICs or NAT. |
| Interfaces | LocalCloud CLI, authenticated management API, AWS EC2 Query XML, Boto3/AWS CLI, dashboard infrastructure forms. |

## Start from a stopped server

Use Python 3.11+, Docker CLI/daemon, and an installed editable LocalCloud checkout.
`ssh-keygen` is needed on the **server** for generated key pairs. The server image
now includes Docker CLI and OpenSSH client tools; it does not run a Docker daemon.
Running the server directly on your development host avoids socket/user-group
configuration inside a server container.

From this checkout:

```bash
python3 -m pip install -e .
docker build -t localcloud:ec2-runtime .
mkdir -p /tmp/localcloud-web
cat > /tmp/localcloud-web/Dockerfile <<'DOCKER'
FROM python:3.13-alpine
WORKDIR /app
RUN printf 'Hello from LocalCloud EC2\n' > index.html
USER 65534:65534
CMD ["python3", "-m", "http.server", "8080", "--bind", "0.0.0.0"]
DOCKER
docker build -t localcloud-ec2-web:dev /tmp/localcloud-web
```

Keep your existing management token. If you have never created the file, create
it **once** with private permissions:

```bash
mkdir -p ~/.config/localcloud
if [ ! -s ~/.config/localcloud/server-token ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/localcloud/server-token)
fi
export LOCALCLOUD_MANAGEMENT_TOKEN="$(cat ~/.config/localcloud/server-token)"
localcloud start --port 4566 --data .localcloud/ec2-infrastructure.sqlite3 \
  --ec2-image ami-web=localcloud-ec2-web:dev \
  --ec2-helper-image localcloud:ec2-runtime --ec2-imds
```

Leave that terminal running. The helper image supplies bounded file-copy and
metadata containers. Restart with the **same database, token, image mappings and
runtime flags**. Stopping the backend leaves workload containers running.
If port 4566 is occupied, stop the known old listener or explicitly start on
another port and configure the CLI to that same port. A configured client
endpoint does not change the server's listening port.

In a second terminal:

```bash
unset LOCALCLOUD_MANAGEMENT_TOKEN
localcloud configure --endpoint-url http://127.0.0.1:4566
cat ~/.config/localcloud/server-token | localcloud auth login --token-stdin
localcloud status
localcloud ec2 describe-images
```

The `unset` prevents an old exported token from overriding the newly saved login.
You can also visit `http://127.0.0.1:4566/_local`, enter the **contents** of your
saved token file and connect. The server must be running first.

## Launch a web app with a network and volume

These commands print JSON. Replace `vpc-REPLACE`, `subnet-REPLACE` and `sg-REPLACE`
with the IDs returned by the preceding creation commands. You can always recover
IDs using the corresponding `describe-*` command.

```bash
localcloud ec2 create-vpc --cidr-block 10.90.0.0/16
localcloud ec2 create-subnet --vpc-id vpc-REPLACE \
  --cidr-block 10.90.1.0/24 --availability-zone us-east-1a
localcloud ec2 create-security-group --vpc-id vpc-REPLACE \
  --group-name web --description 'Local web app'
localcloud ec2 authorize-security-group-ingress --group-id sg-REPLACE \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":8080,"ToPort":8080,"IpRanges":[{"CidrIp":"127.0.0.1/32"}]}]'
localcloud ec2 run-instances --image-id ami-web --name web \
  --subnet-id subnet-REPLACE --security-group-ids sg-REPLACE \
  --publish 8088:8080 \
  --block-device-mappings '[{"DeviceName":"/data","Ebs":{"VolumeSize":1,"DeleteOnTermination":false}}]' \
  --timeout 60
localcloud ec2 describe-instances --name web
```

Open `http://127.0.0.1:8088`. The app runs inside the managed container. Your own
image must bind its web server to `0.0.0.0` inside the container. Explicit
`--publish` uses a chosen localhost port. When omitted, up to 32 TCP ports from
the selected security groups are published on dynamically assigned localhost
ports; inspect `PortMappings` to find them.

Use the CLI or the dashboard to stop/start/reboot, rename, tag, change a stopped
instance's type, read console output and terminate. The dashboard also supports
creating/deleting VPCs, subnets, security groups, volumes, snapshots and keys,
viewing resource details, attaching/detaching volumes and adding ingress rules.
Gateway and route administration currently uses the CLI/SDK.

```bash
localcloud ec2 get-console-output --name web
localcloud ec2 reboot-instances --name web --timeout 60
localcloud ec2 stop-instances --name web --timeout 60
localcloud ec2 modify-instance-attribute --name web --instance-type t3.small
localcloud ec2 start-instances --name web --timeout 60
```

## Store data, snapshot and restore

Write application data under `/data`, the attached filesystem. `VolumeSize` is
size **metadata**, not an enforced Docker disk quota. `/dev/sdf` mappings become
filesystem directories at `/mnt/ebs/sdf`; they are not raw block devices and
must not be formatted with `mkfs`.

For a manual smoke test, replace the instance/container ID from describe:

```bash
docker exec localcloud-i-REPLACE python3 -c "open('/data/marker','w').write('saved data')"
localcloud ec2 stop-instances --name web --timeout 60
localcloud ec2 describe-volumes
localcloud ec2 create-snapshot --volume-id vol-REPLACE --description 'Before change' --timeout 60
localcloud ec2 describe-snapshots
localcloud ec2 create-volume --availability-zone us-east-1a --snapshot-id snap-REPLACE --timeout 60
localcloud ec2 detach-volume --volume-id vol-REPLACE --timeout 60
localcloud ec2 attach-volume --volume-id vol-RESTORED --instance-id i-REPLACE --device /data --timeout 60
localcloud ec2 start-instances --name web --timeout 60
docker exec localcloud-i-REPLACE python3 -c "print(open('/data/marker').read())"
```

Docker snapshots now pause attached running guests while copying files and resume
only guests paused by that operation. Flush or stop application writers first for
application consistency: pausing does not flush application buffers, include
external writers, or make a multi-volume batch one atomic transaction. See the
[storage comparison and recovery tests](REA_EC2_STORAGE.md). Storage operations also
use a durable journal for helper/copy/pause recovery after a backend crash. Restart
with the same state database to recover; Docker must be reachable. Docker
attachment changes require a stopped instance and recreate its container; its
container ID changes, while its EC2 ID/name stays stable. Volumes survive that
recreation. Guest mode also commits/preserves its writable root. Normal app mode
has a read-only root. `/tmp` is temporary in both modes.

With `DeleteOnTermination:false`, volumes must be deleted separately after
termination. Snapshots also need explicit deletion. Delete resources in dependency
order: instances, volumes/snapshots, custom interfaces, subnet associations/routes,
custom route tables, gateway attachments/gateways, security groups, subnets, VPCs.

## SSH into an OS-like guest

This is an opt-in profile with a writable root and additional container
capabilities. It still uses the host kernel. Use an image you trust containing
`/bin/sh`, `ssh-keygen` and `/usr/sbin/sshd`; this is not an AMI disk/VM boot.
The live verification used the LocalStack guest image
`localstack-ec2/ubuntu-26.04-resolute-raccoon:ami-61ad6e59d7b0` already present on
this development host. You can build your own image containing `openssh-server`.

Add an `--ec2-image ami-guest=YOUR_GUEST_IMAGE` mapping and
`--ec2-guest-mode` when starting a **separate guest-profile server/database**.
Guest mode applies to every Docker instance on that server, overrides the image
command with a keepalive shell and enables root key-based SSH. It should not be
used for the web app image above. A launch with `KeyName` automatically publishes
port 22 to a random localhost host port. Password login is disabled.

Generated private key material is returned once and is not stored by LocalCloud.
The dashboard downloads it once. The AWS CLI can save it directly to a private
file; the LocalCloud CLI returns it in its JSON response:

```bash
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export AWS_SESSION_TOKEN="$(cat ~/.config/localcloud/server-token)"
(umask 077; aws --endpoint-url http://127.0.0.1:4566 ec2 create-key-pair \
  --key-name dev-key --key-type ed25519 --query KeyMaterial --output text > ./dev-key.pem)
localcloud ec2 run-instances --image-id ami-guest --key-name dev-key --name guest --timeout 60
localcloud ec2 describe-instances --name guest
ssh -i ./dev-key.pem -p REPLACE_HOST_PORT root@127.0.0.1
```

Alternatively create your own key locally and import only its public half:

```bash
ssh-keygen -t ed25519 -f ./dev-key
localcloud ec2 import-key-pair --key-name dev-key --public-key-material fileb://./dev-key.pub
```

Server image execution requires normal Docker daemon access. No workload gets a
host Docker socket. No host bind mounts or arbitrary host paths are accepted by
EC2 APIs.

## Metadata and SDK access

`--ec2-imds` starts a separate helper in each guest's network namespace. It binds
`169.254.169.254:80` there; it does not alter host routes. A guest can request its
instance ID, AMI, type, network/placement data, user data and identity document.
Use `ModifyInstanceMetadataOptions` to require IMDSv2:

```bash
localcloud ec2 modify-instance-metadata-options --name web --http-tokens required --timeout 60
```

IMDSv2 uses a PUT to `/latest/api/token` with
`X-aws-ec2-metadata-token-ttl-seconds: 1..21600`, then GET with
`X-aws-ec2-metadata-token`. Tokens are per guest and expire. No IAM role credentials
are issued. IPv6 metadata and metadata tags are unsupported; the stored hop-limit
option does not implement AWS packet TTL enforcement. Updating options recreates
the helper and invalidates existing tokens.

AWS CLI uses the backend endpoint and management token as `AWS_SESSION_TOKEN`,
as in the SSH example. Boto3 uses `endpoint_url`, test access/secret keys and
`aws_session_token`. Configure `region_name='us-east-1'`. The runtime stays local.
No LocalStack token is needed to run this project.

## Evidence and limits

The [REA archive index](research/rea/ec2-infrastructure-index.json) records hashes
and coverage for the [compressed evidence bundle](research/rea/ec2-infrastructure-evidence.json.gz).
[Selected evidence IDs](research/rea/ec2-infrastructure-records.json) identify the
relevant successful captures. The archive retains earlier failed captures and
session unknowns as well; it is not a declaration of whole-session parity.

Reference: LocalStack 2026.10.0 build `738d58ee`, normally activated with the
provided Student license, pinned image digest
`sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
No encrypted provider code or entitlement bypass was used.

- [Reference Boto3 capture](research/rea/ec2-infrastructure-reference.json) and
  [LocalCloud replay](research/rea/ec2-infrastructure-localcloud.json): 47 cases
  each, 46 matching HTTP statuses. Detach-by-volume-ID returned 500 from the
  reference and 200 locally. Status agreement does not establish whole-response
  or all-parameter equivalence.
- [Live Docker result](research/rea/ec2-infrastructure-live.json): actual web
  endpoint, IMDSv1/v2 and file snapshot/restore/reattach succeeded.
- [Live SSH result](research/rea/ec2-guest-root-live.json): real SSH and writable
  root preservation across volume attach and detach succeeded.
- [Browser result](research/rea/ec2-browser-live.json): dashboard connection and
  VPC creation succeeded. The last test wait had an ambiguous locator; the
  backend independently confirmed the created VPC. This is not full UI coverage.
- [Final audit](research/rea/ec2-infrastructure-final-audit.json): 155 tests and
  11 live regression checks passed. The rebuilt server image passed Docker CLI,
  nonroot key generation and schema packaging checks.
- Reproduce execution checks using `python3 scripts/ec2-docker-smoke.py` and
  `python3 scripts/ec2-ssh-smoke.py` with the explicitly named local images present.
  They use temporary databases and clean their created resources.

VPC bridges do not reproduce AWS routing or security-group firewalls. CIDRs,
DNS attributes and route tables are configuration records; the guest's actual
Docker IP can differ from its emulated private IP. Ingress currently controls
launch-time TCP port publishing only. CIDR source restrictions and later rule
changes are not enforced on running Docker traffic. New elastic IPs use a loopback identifier pool; they do not bind host listeners.
Secondary NICs and EIP associations are control-plane records in both modes.

Volume encryption, raw EBS devices, disk quotas, full AMI import/catalog, real VM
boot, Windows, IAM instance profiles/credentials, full policy semantics,
multi-account/region isolation, autoscaling and Kubernetes-backed EC2 are not
implemented. Only `t3.micro/small/medium` are runnable; actual Docker limits are
128/256/512 MiB and one CPU, regardless of AWS instance-type discovery metadata.
Unsupported fields fail explicitly where validated. Support for an operation
name does not imply support for its complete AWS parameter set or semantics.

The public Botocore 1.43.112 EC2 schema supplies wire-shape metadata only;
[attribution](../localcloud/BOTOCORE-NOTICE.txt) and
[Apache license](../localcloud/BOTOCORE-LICENSE.txt) are included. The engine,
protocol encoder/decoder and runtime are independent code informed by captures.

For current EBS default-flag commands, modification history and batch-copy pause/recovery behavior, see the [24 October storage follow-up](REA_EC2_STORAGE_FOLLOWUP.md).
