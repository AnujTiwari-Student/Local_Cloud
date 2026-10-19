# Evidence ledger

## Boundaries

No proprietary LocalStack source, container image, entitlement mechanism or authenticated service execution was inspected. This project is independently written from public behavior descriptions, AWS API concepts and the supplied dashboard screenshot. It makes no claims about LocalStack's private implementation or the 2026 image/repository changes described in the conversation.

## Evidence and design mapping

| ID    | Source and observation                                                                                                                                                                                                                                                                                      | Status                                                                                                                     | LocalCloud implementation                                                                                                        |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| UI-1  | User screenshot of `/inst/default/resources`: instance selector, categorized service catalog, search, Cloud Pods, Stack Insights and Extensions navigation.                                                                                                                                                 | Direct visual observation; no working service execution shown.                                                             | Sidebar and resource search, scoped to the implemented S3/SQS resources. Other screenshot services are not claimed as available. |
| UI-2  | Screenshot warns the web app cannot connect to the running LocalStack instance.                                                                                                                                                                                                                             | Direct visual observation; cause unknown.                                                                                  | Dashboard fetches local state and reports connection success/failure.                                                            |
| WEB-1 | Automation browser navigating to both `/` and `/inst/default/resources` redirected to `/sign-in`.                                                                                                                                                                                                           | Runtime observation in the automation session only; user's screenshot establishes their separate session is authenticated. | No authenticated dashboard behavior inferred.                                                                                    |
| DOC-1 | [LocalStack persistence](https://docs.localstack.cloud/aws/developer-tools/snapshots/persistence/) describes snapshot-based durability and configurable save strategies.                                                                                                                                    | Public documentation, not internal-code evidence.                                                                          | Independent choice: commit successful operations to SQLite instead of implementing LocalStack's snapshot scheduler.              |
| DOC-2 | [LocalStack snapshots](https://docs.localstack.cloud/aws/developer-tools/snapshots/) describes local/remote saves, persistence and merging. [Cloud Pods](https://docs.localstack.cloud/aws/developer-tools/snapshots/cloud-pods/) documents hosted snapshots and version compatibility limits.              | Public documentation.                                                                                                      | Local named snapshots with atomic replacement restore; hosted sharing and merge are outside current scope.                       |
| DOC-3 | [IAM enforcement](https://docs.localstack.cloud/aws/developer-tools/security-testing/iam-policy-enforcement/) documents enabling enforcement. [IAM policy stream tutorial](https://docs.localstack.cloud/aws/tutorials/iam-policy-stream/) describes soft mode logging violations while allowing API calls. | Public documentation.                                                                                                      | Independent limited policy evaluator with off/soft/enforce modes. No attempt at complete AWS IAM semantics.                      |
| API-1 | [AWS SQS API reference](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/Welcome.html) defines queue and message operations.                                                                                                                                                           | Public API contract; emulator implements a subset.                                                                         | SQS JSON operations, timed invisibility and rotating receipt handles.                                                            |

## How this implementation works

`localcloud/engine.py` serializes state into a single SQLite record under a process lock. Successful transactions persist changes; rejected calls roll back mutations. Enforced access denials persist their audit record. Snapshot rows capture the complete state string in the same SQLite database; restore changes only the active state row.

SQS stores messages with an availability timestamp. Receive assigns a fresh receipt and moves that timestamp forward. A subsequent receive can redeliver an expired message and invalidate the old receipt. Delete requires the current, unexpired receipt. S3 stores binary bytes as base64 strings indexed by bucket and object key.

IAM policies belong to local principal names, selected explicitly by a request header. The evaluator matches action/resource patterns, applies explicit deny precedence and otherwise requires an allow. Soft mode records violations and continues. Unsupported condition/role policy constructs are rejected rather than approximated.

`localcloud/__main__.py` exposes SQS JSON, S3 path-style XML/binary responses and local management endpoints. `dashboard.html` uses those management endpoints to display actual persisted resources. Management access is intentionally a trusted-loopback development interface.

These are our implementation choices. They are not findings about LocalStack's private architecture.

## Initial baseline verification

- 10 engine tests pass: persistence, snapshot replacement, delivery/acknowledgement, redelivery receipt rotation, enforcement, policy rejection, soft-mode logging, concurrency, bucket constraints and invalid-request rollback.
- 5 HTTP handler tests pass: dashboard/SQS JSON, S3 binary/XML, snapshot routes/content type, malformed/unsupported requests, and principal enforcement.
- Live TCP server and browser integration remain unverified: creating sockets is denied by this environment.
- Full Pro feature parity remains unresolved and requires concrete per-service requirements. Only the implemented subset in README is represented as complete.

## Expanded developer tooling

The supplied paid-tier guide was used as a requirements inventory, not treated as proof of plan entitlements. [Feature coverage](FEATURES.md) maps every numbered area to executable functionality or open work.

| ID | Official source | Observed documented capability | Independent implementation |
|---|---|---|---|
| DOC-4 | [Instance management](https://docs.localstack.cloud/aws/connecting/console/instance-management/) | State transfer, policy stream, inspector and fault controls are exposed as instance tools. | Separate local management endpoints for the same broad tasks. |
| DOC-5 | [App Inspector](https://docs.localstack.cloud/aws/developer-tools/app-inspector/) | Investigates operation flow, exchanged data, errors and IAM issues. | Bounded metadata-only request records. No equivalent claim for payload capture or service dependency graphs. |
| DOC-6 | [MCP tooling reference](https://docs.localstack.cloud/aws/developer-tools/running-localstack/mcp-server/) | Describes enforced/soft/disabled IAM modes, policy proposals from logs, and fault rules using service/operation/probability/error plus latency. | Plain REST/Python implementations of policy proposals and fault configuration; no MCP server. |
| DOC-7 | [Chaos API](https://docs.localstack.cloud/aws/developer-tools/chaos-engineering/chaos-api/) | Documents fault injection as a developer testing mechanism. | Persistent local rules applied before service execution. Our endpoint and schema are not the LocalStack Chaos API or AWS FIS contract. |
| DOC-8 | [Automatic state loading](https://blog.localstack.cloud/how-to-automatically-load-state-in-localstack/) | Describes state loading approaches for repeatable environments. | LocalCloud `--load-state` and `--export-state`, using our own versioned JSON format. |

### Pricing verification boundary

The public [pricing page](https://www.localstack.cloud/pricing), inspected during this task, lists Base at $39 per license/month with annual billing ($45 monthly), 55+ services, 300 MB workspace state storage and 100 monthly preview minutes. Ultimate is listed at $89 with annual billing, 110+ services, 3 GB workspace state storage and 500 monthly preview minutes. These are reference facts at inspection time, not promises or features implemented here.

Some comparison-table inclusions are rendered as unlabeled images in the text extraction. Their plan assignment, CI credit counts, the exact Hobby restrictions, add-on packaging and enterprise pack contents were not established by that extraction and remain unverified. Image unification and repository history were not investigated. No dependency on these commercial plan details was added to LocalCloud.

### Implementation evidence

- `localcloud/tooling.py`: versioned/checksummed state envelopes; full import validation before atomic replacement; trace records capped at 1,000; action/resource policy proposals; validated fault rules.
- `localcloud/engine.py`: SQS/S3 operation wrappers capture outcomes and IAM decisions. Faults run before service state mutations. Per-thread request context keeps concurrent evaluations separate.
- `localcloud/__main__.py`: management REST routes and headless CLI state import/export.
- `localcloud/dashboard.html`: searchable request details, editable policy proposals, file import/download and fault controls.
- Resource snapshots/state bundles intentionally exclude instance traces and fault rules. Those persist independently across restart. State bundles exclude named snapshot history as well.

### Updated validation

28 Python tests pass, including 9 tooling tests, 7 HTTP handler tests, 10 engine tests and 2 CLI process tests. Policy proposals are never auto-applied. Invalid bundle checksums, schema, binary encoding and policy shape are rejected without replacing active state. Error injection prevents mutation; unrelated services and management remain operational. Probability and latency tests use controlled random/sleep functions instead of timing-sensitive assertions.

A browser smoke test served the real dashboard HTML with intercepted fixture API responses at `https://localcloud.test/_local`. It verified connected resource rendering, search, policy proposal generation, fault creation/removal and trace clearing; no page errors occurred. This establishes UI behavior against fixtures, not live end-to-end server integration. Live TCP serving remains unverified in the socket-restricted sandbox.

## Kubernetes pack and CI/CD implementation

Public primary references used for the implementation:

- [Kubernetes operator pattern](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/) and [custom resources](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/): desired-state resources combined with a controller. LocalCloud implements its own namespace-scoped polling reconciler and `LocalCloudInstance` CRD.
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/) and [finished-Job TTL](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/): workload completion and cleanup mechanisms. LocalCloud builds bounded, non-retrying Jobs and sets `ttlSecondsAfterFinished`.
- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) and the [Helm Kind action](https://github.com/helm/kind-action): workflow structure and disposable cluster setup. The included workflow uses separate Python, container and Kubernetes jobs.

These references establish public platform mechanisms, not the internals or compatibility of LocalStack's proprietary Kubernetes executor.

### Code and behavior

- `localcloud/kube.py`: verified-TLS/in-cluster REST client, projected token rereading, paginated lists, workload image allowlist, namespaced Jobs with ownership checks, status/log/cancel operations and cleanup deadlines.
- `localcloud/operator.py`: renders PVC/Service/Deployment from allowlisted fields, respects API-defaulted fields, reconciles readiness, rejects foreign resources, handles retained storage and expires instance CRs using UID delete preconditions.
- `localcloud/kube_manifests.py`: generates CRD, namespaced RBAC, service accounts, Job quota and operator Deployment. No cluster-wide runtime role or Secret-read permission is granted to the controller/executor.
- `localcloud/__main__.py`: optional management bearer protection, mandatory token for enabled execution, advertised Service endpoint and execution REST routes.
- `localcloud/ci.py`: independent temporary state/server per run, live HTTP smoke routine, fixture validation, per-run token, optional application command, artifact writing and cleanup on failure.
- `Dockerfile`, `.github/workflows/ci.yml`, `scripts/container_smoke.py`, `scripts/kubernetes_smoke.py`: image build and runnable live validation jobs.
- Dashboard: management-token entry stays in memory; job submission, logs and deletion call protected backend endpoints.

### Verification and limits

52 Python tests pass. Added tests cover namespace-scoped resource generation/RBAC, reconciliation idempotence with API defaults, readiness, retained PVC behavior, allocated Service IP preservation, expiry, foreign-resource rejection, Job ownership and log checks, image/command validation, token rotation, pagination, bearer gates, runner exit codes/artifacts and failure cleanup. The HTTP smoke routine passes against the real handler using an in-memory connection; Kubernetes behavior uses a fake API contract.

A browser test against intercepted fixture responses passed: unauthenticated rejection, token login, Job submission, log viewing and deletion, with no page errors. Workflow YAML parsed successfully; CLI help/manifests and Python/JavaScript syntax checks passed.

A live `python3 -m localcloud.ci --artifacts /tmp/localcloud-ci-validation` attempt exited 1 with `[Errno 1] Operation not permitted`, preserving a failure report. `docker info` was denied access to `/var/run/docker.sock`. No live Kubernetes deployment or external GitHub Actions run was performed. The installed kubectl client was inspected without changing a cluster. The provided live container/Kind jobs still need execution in a capable environment.

This is a Kubernetes/CI implementation subset: no AWS Lambda/ECS/EC2 API compatibility, warm runtime pool, managed ingress/hosted cloud service, credit billing or issuer for commercial CI tokens. Instance previews remain private cluster Services. The operator must run for instance TTL cleanup; Kubernetes itself handles completed Job TTL. Management tokens are an access gate for development tools, not complete multi-tenant authentication for AWS service routes.
