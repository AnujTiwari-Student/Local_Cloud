# LocalCloud

An independent, dependency-free Python development emulator. This implementation provides SQS and S3 subsets, durable state, named snapshots, state portability, IAM policy proposals, request inspection, fault injection and a resource dashboard. It is a starting project for adding the specific services you need, not full AWS or LocalStack Pro parity.

Use the [AWS-style CLI guide](docs/CLI.md) for every supported service and tool. Also see [getting started](docs/USAGE.md), [Kubernetes setup](docs/KUBERNETES.md), and [CI/CD usage](docs/CI_CD.md).

## Run

Requires Python 3.11 or newer. No pip dependencies.

```bash
export PATH="$PWD/bin:$PATH"
localcloud start
```

For a persistent command, run `python3 scripts/install-cli.py` and ensure `~/.local/bin` is in PATH. No pip is required. Existing `python3 -m localcloud` entry points still work.

Open **http://127.0.0.1:4566/_local**. Data persists in `.localcloud/state.sqlite3`.

```bash
python3 -m localcloud --port 4566 --data .localcloud/state.sqlite3 --iam soft
# Reject unauthorized service operations instead of only logging them:
python3 -m localcloud --iam enforce
```

Stop the first process before starting another. The default bind address is loopback. This is a trusted local testing tool: management APIs support an optional bearer token (required for Kubernetes execution), identities are supplied through `X-Local-Principal`, and AWS signatures are not validated. Do not expose it as a shared or public service. One process owns each database.

See [EC2 usage](docs/EC2.md) for persistent mock instances and optional Docker-backed execution.

## Implemented features

| Feature            | Behavior                                                                                                                                                                                                                |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Persistence        | Each successful state operation commits to SQLite; state survives process restart.                                                                                                                                      |
| Named snapshots    | Capture all queues, messages, bucket contents, policies and audit entries. Restore replaces current state atomically. Duplicate names are rejected. Snapshots remain available after restore.                           |
| IAM testing        | Off, soft or enforce mode. Policies attached to local principal names support Action/Resource wildcards and Allow/Deny. Explicit deny wins; otherwise an allow is required. Unsupported policy constructs are rejected. |
| SQS                | Create/List/Get/Delete queues; attributes; send, receive, delete messages; purge; change visibility. Receipts rotate on delivery.                                                                                       |
| S3                 | Path-style create/list/delete buckets; put/get/delete binary objects; list objects with XML responses.                                                                                                                  |
| EC2                | Run/describe/start/stop/terminate; durable mock instances or optional bounded Docker containers. [Usage and limitations](docs/EC2.md). |
| Dashboard          | Connection status, resource search/counts, snapshots, state transfer, policies, request inspector and fault controls.                                                                                                   |
| State transfer     | Validated versioned JSON export/import and startup seeding.                                                                                                                                                             |
| Request inspection | Bounded durable request metadata, outcomes, timings and IAM decisions.                                                                                                                                                  |
| Policy proposals   | Generate action/resource statements from successful calls or violations.                                                                                                                                                |
| Fault injection    | Service/operation rules, probability, error injection and latency.                                                                                                                                                      |

## Use the services

For an existing Kubernetes instance, keep its Service port-forward running and configure the client:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud auth login --kubernetes-secret localcloud-access --namespace localcloud
localcloud status
localcloud resources
```

Plain local instances without a management token do not need login.

```bash
localcloud s3 mb s3://assets
printf 'hello world' > hello.txt
localcloud s3 cp hello.txt s3://assets/hello.txt
localcloud s3 ls s3://assets
localcloud s3 cp s3://assets/hello.txt downloaded.txt

localcloud sqs create-queue --queue-name jobs
localcloud sqs send-message --queue-name jobs --message-body 'hello'
localcloud sqs receive-message --queue-name jobs

