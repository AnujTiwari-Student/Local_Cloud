# LocalCloud

**Start here: [Complete handbook](docs/HANDBOOK.md)** — setup, real application
integration, local CI, EC2, Kubernetes, core-engine internals and troubleshooting.
The [documentation index](docs/README.md) links the specialist guides.
Contributors can start with [the future-service build guide](docs/HANDBOOK.md#26-contributor-guide-to-the-future-project): core scope, optional add-ons, independent backends and acceptance scenarios for all 51 screenshot services.

An independent, dependency-free Python development emulator. This implementation provides EC2, SQS and S3 subsets, durable state, named snapshots, state portability, IAM policy proposals, request inspection, fault injection and a resource dashboard. It is a starting project for adding the specific services you need, not full AWS or LocalStack Pro parity.

Use the [AWS-style CLI guide](docs/CLI.md) for every supported service and tool. Also see [getting started](docs/USAGE.md), [Kubernetes setup](docs/KUBERNETES.md), and [CI/CD usage](docs/CI_CD.md).

For browser-based creation, details and lifecycle controls, follow the [dashboard and instance connection guide](docs/DASHBOARD.md). Start a named server, connect, then manage EC2, S3 and SQS from the same endpoint.

The [public-engine investigation](docs/ENGINE_RE.md) records pinned LocalStack source evidence, the first gateway/provider migration and remaining compatibility gaps. Inspect it at runtime with `localcloud engine status`.

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

The latest saved EC2 release audit records **181 passing tests and 11 live
regression checks**, plus separate live networking/browser and Docker AMI clone
checks. These verify their recorded scopes, not full AWS/LocalStack compatibility.
See [the current release guide](docs/EC2_RELEASE.md) and its linked evidence for
capture details. Run the suite above to check your current checkout.

## Compatibility limits

- Fixed account `000000000000` and region `us-east-1`; no multi-account or region isolation.
- SQS JSON protocol only; no Query protocol, FIFO, long polling, batching, message attributes, retention scheduler, DLQs or production delivery guarantees.
- S3 path-style only; no signing verification, presigned URL validation, multipart upload, versioning, encryption, object metadata, pagination or bucket policies. Object data lives as base64 inside JSON state; requests are limited to 8 MiB. SDK compatibility has not been verified; the bundled CLI is tested against our handler.
- IAM supports only identity policies containing Sid, Effect, Action and Resource. No Condition, Principal, resource policies, roles, STS, SCPs, permissions boundaries or signature validation.
- Named snapshots are local database records. No Cloud Pods integration, remote sharing, merge or cross-version migration. Portable resource-state export/import is available separately.
- Dashboard supports S3 object editors, SQS message/settings controls and selected EC2/infrastructure forms. It does not provide every service or every operation shown in the reference screenshot.
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

Services: `s3`, `sqs`, `ec2`, or `*`; operations accept `*`/`?` patterns. Probability is 0–1. Latency is 0–5,000 ms. Omit `error` for latency only. Rules are evaluated in name order; the first rule whose service/operation match and probability triggers wins. Latency occurs outside the state lock. Errors happen before authorization and mutation, so injected failures have no IAM evaluation. Management endpoints bypass faults so rules can always be removed. Rules persist across restart, but are deliberately not transferred or reverted with resource state.

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

Follow [Kubernetes usage](docs/KUBERNETES.md) to load the image into your cluster, install the operator, create its token Secret, launch instances and execute Jobs. Follow [CI/CD usage](docs/CI_CD.md) for project configuration, application tests, fixtures, tokens and artifacts. Neither guide requires a LocalStack subscription. Saved evidence includes isolated Docker and kind runtime checks. Standalone smoke scripts let you verify the relevant workflow in your own environment.

## Add local CI to your own application

In your application's directory, beside its package.json:

```bash
localcloud ci init --command npm test
localcloud ci
```

This creates and reads `localcloud.toml`. The file declares your test command, supported service dependencies, initial resources, policy files and environment settings. No remote CI configuration is required.

See [the complete Node.js walkthrough](docs/NODEJS_LOCAL_CI.md) and [the runnable app](examples/node-app/README.md). The example covers S3/SQS processing, IAM denial, snapshots, state transfer, faults and request inspection, with a separate optional Kubernetes workload recipe.

For a reachable Docker-backed EC2 web app, see [the complete walkthrough](docs/EC2.md#deploy-a-web-application) and [sample app](examples/ec2-web/README.md).

The [REA MCP service audit](docs/REA_SERVICES.md) records the latest reference image, original Evidence IDs, 11 live checks (including Docker), a source-backed SQS correction and unresolved compatibility work.

See [EC2 networks, volumes, IMDS and SSH](docs/EC2_INFRASTRUCTURE.md) for the networking/storage milestone, local setup and captured verification results.
The later release checkpoint below expands the EC2 subset to 97 AWS operations.

The [EC2 networking REA report](docs/EC2_NETWORKING.md) covers private IPs, network
interfaces, elastic-IP associations, CLI/dashboard usage and actual traffic limits.

The latest [EC2 release checkpoint](docs/EC2_RELEASE.md) covers 97 selected AWS
operations, launch templates, image lifecycle and snapshot-copy workflows, with
usage, verification and explicit remaining limits.
