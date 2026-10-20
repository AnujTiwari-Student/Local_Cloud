# EC2 lifecycle implementation and reference coverage

**Historical lifecycle milestone.** The current provider has 74 selected AWS
operations plus NameInstance. Networking, storage, IMDS and SSH were added after
this report; see [the current EC2 guide](EC2_INFRASTRUCTURE.md) for setup, evidence
and remaining limits. The 12-operation counts and missing-feature lists below
describe the earlier milestone.

20 October 2026. This replaces the EC2 gap summary in the earlier
[EC2/Kubernetes investigation](REA_EC2_KUBERNETES.md). It does not certify full
LocalStack EC2 parity.

## Implemented operations

The provider now supports 12 operations through the management API and EC2 Query
XML: RunInstances, DescribeInstances, StartInstances, StopInstances,
TerminateInstances, ModifyInstanceAttribute, DescribeInstanceAttribute,
DescribeImages, CreateTags, DeleteTags, DescribeTags, and the LocalCloud extension
NameInstance. These are 11 AWS operations plus one extension, not AWS-wide API
coverage.

Mock launch/lifecycle responses now report `pending`, `stopping` and
`shutting-down`, matching the captured reference responses. Operations settle
immediately in stored state, so a subsequent describe returns the final state.
Docker launches report pending; Docker start/stop/terminate report final states,
as observed from the reference. Repeated lifecycle calls remain supported.
Reservation IDs now persist and DescribeInstances groups by reservation.

Instance tags support launch specifications, creation/update, conditional
deletion and describe. Filters combine with AND; values within each filter use
OR and support `*`/`?` wildcards. Supported filters:

| Resource | Filters                                                                                                                   |
| -------- | ------------------------------------------------------------------------------------------------------------------------- |
| Instance | instance-id, image-id, instance-type, instance-state-name, instance-state-code, client-token, tag:KEY, tag-key, tag-value |
| Image    | image-id, name, state, owner-id, architecture, tag:KEY, tag-key, tag-value                                                |
| Tag      | resource-id, resource-type, key, value                                                                                    |

Unknown filters fail explicitly. Tags are limited to 50 per instance; resource
tag APIs currently support instance IDs only. An AWS Name tag can be used with
the existing name selector when it is valid for that selector and unambiguous.
The older unique LocalCloud Name field remains separate from AWS tags.

DescribeImages exposes the server's allowlisted Docker AMIs. Mock mode exposes
`ami-local` plus images already used in that state database. This is a local
catalog, not LocalStack's complete AMI database. Only owner `self` or
`000000000000` selects these local images. The currently advertised architecture
is x86_64; this is not multi-architecture AMI emulation.

DescribeInstanceAttribute supports `instanceType` and `userData`. User data is
base64 in the APIs; CLI text/files and dashboard scripts are encoded for you.
Docker executes a UTF-8 shell script once after launch, inside the owned
container as UID 65534. The script is limited to 16 KiB and the existing 30-second
Docker operation timeout. It needs `/bin/sh` in the image and a running container.
The filesystem stays read-only except a bounded `/tmp` tmpfs. `/tmp` contents are
lost on stop/start, and user data does not rerun on restart. This is a small shell
runner, not cloud-init, root setup, package installation or an OS boot process.
Mock mode stores user data without executing it. User data is returned only by
the explicit attribute read, not by DescribeInstances or the resource overview.

Docker lifecycle calls now refresh actual container state before deciding
whether to start or stop it, fixing external-stop recovery without a prior
DescribeInstances call. Existing ownership labels, image allowlists, loopback
TCP publishing and container restrictions still apply.

## CLI walkthrough

Keep a LocalCloud backend running and authenticate the CLI to that endpoint as
in [the EC2 setup guide](EC2.md). In mock mode, use `ami-local`; for a real app,
start with the existing demo image mapping and publish its port:

```bash
# Server terminal: use your existing saved token, without regenerating it.
export LOCALCLOUD_MANAGEMENT_TOKEN="$(cat ~/.config/localcloud/server-token)"
localcloud start --port 4566 --data .localcloud/ec2-docker.sqlite3 \
  --ec2-image ami-web=localcloud-ec2-web:dev
```

In another terminal:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
cat ~/.config/localcloud/server-token | localcloud auth login --token-stdin
localcloud ec2 describe-images
printf 'printf hello >/tmp/setup-marker\n' > /tmp/localcloud-user-data.sh
localcloud ec2 run-instances --image-id ami-web --name web \
  --client-token web-launch-1 --publish 8088:8080 \
  --tags Key=Env,Value=dev Key=Owner,Value=me \
  --user-data file:///tmp/localcloud-user-data.sh --timeout 60
```

The example assumes the demo image has already been built using [EC2.md](EC2.md).
Visit `http://127.0.0.1:8088`. Repeating the same launch command with the same
token returns the existing instance and reservation. Changing launch parameters
with that token returns IdempotentParameterMismatch. Keep a token per logical
launch; use a new token to create another instance.

