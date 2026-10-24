# EC2 executor investigation — 23 October 2026

This investigation observes the normal, licensed LocalStack 2026.10.0 product.
It does not recover proprietary source or establish full EC2 compatibility.
The pinned image is
`localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
Startup reported build `738d58ee` and an activated Student license.

## What was observed

| Executor | Evidence boundary | Result |
| --- | --- | --- |
| Mock | Earlier isolated API probes | Resource records and API responses; no guest process. See [earlier comparison](REA_EC2_KUBERNETES.md). |
| Docker | Real guest and storage probes | Runs actual containers. See [storage findings](REA_EC2_STORAGE.md). |
| Kubernetes | Stock run, then a separately marked deployment intervention | Stock creates workload resources but fails pulling an AMI name. A controlled node-cache run starts a real guest and executes user data. Details below. |

These are three observed executor selections, not a complete catalogue. A bounded
inspection of the shipped public `localstack/config.py` found no
`EC2_VM_MANAGER` or `K8S_IMAGE_PULL_POLICY` entry. That negative search does not
establish which other executor names the proprietary provider accepts. QEMU,
libvirt, Windows and other VM backends were not tested or installed.

## Stock Kubernetes run

The reference ran in its own namespace on `kind-localcloud`, with namespace-only
RBAC and its normal auth token supplied through a Secret. A new RegisterImage
result was tagged to the cached Ubuntu guest image and imported into the node.
Existing host image tags and user workloads were preserved.

`RunInstances` returned HTTP 200 with instance state `pending`. The provider
created an EC2 Deployment, an `imds-proxy-sidecar` using `alpine/socat:latest`,
and an `init-iptables` init container. The guest image was the generated AMI ID
with `imagePullPolicy: Always`. Kubernetes attempted to fetch
`docker.io/library/ami-…:latest` from the registry and failed, even though that
tag had been imported into the node cache. A successful API response therefore
did not mean the guest was running.

Evidence: `ev_9543e68111e03a6f77064fedc66645941ab2c4efa1824972c0984d9cc463630b`;
[stock report](research/rea/ec2-executor-reference.json).

## Controlled guest run

A second independent run patched **only its own generated guest Deployment** to
use `imagePullPolicy: Never`. This is an explicit experiment intervention, not
stock reference behavior and not a discovered product configuration option.
The guest and IMDS sidecar then reached Running.

Observed outcomes:

- User data wrote `/tmp/rea-marker`; reading it inside the guest returned
  `executor-user-data-ok`. DescribeInstances then reported `running`.
- CreateVolume and AttachVolume succeeded and DescribeVolumes recorded an
  attachment at `/dev/sdf`. The guest's mount entries did not change, `/dev/sdf`
  did not exist, and `/mnt` remained empty. This experiment demonstrates metadata
  attachment, not a usable block disk or mounted data filesystem.
- CreateSnapshot, DetachVolume, DeleteSnapshot and DeleteVolume succeeded.
  Snapshot success alone does not demonstrate a byte-level disk copy.
- StopInstances returned previous and current state `running`; the Deployment
  remained at one replica. StartInstances also returned `running` to `running`.
  The root marker remained readable, but this does **not** demonstrate persistence
  across a stopped or recreated Pod: no successful stop occurred in this probe.
- TerminateInstances returned `terminated` and removed the EC2 Deployment.
  The reference logged a missing-Service deletion error while still returning
  success. Namespace teardown subsequently removed the remaining workload Pod.

Evidence: `ev_2f1b5da3a604aaaf2730811bfdbb88e920f234a0548afea27a052547eede70e0`;
[controlled report](research/rea/ec2-executor-controlled.json).

## Later implementation

The subsequent [EC2 Kubernetes adapter](EC2_KUBERNETES.md) now implements a bounded LocalCloud workload path. The investigation below records the earlier checkpoint and preserves its original evidence boundaries.

## What this means for LocalCloud

LocalCloud's existing Kubernetes operator and generic Job executor are separate
from an EC2 Kubernetes backend. This pass adds evidence and a reproducible probe;
it does not implement that backend. A future adapter needs explicit ownership,
guest image mapping, readiness/failure reporting, lifecycle reconciliation,
metadata networking, and a documented storage contract. It should not silently
report a running instance when its Pod cannot pull an image.

Do not infer working EBS disks from attachment records, working stop/start from
HTTP 200, or EKS support from Kubernetes workload execution. LocalCloud's Docker
file-backed volumes are a separate implementation choice and support actual
file copies; they are not raw AWS EBS devices.

## Reproduce and inspect evidence

Requirements: the pinned reference cached on the host and kind node, the cached
Ubuntu guest image, the `kind-localcloud` context and node
`localcloud-control-plane`, Docker, kubectl, and a normally entitled token at
`~/.config/localstack/rea-auth-token`. The script assumes the node's existing
`localstack/localstack:latest` cache points at the pinned reference; inspect the
recorded host identity and reference startup version when reviewing results.
It uses a unique namespace, Secret, image tag and API-generated AMI per run.

```bash
# Stock behavior; does not patch the generated guest workload.
python3 scripts/ec2-executor-reference.py

# Separate controlled experiment using the imported node image.
python3 scripts/ec2-executor-reference.py --bootstrap-cached
```

The probe executes real workloads. Reports omit container environment values and
Secret contents. Cleanup verifies the namespace ownership label before deletion
and removes its unique node image tag after successful namespace teardown.
Review a failed run's cleanup result before rerunning it.

Both namespaces were confirmed absent, and both unique imported AMI tags were
removed. Shared Ubuntu, reference and socat image caches were preserved.
Cleanup evidence:
`ev_2b2bd528ab484544f8778117d1da0e72f60d7504d9628930b9fef26e5c566d1e`.

The [evidence index](research/rea/ec2-executor-index.json) records hashes and
boundaries. The [compressed full REA bundle](research/rea/ec2-executor-evidence.json.gz)
contains 11 records, including partial and negative observations; this count is
not a service-parity score. Final script cleanup conveniences were compile
checked after the recorded runs, rather than rerunning the whole experiment.
