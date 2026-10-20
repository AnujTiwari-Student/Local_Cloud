# REA MCP investigation of current services

**Follow-up:** the auth-token blocker described below is resolved. See
[EC2 and Kubernetes runtime comparison](REA_EC2_KUBERNETES.md) for authenticated
2026.10.0 captures, live kind checks, the Query compatibility fix and remaining gaps.

**Date:** 20 October 2026. **Tool:** REA MCP 6.7.0. This pass used actual REA tool calls, with retained Evidence IDs and an exported evidence bundle. It is an investigation of the implemented LocalCloud services and a pinned current LocalStack image; it does not claim all services have been reconstructed or made equivalent.

## Reference selected by “latest”

REA pulled `localstack/localstack:latest`, inspected its distribution metadata, and recorded:

- Version: **2026.10.0**, build date **2026-10-13**, build Git hash **738d58ee**.
- Immutable reference: `localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
- The image metadata lists both `localstack/localstack` and `localstack/localstack-pro` repository digests with this digest.
- Package metadata includes `localstack-core`, `localstack-pro-core`, `localstack-pro-apps` and Rolo 0.8.8. Relevant package paths include `.py.enc` modules. Names, sizes and hashes were inspected; encrypted code was not decrypted.

The isolated reference container was started normally with no credentials, no host mounts, no published ports, no Docker socket and networking disabled. It exited with **code 55**, reporting that `LOCALSTACK_AUTH_TOKEN` was missing. The container was removed. This establishes a startup/authentication blocker; it does not establish service behavior or feature entitlements. No authenticated latest-version S3, SQS, IAM, state, chaos or Kubernetes API comparison was possible in this pass.

Reference identity and selected file metadata are in [reference-image.json](research/rea/reference-image.json). The downloaded reference image remains cached locally; the disposable inspection containers were removed. Existing LocalCloud servers, databases and workload containers were not used as audit fixtures or stopped.

The public GitHub source investigated earlier remains a separate reference at `8b9a79f05846835cf4dff63ab7eefdde9df83783`. Its source findings must not be presented as observations of the October image. See [ENGINE_RE.md](ENGINE_RE.md).

## What REA actually did

| REA operation                    | Target and result                                                                                                                                                                   | Evidence                                                                                                                                                      |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `analyze_javascript_application` | Parsed LocalCloud's dashboard JavaScript/HTML as inert text; one JS file parsed, no parse failures. Static semantic relationships remain partial/unknown.                           | `ev_171de1060fc70a0394cfe6b50f375aa40880e181aa0db87af4dc1a3f7da7ffd6`                                                                                         |
| `inspect_analysis_view`          | Read summary/module identities without repeating analysis. The summary retains **827 semantic unknowns**.                                                                           | [dashboard-analysis.json](research/rea/dashboard-analysis.json)                                                                                               |
| `trace_application_feature`      | Traced the dashboard module relationship. A separate exact `SendMessage` literal trace had no matching application-graph entity; this is not evidence that the operation is absent. | `ev_1530884744edffc1035411cba0620d1f5e8422fff0c64bce8037d2b3499a09e2`, `ev_d40dcaa64cb64c0362c1c1ca080dee93551838b23156425f8303a5c094938186`                  |
| `capture_process_scenario`       | Captured the image pull, immutable image identity, Python distribution metadata and normal startup failure.                                                                         | Identity `ev_12686c301921b26121805670129c71e0ebf76d68bcde85ae3c1234fcb3d1f5db`; startup `ev_8c8901e5385cf321c46daa91a24565f4ba2568b08e5dbfced60be918f2fb811e` |
| `capture_process_scenario`       | Captured a reproducible LocalCloud audit: **131 tests, zero failures/errors/skips, and 11 live checks**, including real HTTP and Docker execution. Final capture is not truncated.  | `ev_f9b32fac2b415f0b4210fbefc117cc5c1704fc08cef1bf3c68677e78f5a84e49`                                                                                         |
| `compare_process_captures`       | Compared identical SQS probes before/after the source-backed queue creation fix, including recorded engine-file hashes.                                                             | `ev_7424cbc18057b22c82340156f941cbbbb0e4a40154801f0562fc89a9e59dcd39`                                                                                         |
| `export_evidence_bundle`         | Exported 26 retained records and five residual-unknown records, preserved as a compressed original bundle.                                                                          | [Evidence index and hashes](research/rea/evidence-index.json), [original bundle](research/rea/evidence-bundle.json.gz)                                        |

The REA process runner could access the host Docker daemon, unlike the restricted shell used earlier. That allowed genuine live Docker validation in this pass. These captures record process output and sampled process/filesystem facts; they do **not** independently intercept or record all network traffic or prove causal equivalence.

## Coverage across everything currently implemented

| Current component                         | Observed in this pass                                                                                                                                                                         | Still unknown / not established                                                                                                                                             |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S3 subset                                 | Live HTTP binary put/get, XML listing, object sizes and nonempty-bucket deletion error; existing CRUD/dashboard contracts pass. Public provider source was inspected as historical reference. | Latest LocalStack equivalence, signing, multipart, versions, ACL/policy/encryption and comprehensive error/serialization compatibility.                                     |
| SQS subset                                | Live HTTP delivery, visibility hiding, receipt rotation, acknowledgement, settings and repeated queue creation; existing CLI/dashboard contracts pass.                                        | Latest LocalStack equivalence, FIFO, batching, long polling, retention/DLQ and unsupported attributes.                                                                      |
| EC2                                       | Live mock API lifecycle/names/type modification. **Real Docker web app** reached through a loopback-published port, stopped, resized, restarted and terminated; container removal checked.    | Paid LocalStack Docker VM manager internals/behavior. No actual VM, VPC/EBS or EKS equivalence claim.                                                                       |
| IAM enforcement                           | Live allow and deny decisions for a selected local test principal.                                                                                                                            | This is a local policy evaluator, not an implemented AWS IAM/STS identity service. Full policy grammar/authentication and latest enforcement semantics are not established. |
| Policy proposals                          | Live generated action/resource proposal from observed calls.                                                                                                                                  | Least-privilege completeness or compatibility with LocalStack's policy stream.                                                                                              |
| Persistence                               | Resources survived a full backend shutdown/reopen on the audit database.                                                                                                                      | LocalStack state-store format, multi-account/region partitioning or migration compatibility.                                                                                |
| Snapshots / portable state                | Save, restore, export/import, atomic error behavior and checksum rejection exercised with synthetic fixtures.                                                                                 | Cloud Pods, hosted storage/sharing/version history and compatibility with LocalStack snapshot files.                                                                        |
| Fault injection                           | Real HTTP service error, continued management access and successful recovery after removing the rule.                                                                                         | LocalStack chaos/FIS API or network fault semantics.                                                                                                                        |
| Traces / inspector                        | Live request metadata, injected-fault status, correlated request IDs and policy evidence.                                                                                                     | Full payload/span capture, service dependency graphs and equivalence to LocalStack App Inspector.                                                                           |
| CLI / instance connection                 | Real subprocess connected to the isolated backend using a temporary private profile, then listed shared resources.                                                                            | Compatibility with LocalStack's own CLI/authentication model.                                                                                                               |
| Dashboard                                 | REA static analysis and module trace; existing handler/UI contracts passed. Earlier browser fixture checks remain documented separately.                                                      | This REA pass did not execute a browser scenario against the latest LocalStack web app. Static candidate edges are not executed feature traces.                             |
| Local project CI                          | Real isolated CI server, S3/SQS smoke checks, application subprocess, diagnostic report and cleanup passed.                                                                                   | Hosted CI tokens/credits, general workflow/DAG execution or managed CD. This is not AWS CodeBuild/CodePipeline emulation.                                                   |
| Kubernetes operator / executor / previews | **16 existing contract tests passed**, covering the project's reconciliation/execution boundaries.                                                                                            | No live Kubernetes cluster reconciliation or pod execution was performed in this pass; no LocalStack Kubernetes Pack parity claim.                                          |
| Core gateway                              | Existing context, provider loading, operation routing, concurrency, metadata and request-ID tests passed.                                                                                     | Full Rolo/Botocore runtime, AWS protocol models and account/region isolation remain outside the current migration.                                                          |

Planned providers such as DynamoDB, EKS, RDS and Lambda do not become implemented or reverse engineered through this audit. Enterprise/team/extension/MCP features remain at their previously documented status.

## Concrete source-backed SQS correction

The archived public [`SqsProvider.create_queue`](https://github.com/localstack/localstack/blob/8b9a79f05846835cf4dff63ab7eefdde9df83783/localstack-core/localstack/services/sqs/provider.py#L759) checks existing queue settings against **attributes actually supplied**. LocalCloud previously compared a missing visibility attribute with its default of 30, incorrectly rejecting an existing queue configured to 90.

REA captured this exact four-case probe before and after the correction:

| Re-create a queue whose visibility timeout is 90 | Before            | After                 |
| ------------------------------------------------ | ----------------- | --------------------- |
| Attributes omitted                               | `QueueNameExists` | Success, existing URL |
| Empty attributes object                          | `QueueNameExists` | Success, existing URL |
| Explicit timeout 90                              | Success           | Success               |
| Explicit timeout 30                              | `QueueNameExists` | `QueueNameExists`     |

Changed [`localcloud/engine.py`](../localcloud/engine.py) to compare only an explicitly supplied visibility timeout. Added a regression test and repeated the live audit. Names/settings are preserved. The [before/after record](research/rea/sqs-idempotence-fix.json) links the two process captures and comparison.

This is a demonstrated LocalCloud correction guided by a pinned source contract. It is **not** an authenticated differential comparison with LocalStack 2026.10.0. Other SQS and S3 compatibility gaps remain.

## Reproduce the LocalCloud runtime audit

From the repository root, on a host where Python can bind loopback sockets:

```bash
python3 scripts/rea-runtime-audit.py
```

For the additional real Docker web-app lifecycle check, build the included sample if needed, then select it explicitly:

```bash
docker build -t localcloud-ec2-web:dev examples/ec2-web
python3 scripts/rea-runtime-audit.py --docker-image localcloud-ec2-web:dev
```

The script uses new temporary databases, temporary CLI profiles, ephemeral loopback ports, synthetic resources and a uniquely identified workload container. It does not connect to an existing emulator or Kubernetes cluster. It prints a compact JSON report with source hashes and per-test-module counts. `--contracts-only` runs the contract suite without live servers or Docker. For new authenticated REA evidence, invoke the script through `capture_process_scenario`; running it in a terminal produces a report, not an REA Evidence ID.

A host port is selected through a temporary socket reservation and released before Docker binds it; an unrelated process can still win that race, in which case the audit fails rather than replacing the listener. Docker workload cleanup is attempted in a `finally` block. No arbitrary Docker image is pulled or built by the audit script itself.

## Remaining input for latest-version reconstruction

A **valid LocalStack auth token with the required service entitlements** is needed to start the pinned reference normally and compare service APIs. It is different from `LOCALCLOUD_MANAGEMENT_TOKEN`; your LocalCloud server-token file cannot activate LocalStack. Keep credentials local and out of chat, reports and Git.

The next comparison must use disposable reference resources and record raw status/response/state differences per service. Kubernetes requires a separately selected disposable cluster/namespace and a supported reference deployment. Until those reference observations exist, the correct result is **REA inspection and local behavior verified; latest LocalStack service parity unresolved**.
