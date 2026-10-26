# REA: EC2 instance roles, STS and metadata credentials

This investigation implements a bounded instance-role workflow in LocalCloud. It compares normally licensed LocalStack API behavior, then independently adds usable credential delivery and request authentication. It does not recover proprietary provider source or establish full LocalStack parity.

Start with the [application setup guide](EC2_INSTANCE_ROLES.md) for CLI commands and an example that reads an S3 object using automatic SDK credentials.

## Reference and method

Reference: LocalStack **2026.10.0**, build `738d58ee`, image:

```
localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872
```

The fixtures use the user's normal local license file, isolated reference containers and namespaces, owned tags and resource names. No entitlement bypass or protected provider extraction is involved. Credential values are redacted from reference reports; shapes, lengths and returned identities are sufficient for these comparisons. The selected EC2 Query schemas come from public Botocore model data, not LocalStack provider code.

The [reference index](research/rea/ec2-iam-reference-index.json) binds scripts and reports with SHA-256. The [reference bundle](research/rea/ec2-iam-reference-evidence.json.gz) retains 13 Evidence records and their limitations.

## Findings

| Area | Observed LocalStack result | LocalCloud behavior |
| --- | --- | --- |
| Missing IAM role/profile | `NoSuchEntity`, HTTP 404 | Same selected errors |
| Duplicate IAM role/profile | `EntityAlreadyExists`, HTTP 409 | Same selected errors |
| Profile membership | One role; duplicate/second role `LimitExceeded`, HTTP 409 | Same bound |
| Inline role policies | Put/get/list round trip | Supported bounded Action/Resource policy language |
| STS AssumeRole | Credentials, expiry and assumed-role identity; default unenforced reference even accepted a nonexistent role | Requires an existing role and matching trust; caller permissions apply in enforcement mode |
| STS credential format | `LSIA` prefix, access key length 20; secret length 40; token length 356 in this run | Local `ASIA` keys, generated secret/session token; exact reference token bytes and lengths are not reproduced |
| EC2 association | Associate returns `associating`, next describe `associated`; duplicate association `IncorrectState` | Matching selected response progression |
| Replace | Preserves association ID, returns `associating` | Same selected behavior; metadata resolves replacement dynamically |
| Disassociate | Returns `disassociating`; next describe omits retired association | Same selected behavior; local sessions revoked |
| Instance profile ID | Tested reference RunInstances/DescribeInstances used association ID in `IamInstanceProfile.Id`, while association describe used the actual AIPA profile ID | Keeps the actual AIPA profile ID, an intentional consistent identity choice |
| Docker IAM metadata | Guest link-local requests timed out/no route; direct backend port 4510 refused | Delivery now verified in a real Docker guest; reference transport failure does not prove feature absence |
| Kubernetes IAM metadata | With a valid attached profile and corrected `pods/exec` RBAC, basic metadata and IMDSv2 worked; IAM subtree/list/named-role requests returned explicit 404 “not implemented.” Guest had no AWS credential environment or root credentials file | Implements the AWS-style credential routes as an independent extension |

Principal reference evidence:

- `ev_d20c90ac0ab665f1cd76531142cc6f5eb1a97912d680b5bff51273f6aac5edf2` — IAM/STS contracts; [report](research/rea/ec2-iam-api-reference-report.json).
- `ev_cd8d3e5c85c1fb962849d27cf7cd6aa1a98756e621ad580d9972e7d7eca59011` — EC2 launch/association/replace/disassociate and errors; [report](research/rea/ec2-iam-reference-report.json).
- `ev_2183ccf3eb14b5cc3839a4a5191ca7bc065e58ad333046b512529fcf6b94ab9c` — corrected Kubernetes profile-attached metadata test; [report](research/rea/ec2-iam-kube-reference-report.json).
- `ev_7a8394b7a8b976a69adf3d2d0c9a18c087d083ea43c4dbe537d700c32fc605ea` — reference cleanup and artifact hashes.

The Kubernetes reference needs declared cached-image `Never`/`Recreate` adjustments and diagnostic bootstrap identity headers. The initial missing `pods/exec` fixture permission is retained in the preliminary report; the final repeat corrects it. Observations are specific to this pinned version, backend and controlled fixture.

AWS documents the intended workflow separately: the SDK automatically retrieves role credentials from [EC2 metadata](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-security-credentials.html), while [STS AssumeRole](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html) returns temporary credentials with a bounded duration. These public contracts inform LocalCloud's extension where the reference did not establish a working delivery path.

## Implementation

- `identity.py`: role/profile records, normalized inline and trust policies, STS sessions, instance-scoped broker capabilities, rotation and revocation. Credential secrets are stored in separate runtime SQL rows rather than the portable resource JSON.
- `ec2_profiles.py`: four EC2 profile APIs, profile selection at launch, `iam:PassRole`, response progression and termination cleanup.
- `ec2_metadata.py`: dynamically resolves `iam/info`, role listing and credential documents only after existing endpoint/IMDSv2 authorization. A bounded Docker bootstrap seed handles launch-transaction timing.
- `ec2_kubernetes.py` / `ec2_runtime.py`: helper-only broker transport. Docker private environment values are not placed in command arguments. Kubernetes retains its native metadata startup gate.
- `identity_wire.py`: IAM/STS Query XML and header-based SigV4 verification for locally issued temporary sessions.
- Gateway/HTTP/CLI: explicit IAM and STS providers, management routes, AWS Query dispatch, instance-scoped broker route and AWS-style CLI operations.