localcloud snapshots save baseline
localcloud traces list
localcloud jobs list
```

Use `localcloud <service> <operation> --help` for flags, such as `localcloud sqs receive-message --help`. See [complete CLI usage](docs/CLI.md) for acknowledgements, policies, state transfer, faults, jobs, profiles and CI. The [HTTP reference](docs/HTTP_API.md) remains available for API integrations.

## Tests and verification

```bash
python3 -m unittest discover -s tests -v
```

<<<<<<< HEAD
72 passing Python tests cover persistence, snapshots, policy enforcement/generation, portable state validation, fault rules, trace retention, CLI seeding/export and HTTP request/response handling. HTTP tests use an in-memory connection to exercise the real standard-library handler. Browser smoke tests passed resource/tool controls and protected Kubernetes job controls using fixture API responses, with no page errors. Kubernetes and CI unit tests cover reconciliation, ownership, credentials and cleanup. Live browser-to-server integration remains unverified because the authoring sandbox disallows listening sockets.
=======
<<<<<<< Updated upstream
52 passing Python tests cover persistence, snapshots, policy enforcement/generation, portable state validation, fault rules, trace retention, CLI seeding/export and HTTP request/response handling. HTTP tests use an in-memory connection to exercise the real standard-library handler. Browser smoke tests passed resource/tool controls and protected Kubernetes job controls using fixture API responses, with no page errors. Kubernetes and CI unit tests cover reconciliation, ownership, credentials and cleanup. Live browser-to-server integration remains unverified because the authoring sandbox disallows listening sockets.
=======
88 passing Python tests cover persistence, snapshots, policy enforcement/generation, portable state validation, fault rules, trace retention, CLI seeding/export and HTTP request/response handling. HTTP tests use an in-memory connection to exercise the real standard-library handler. Browser smoke tests passed resource/tool controls and protected Kubernetes job controls using fixture API responses, with no page errors. Kubernetes and CI unit tests cover reconciliation, ownership, credentials and cleanup. Live browser-to-server integration remains unverified because the authoring sandbox disallows listening sockets.
>>>>>>> Stashed changes
>>>>>>> 28aa52d (Feature: Add ec2 service)

## Compatibility limits

- Fixed account `000000000000` and region `us-east-1`; no multi-account or region isolation.
- SQS JSON protocol only; no Query protocol, FIFO, long polling, batching, message attributes, retention scheduler, DLQs or production delivery guarantees.
- S3 path-style only; no signing verification, presigned URL validation, multipart upload, versioning, encryption, object metadata, pagination or bucket policies. Object data lives as base64 inside JSON state; requests are limited to 8 MiB. SDK compatibility has not been verified; the bundled CLI is tested against our handler.
- IAM supports only identity policies containing Sid, Effect, Action and Resource. No Condition, Principal, resource policies, roles, STS, SCPs, permissions boundaries or signature validation.
- Named snapshots are local database records. No Cloud Pods integration, remote sharing, merge or cross-version migration. Portable resource-state export/import is available separately.
- Dashboard lists queue/bucket counts; individual object/message editors and the other service screens are not implemented.
- SQLite state is serialized as one JSON document. This favors small deterministic development workloads over throughput. Queue timing uses the host wall clock.
- No RDS, Cognito, Lambda, DynamoDB, API Gateway, CloudFormation or extensions yet.

## Evidence and next services

See [the evidence ledger](docs/EVIDENCE.md) for observed facts, official references and implementation choices. Supply the next feature's inputs, API operations, expected responses/errors and state changes to extend this project with a defined compatibility target.

## Request inspection and IAM policy proposals

Open Request Inspector in the dashboard, or use the CLI:

```bash
localcloud traces list
localcloud traces list --user-name alice
localcloud iam generate-policy --user-name alice --source successful
localcloud iam generate-policy --user-name alice --source violations
```

The latest 1,000 engine calls retain service, operation, resource, principal, timing, error and IAM evaluation. Request/response payloads and credentials are not captured. Management calls and malformed requests rejected before reaching the engine are not traced. The bounded history survives restart, but is excluded from state bundles and snapshot restoration. Traces include resource names, so treat the local database as development data.

Generated policies are proposals, not proof of complete least privilege. They preserve each observed action/resource pair rather than combining unrelated resources. Violations may reflect deliberately forbidden operations: review before using the normal policy-save endpoint. Nothing is granted automatically.

## Portable state and startup seeding

```bash
localcloud state export baseline.json
localcloud state import baseline.json

# Operate on a stopped database, or start a seeded instance:
localcloud start --export-state baseline.json
localcloud start --data .localcloud/seeded.sqlite3 --load-state baseline.json
```

The versioned JSON bundle includes a SHA-256 checksum for corruption detection (not authenticity), queue/message state, objects, policies and audit records. Import validates the complete schema before replacing live state. Canonical state is limited to 6 MiB for portability; larger local environments can still use named snapshots. No merging occurs. Existing named snapshots, trace history and fault rules remain on the destination instance. Message visibility uses absolute timestamps, so elapsed time during transfer counts toward visibility expiry. Queue URLs are rebuilt using the destination endpoint.

## Fault injection

Configure and remove rules through the dashboard, or:

```bash
localcloud faults add --name s3-outage --service s3 --operation PutObject \
  --probability 1 --latency-ms 200 --error-code ServiceUnavailable --status-code 503
localcloud faults list
localcloud faults remove s3-outage
```

Services: `s3`, `sqs`, or `*`; operations accept `*`/`?` patterns. Probability is 0–1. Latency is 0–5,000 ms. Omit `error` for latency only. Rules are evaluated in name order; the first rule whose service/operation match and probability triggers wins. Latency occurs outside the state lock. Errors happen before authorization and mutation, so injected failures have no IAM evaluation. Management endpoints bypass faults so rules can always be removed. Rules persist across restart, but are deliberately not transferred or reverted with resource state.

For the full guide-to-code mapping, see [feature coverage](docs/FEATURES.md). Public cloud sandbox hosting, expanded AWS providers and enterprise controls remain open work. Private Kubernetes previews and Job execution are available; see the guides above.

## Kubernetes and CI quick start

Run an isolated backend with live HTTP smoke checks and diagnostics:

```bash
localcloud ci
# Or run your own test command against the temporary endpoint:
localcloud ci -- python3 -m unittest discover -s tests -v
```

Build and generate installation manifests for a development cluster:

```bash
docker build -t localcloud:dev .
localcloud kube --namespace localcloud manifests --image localcloud:dev > /tmp/localcloud-install.json
```

Follow [Kubernetes usage](docs/KUBERNETES.md) to load the image into your cluster, install the operator, create its token Secret, launch instances and execute Jobs. Follow [CI/CD usage](docs/CI_CD.md) for project configuration, application tests, fixtures, tokens and artifacts. Neither guide requires a LocalStack subscription. Live Docker/cluster validation could not run in the authoring sandbox; standalone live smoke scripts are provided for a capable environment.

## Add local CI to your own application

In your application's directory, beside its package.json:

```bash
localcloud ci init --command npm test
localcloud ci
```

This creates and reads `localcloud.toml`. The file declares your test command, supported service dependencies, initial resources, policy files and environment settings. No remote CI configuration is required.

See [the complete Node.js walkthrough](docs/NODEJS_LOCAL_CI.md) and [the runnable app](examples/node-app/README.md). The example covers S3/SQS processing, IAM denial, snapshots, state transfer, faults and request inspection, with a separate optional Kubernetes workload recipe.