```bash
localcloud ec2 describe-instances --filters '[{"Name":"tag:Env","Values":["dev"]}]'
localcloud ec2 create-tags --resources i-REPLACE --tags Key=Env,Value=test
localcloud ec2 describe-tags --filters '[{"Name":"resource-id","Values":["i-REPLACE"]}]'
localcloud ec2 delete-tags --resources i-REPLACE --tags Key=Owner
localcloud ec2 describe-instance-attribute --instance-id i-REPLACE --attribute userData
localcloud ec2 stop-instances --name web --timeout 60
localcloud ec2 modify-instance-attribute --name web --instance-type t3.small
localcloud ec2 start-instances --name web --timeout 60
localcloud ec2 terminate-instances --name web --timeout 60
```

The dashboard exposes launch tokens, user-data scripts and tag create/update/delete
in the EC2 detail view. Its existing names, instance types, port mappings and
lifecycle controls remain available.

## AWS SDK and CLI access to Docker mode

For local tests, use dummy access/secret keys and send the management token as
the AWS session token. It is checked as a bearer credential; LocalCloud does not
verify SigV4 cryptography or emulate AWS credential issuance.

```bash
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
AWS_SESSION_TOKEN="$(cat ~/.config/localcloud/server-token)" \
aws --endpoint-url http://127.0.0.1:4566 --region us-east-1 ec2 describe-images
```

For Boto3 use the same endpoint/region, dummy access and secret keys, and
`aws_session_token` read from that file. The LocalCloud CLI/dashboard continue to
send their existing Authorization bearer header. Unauthenticated or wrong-token
Docker EC2 requests return 401.

## Evidence and differences

Reference: LocalStack **2026.10.0**, build **738d58ee**, image digest
`aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
The token activated normally; no entitlement checks were bypassed.

| Capture                                                         | REA Evidence                                                        |
| --------------------------------------------------------------- | ------------------------------------------------------------------- |
| Reference, 27 Boto3 mock scenarios                              | ev_95c52c636e6e34fd9554c9a214bae5a422d54d66b2581deb5cc97ea84d40c182 |
| Reference, same scenarios with Docker AMI                       | ev_c09ec103c60fae7785c242da74f22b4d0a785f686b6f50fe93825bd1870277f9 |
| LocalCloud mock, same SDK scenarios                             | ev_5b7de1d1f383a4f9460e600267f79df77b106db575bf16ead5f1dffd8d14dd0c |
| LocalCloud Docker, same SDK scenarios                           | ev_fc9960ea199226ca4a917ee08d65646bad2905cca600e705850033e1162edfad |
| Real Docker user-data marker, replay and external-stop recovery | ev_5ddcf43072aa4f783ff30a066d47b9ce8bcb3de9c687ab1d018fc33088756d62 |
| Final runtime audit: 141 tests and 11 live checks passed        | ev_6e0c10ca5be8d4c758f241c611a6689eae8593bc54b8140003f961bba4c38c5f |

All **nine earlier wire projections now match** the reference mock lifecycle
capture: [comparison](research/rea/ec2-wire-comparison-final.json). These compare
only status, XML root, relevant state names, type and error code, not full XML.

The expanded [SDK status comparison](research/rea/ec2-sdk-comparison.json) retains
27 scenarios per backend and lists missing instance-response fields. It is not
a full-payload equality test. Raw SDK responses are retained beside it.
LocalCloud's mock AMI and the reference's preinstalled AMI differ intentionally.

The Docker reference's mismatched-replay request timed out in the final user-data
scenario; its HTTP status is null, not an inferred AWS error or matching result.
That launch nevertheless created a container. It was identified by capture-time
creation, exact AMI/name and the scenario's user-data marker, then removed:
`ev_1d53c1258816489ee719ea3c88f3003a9cf71e85c0db30cd9eb4f23f80dc4b16`.
The probe was subsequently corrected to discover instances by its ClientToken
before cleanup. A controlled timeout after a real Docker launch verified that
recovery path: `ev_d41b6797f08f4e1366b5920f13543f070d6b49e2319fa3ad6d0f92618b490dfc`.
Earlier captures retain the pre-fix probe hash in their filesystem observations.

Two deliberate differences remain: the captured reference creates new instances
when a ClientToken is reused, including when parameters change, and allows a
start request after termination. LocalCloud implements persistent launch
idempotency and rejects restarting terminated instances. These differences also
affect downstream resource counts in the scenario. Unknown-filter error messages
also differ while status/code match.

Broader EC2 remains unfinished: VPC/subnet/security-group APIs, EBS volumes and
attachments, key pairs/SSH, reboot/console APIs, EC2 status waiters, pagination,
AWS IAM profiles/credential issuance, IMDS at 169.254.169.254, full networking and
metadata response shapes, multiple regions/accounts, and Kubernetes-backed EC2
are not implemented. Loopback Docker port publishing and API attribute reads do
not establish parity with those features. This service remains a tested local
subset, not a replacement for every LocalStack EC2 operation.

The dashboard JavaScript passed syntax checking and backend contract tests; this
pass did not run a live browser interaction scenario for the new form controls.

The canonical REA session export and source/script hashes are indexed in
[ec2-final-evidence-index.json](research/rea/ec2-final-evidence-index.json).