A temporary role session is mapped to its role ARN before service authorization. `X-Local-Principal` cannot override that verified identity. Authentication verifies the session token, expiration, role identity, signed header set, timestamp, region/service scope, payload hash and HMAC signature. Current inline policies are evaluated at request time, so edits affect existing sessions. Explicit deny takes precedence when `--iam enforce` is used.

The broker route is checked separately from management authentication. An instance capability cannot read another instance's credentials or invoke management APIs. To avoid deadlocking synchronous Docker user data behind its own launch transaction, a busy broker returns 503 and the helper can use its short-lived initial seed. That fallback is limited to 30 seconds and credential expiry; explicit 401/403/404 responses never use the seed.

Profile replacement/removal, role membership removal, trust changes and termination revoke relevant local sessions. These immediate revocations are deliberate emulator behavior; this investigation does not claim identical AWS propagation/caching timing.

## Live Docker and SDK validation

The final clean capture is `ev_c3f8c29a3c4e306f732d20e2349b1523dab974bee74dfcfe3377f889d4cf2d0f`: **12 checks passed**. See the [live report](research/rea/ec2-iam-live-report.json).

An actual guest uses Botocore/Boto3 1.43.112 from the cached reference image as public SDK tooling. The guest does not start a LocalStack server or use a LocalStack license. A uniquely built helper contains current LocalCloud code.

Verified behavior:

1. User data obtains credentials while launch state is not yet committed.
2. The SDK's credential provider reports `iam-role`, without explicit application keys or the management token.
3. Signed STS returns the expected assumed-role/instance identity.
4. Signed S3 reads succeed and writes are denied by the role policy.
5. IAM Query profile output and SDK STS AssumeRole parse correctly.
6. Instance capabilities cannot cross instances or substitute for management authentication.
7. Bad signatures, bad session tokens and expired sessions fail over HTTP.
8. Rotation creates a new temporary session.
9. Profile replacement changes the SDK-discovered role.
10. Disassociation removes metadata credentials and revokes the old session.
11. Termination invalidates the instance capability.

The listed items group related assertions; the machine report records all 12 check names. All owned guest/helper containers and image tags were removed. A preliminary agent capture predating the environment-argument fix contained revoked local temporary credentials in process samples and is not published as the final evidence bundle. The final root capture uses private environment passing.

## Kubernetes and unit validation

The Kubernetes fixture passed **8 checks**, evidence `ev_002b50cf637ace988d2211b3b526fd849d67cd278cea4d4cdf5d8d8bcd0d4fcf`. See the [live report](research/rea/ec2-iam-kube-live-report.json), [index](research/rea/ec2-iam-kube-live-index.json) and [evidence bundle](research/rea/ec2-iam-kube-live-evidence.json.gz).

Actual Pod user data retrieves its role credentials before the app starts. The running guest signs STS identity and S3 requests; reads return 200 and writes return 403. Profile replacement updates credentials without restarting the Pod; detach returns 404; reattach restores delivery. Stop/start creates a new Pod whose bootstrap and role access still work. Termination cleans workloads and revokes instance access. All owned namespaces and image tags were removed.

The preliminary fixture selected an IPv6 Docker kind gateway and constructed a malformed, unbracketed URL. The final fixture explicitly selects IPv4; production origin parsing now validates the port and rejects malformed endpoints. That failure was a fixture routing error, not evidence that Kubernetes credential delivery was unsupported.

All **303 unit tests pass** (`ev_1ed7881176de25137d3fe2e7086ed1b22039c7a393780b734d179cec6c51ca1a`), including profile lifecycle, trust/PassRole, persistence, transaction rollback, rotation, live policy changes, signature/token/expiry checks, broker isolation and CLI wiring. The [validation index](research/rea/ec2-iam-validation-index.json) and [validation bundle](research/rea/ec2-iam-validation-evidence.json.gz) bind the clean final Docker capture and unit run to the implementation sources. In total, **20 live checks** passed across Docker/SDK and Kubernetes.

## Reproduce

```bash
python3 scripts/ec2-iam-reference.py --api-only
python3 scripts/ec2-iam-reference.py
python3 scripts/ec2-iam-kube-reference.py
python3 scripts/ec2-iam-live.py
python3 scripts/ec2-iam-kube-live.py
python3 -m unittest discover -s tests -q
```

The live scripts require existing Docker images/cluster resources described in their setup. They create and clean their own resources. The Kubernetes fixture uses the caller-authorized loopback proxy, not a new minimal-RBAC claim.

## Remaining boundaries

This closes the documented basic instance-role credential workflow, not the entire IAM/STS service. Managed policies, policy conditions, permissions boundaries, resource/session policies, federation, MFA, broader STS APIs, multi-account/region isolation and production-grade identity security remain outside this subset. Signed streaming payloads, presigned temporary-credential requests and unsigned-payload modes are excluded.

Runtime profiles require an enabled metadata helper and a backend origin reachable from the guest. Existing network-disabled Docker instances cannot be silently turned into working credential clients by attaching a profile. The AWS SDK still needs explicit LocalCloud service endpoints; obtaining local credentials does not redirect real AWS calls.
