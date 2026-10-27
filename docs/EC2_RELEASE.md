# EC2 release checkpoint — 20 October 2026

This is the current release checkpoint for local EC2 workflows. **Full LocalStack
EC2 equivalence is not complete.** LocalCloud dispatches 104 selected AWS operations
plus the local NameInstance operation. The public SDK catalogue has 807 EC2
operation names; that catalogue does not establish what LocalStack implements.
The [inventory](research/rea/ec2-release-inventory.json) distinguishes these counts.

## Added in this checkpoint

- Persistent AMI registration, image creation/deregistration, description and
  launch-permission attributes. Registered snapshot mappings seed mock instances.
- Owned Docker AMI capture from a **stopped** container without attached volumes,
  followed by launching from that captured root image. No arbitrary Docker image
  becomes trusted through RegisterImage. Existing image allowlists and ownership
  labels remain required. Deregistration checks active AMI users.
- Launch templates, versions, default/latest resolution, source-version inheritance,
  instance launches, template-data reads and version/template deletion. See
  [the launch-template walkthrough](EC2_TEMPLATES.md).
- Snapshot copying within us-east-1, batch snapshots of existing instance volume
  mappings, volume auto-enable-IO metadata and snapshot permission attributes.
- Single-region discovery and local account-attribute discovery.
- CLI integration, snapshot/image dependencies, tags and strict portable-state
  validation for the new resources. Old state bundles remain accepted.

## Start and connect

