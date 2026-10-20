# EC2 and Kubernetes reference investigation

**EC2 follow-up:** the [expanded implementation and reference report](EC2_REFERENCE.md)
supersedes the EC2 gap counts below. The nine lifecycle projections now match;
full service parity remains unestablished. Kubernetes observations below remain
historical evidence for their stated scope.

20 October 2026. This continues the earlier [service audit](REA_SERVICES.md).
The auth-token blocker in that earlier report is now resolved. **Full parity is
not established.** This is a bounded runtime investigation and one compatibility
fix, not a completed reconstruction of these services.

## Reference identity and credentials

REA started the official image with the token read inside the controller from
`~/.config/localstack/rea-auth-token`. The token was never included in capture
arguments, output, or repository files. Startup activated a Student license.

- Image: `localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
- Runtime: **2026.10.0**, build **738d58ee**, built 13 October 2026.
- Successful authenticated startup: `ev_26fd7b03efbf8aed37b68210831e08a21314379493fea5c16a5adb11a71f148b`.

The Docker EC2 reference needs the host Docker socket; without it, even
DescribeInstances returned InternalError. With it, real Docker lifecycle calls
worked. The reference launch used its existing Ubuntu AMI, not a real AWS account.
All controller containers were removed. No entitlement bypass was used.

## EC2 observations and implemented correction

The reference accepted the AWS Query form fields `InstanceId` and
`InstanceType.Value` for ModifyInstanceAttribute. LocalCloud previously accepted
only its management API shape and rejected the AWS request. Its Query adapter now
maps those fields to its existing provider arguments, rejects conflicting
selectors, and returns an empty operation response with requestId, as observed
from the reference. The management API keeps its existing response.

Evidence:

- Mock lifecycle: `ev_9dd53d9ceab4ffe09b8c95eaca8ae3702c4ea0425763c3b3708c71c95953597c`.
- Docker lifecycle: `ev_917a4c1fce5a239055d64306d90ff7352c4fa830470b85afdd12c5b272801219`.
- Shared raw Query probe, reference: `ev_940288f4e2772438e6bdfe0080598ac29e19fb7daa7d7042b4b5865775265e26`.
- Same probe after the correction, LocalCloud: `ev_766ad8123952687110b7f1af3eb75d398ee13576d4b88aeaba6683922b6ac2af`.

The [nine-request comparison](research/rea/ec2-wire-comparison.json) has **5 matching
projections and 4 differences** against the mock backend. A projection compares
only HTTP status, XML root, error code, instance type and relevant state names.
Matching a projection does not establish full response equality. Complete XML is
retained in [reference](research/rea/ec2-wire-reference.json) and
[LocalCloud](research/rea/ec2-wire-localcloud.json) reports.

| Request            | Reference mock | LocalCloud |
| ------------------ | -------------- | ---------- |
| RunInstances       | pending        | running    |
| StopInstances      | stopping       | stopped    |
| StartInstances     | pending        | running    |
| TerminateInstances | shutting-down  | terminated |

The Docker reference returned final states for stop/start/terminate, so mock
transition behavior must not be applied indiscriminately to both backends.
LocalStack also returns many networking, reservation, storage, metadata and
placement fields absent from LocalCloud. DescribeImages, AWS tags/filters,
ClientToken idempotency, user data, EBS, security groups, metadata service, SDK
coverage and account/region isolation remain outside the implemented subset.

## Live Kubernetes evidence

LocalCloud was freshly built as `localcloud:rea-a4b89f87`, loaded into the existing
kind cluster, and tested in unique namespace `localcloud-rea-72703825`. The
existing global CRD was not modified. Its actual operator created a Ready
instance; S3 binary roundtrip, SQS delivery/visibility/acknowledgement and a
Kubernetes Job reaching the service endpoint passed. This test used ephemeral
storage; it does not verify PVC durability or TTL expiry.

- Build/load: `ev_cb73a8dc529cec836723da7dd80b15f106b445f690417130b860bd3fee871c0f`.
- Live operator/executor: `ev_1729436f2e5bf2840c40c15f0eacec29fc48fc631ac9426493d4a0e963b538df`.
- [Live report](research/rea/kubernetes-live/report.json).

The reference was then deployed directly, using a namespace-scoped ServiceAccount
in separate disposable namespaces. No reference operator or CRDs were installed.
Inside Kubernetes, DescribeInstances succeeded and RunInstances created a
Deployment and Pod, including an `alpine/socat` IMDS proxy sidecar. The attempted
AMI ID was treated as an image name and failed to pull; the client timed out.
This proves the creation path, **not successful execution or lifecycle parity**.

- In-cluster reference: `ev_9ac7de0a984368abf81c6ecfc0ac8ffe3cb0a6aeaf3ec1169d147ae363e02c1d`.
- [Reference diagnostics](research/rea/kubernetes-reference-live.json).
- A kind multi-platform import initially failed with a missing digest. A
  single-platform Docker export/containerd import succeeded. Its rebuilt index
  digest differs from the original multi-platform index; the imported source
  tag was checked against the pinned host image before export.

All created audit namespaces were requested for deletion. The user's `localcloud`
namespace, existing operator/instances, current kubectl context and CLI profile
were left alone. Imported images remain cached on the kind node.

LocalCloud currently exposes generic Job execution and its own LocalCloudInstance
operator. **EC2 instances do not use that executor.** A Kubernetes EC2 provider,
EC2 Deployment lifecycle, IMDS compatibility and reference-operator reconciliation
comparison still need implementation/investigation. A generic Job is not an
equivalent replacement for the observed EC2 Deployment and sidecar.

Public documentation describes the reference's
[Kubernetes executor](https://docs.localstack.cloud/aws/customization/kubernetes/kubernetes-executor/)
and [operator access](https://docs.localstack.cloud/aws/quickstart-library/k8s-quickstart/)
as Enterprise capabilities. That plan label alone is not evidence this Student
token is denied: the observed runtime did create workload resources.

## Reproduce the bounded checks

Against a disposable local emulator, this creates one instance and attempts to
terminate it in finally. Choose an image valid for the selected backend:

```bash
python3 scripts/ec2-wire-probe.py --endpoint http://127.0.0.1:4566 --image-id ami-local
```

For Docker-backed LocalCloud, also pass
`--token-env LOCALCLOUD_MANAGEMENT_TOKEN` and use an allowlisted AMI.

With a loaded LocalCloud image, installed CRD and a dedicated test namespace:

```bash
python3 scripts/kubernetes_smoke.py --context kind-localcloud --existing-crd \
  --namespace localcloud-audit --image localcloud:dev \
  --artifacts /tmp/localcloud-kubernetes-audit
```

The smoke command removes its instance and Secret, leaving its namespace and
operator installation. Delete that dedicated namespace after inspecting results.
`--existing-crd` prevents overwriting the cluster-wide CRD; `--context` applies to
both API calls and port forwarding.

Final LocalCloud verification: **132 tests, 11 live checks passed**, including a
real Docker web app's launch, port mapping, stop, resize, start and termination.
Evidence: `ev_22639fd3f94d57970effe1c89a28e996cf1a4bfc2a52e131c7e37f64a69b5916`.
The [runtime report](research/rea/ec2-kubernetes-runtime-report.json) records source
hashes. The exported REA session contains 47 records; the compressed canonical
bundle and hashes are indexed in
[ec2-kubernetes-evidence-index.json](research/rea/ec2-kubernetes-evidence-index.json).

Cleanup confirmation: `ev_22579d5bee20762e29c978004cb2d7f6d912ba2813d22e80883680a42adac4b9` found no remaining audit namespaces and confirmed context `kind-localcloud`.