Use the [complete setup guide](EC2_INFRASTRUCTURE.md#start-from-a-stopped-server).
Restart the backend to load updated Python code. Keep the same database, token,
image mappings and runtime flags. No token regeneration is needed.

For a mock-only exercise:

```bash
localcloud start --port 4566 --data .localcloud/ec2-release.sqlite3
```

In another terminal:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud status
localcloud ec2 --help
```

If the server requires a management token, authenticate to that endpoint with its
existing token. Docker mode requires a token and explicit image mappings as in
the infrastructure guide. Configuring a client endpoint does not change the
server's listening port.

## Reusable launch configuration

```bash
localcloud ec2 create-launch-template --launch-template-name app-template \
  --launch-template-data '{"ImageId":"ami-local","InstanceType":"t3.micro"}'
localcloud ec2 run-instances \
  --launch-template '{"LaunchTemplateName":"app-template","Version":"$Default"}' \
  --name from-template
localcloud ec2 describe-instances --name from-template
```

The CLI no longer requires --image-id when --launch-template supplies it.
Explicit launch flags override template defaults. Docker templates must name an
allowlisted or owned captured AMI. Templates support the documented IAM instance-profile reference. Arbitrary network-interface layouts and AWS VM boot remain outside this subset.

## Create a reusable image

Commands print JSON; replace IDs in subsequent commands with returned IDs.

```bash
localcloud ec2 run-instances --image-id ami-local --name source
localcloud ec2 describe-instances --name source
localcloud ec2 create-image --instance-id i-REPLACE --name application-image
localcloud ec2 describe-images
localcloud ec2 run-instances --image-id ami-REPLACE --name clone
localcloud ec2 describe-image-attribute --image-id ami-REPLACE --attribute description
localcloud ec2 modify-image-attribute --image-id ami-REPLACE \
  --description '{"Value":"Application fixture"}'
```

In mock mode this creates metadata. In Docker mode stop the source instance
first. The source must have no attached filesystem volumes; snapshot those and
use RegisterImage separately for mock volume metadata. A Docker capture commits
its root filesystem to a private, ownership-checked Docker image. It does not
create a VM disk or copy tmpfs contents. Writable root changes require the opt-in
guest profile described in the infrastructure guide. The live test wrote a root
marker, captured the stopped container and read the marker from a new instance.

Terminate instances using an owned Docker AMI before deregistering that AMI:

```bash
localcloud ec2 terminate-instances --name clone --timeout 60
localcloud ec2 deregister-image --image-id ami-REPLACE --timeout 60
```

Deregistration does not delete unrelated source snapshots. DeleteSnapshot rejects
snapshots still referenced by registered images.

## Copy and batch snapshots

```bash
localcloud ec2 copy-snapshot --source-region us-east-1 \
  --source-snapshot-id snap-REPLACE --description 'Copied fixture' --timeout 60
localcloud ec2 create-snapshots \
  --instance-specification '{"InstanceId":"i-REPLACE"}' \
  --description 'Application volumes' --timeout 60
localcloud ec2 describe-snapshots
localcloud ec2 describe-regions --region-names us-east-1
```

Batch snapshots cover the instance's actual emulated volume mappings. An instance
without mapped volumes produces an empty batch; LocalCloud does not fabricate a
root EBS volume. Stop file writers before taking Docker filesystem snapshots.
CopySnapshot accepts Botocore-generated signing metadata without fetching or
persisting a presigned URL. Cross-region copies, encrypted Docker block storage,
raw block devices and disk quotas remain unsupported.

Volume auto-enable-IO, snapshot sharing and image sharing are stored configuration;
they do not implement real hardware controls or multi-account authorization.

## Verification and differences

See the [release evidence index](research/rea/ec2-release-evidence-index.json) for
archive hashes, source hashes and selected capture IDs. The
[template archive](research/rea/ec2-templates-evidence.json.gz) retains the
separately captured template evidence.

- **181 unit tests and 11 live regression checks passed**, including new image,
  template and storage tests. The rebuilt Docker image passed packaged CLI checks.
- [Image replay](research/rea/ec2-images-localcloud.json): 21 recorded cases.
- [Template replay](research/rea/ec2-templates-localcloud.json): 17 recorded cases,
  including a launch through the EC2 Query endpoint.
- [Storage replay](research/rea/ec2-storage-localcloud.json): copy/attribute/batch
  requests and cleanup completed. One batch had no volumes, so produced no snapshots.
- [Actual Docker image test](research/rea/ec2-images-docker-live.json): stopped root
  capture, launch from owned AMI and root marker preservation passed; fixtures removed.
- Source reference captures are retained alongside those reports. They used the
  normally activated pinned LocalStack 2026.10.0 image, not decrypted provider code.

The reference returned 501 for DeleteLaunchTemplateVersions, ResetImageAttribute,
ResetSnapshotAttribute and the tested volume-attribute operations. LocalCloud
implements bounded metadata behavior for these instead. Reference description
mutation and snapshot permission mutation did not persist in the tested calls;
LocalCloud applies them. Template source-version inheritance and idempotent create
also differ. These are deliberate behavior differences, not a claim of exact parity.

The earlier [networking report](EC2_NETWORKING.md) records three stricter validation
checks and actual packet-level observations. Live firewalls, extra guest NICs,
EIP NAT, actual VM/Windows execution, complete IAM/STS semantics, full multi-region/account
isolation remain outside the implemented scope. Kubernetes-backed EC2 now has
a [working subset](EC2_KUBERNETES.md) with Deployment readiness, lifecycle and PVC mounts.

Run the checks:

```bash
python3 -m unittest discover -s tests -q
python3 scripts/ec2-coverage-audit.py --output docs/research/rea/ec2-release-inventory.json
```

A registered operation means a bounded handler exists. It does not mean all AWS
parameters, error conditions, executor modes or response fields have been reproduced.

The [24 October storage follow-up](REA_EC2_STORAGE_FOLLOWUP.md) adds three EBS encryption-default flag operations, modification history and journaled batch-copy quiescing. Earlier evidence counts remain historical checkpoints.

## Instance-role follow-up

The [instance-role guide](EC2_INSTANCE_ROLES.md) adds IAM roles/profiles, inline policies, EC2 profile associations, STS sessions and automatic IMDS credential delivery for Docker and Kubernetes guests. See the [REA report](REA_EC2_INSTANCE_ROLES.md) for observed LocalStack behavior and deliberate differences. Earlier exclusions of IAM instance profiles in this release checkpoint are superseded for the documented subset.

## Docker packet-enforcement follow-up

[Opt-in packet networking](EC2_PACKET_NETWORKING.md) now implements primary IPv4 SG filtering, route reachability gates and local EIP TCP forwarding for explicit published ports. Historical exclusions of all SG filtering/EIP forwarding are superseded for this Docker subset. Broader routing/NAT, secondary interfaces, IPv6 and Kubernetes packet enforcement remain outside it.
