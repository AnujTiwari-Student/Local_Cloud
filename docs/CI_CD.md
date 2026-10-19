# Local project CI

<<<<<<< HEAD
Prefer commands over curl? See the [CLI guide](CLI.md), including automatic token login from a Kubernetes Secret.

=======
<<<<<<< Updated upstream
>>>>>>> 28aa52d (Feature: Add ec2 service)
LocalCloud now has a CI runner, GitHub Actions workflows and a Kubernetes integration smoke test. There is no hosted billing/credit service or externally issued CI license token; these tools run in your own CI infrastructure.
=======
Put **`localcloud.toml` beside your application's `package.json`**, then run **`localcloud ci` from that directory**. The config tells LocalCloud which tests to execute and which supported resources to prepare.

For a complete Node.js application, source files, tests and every setup step, see [the start-to-finish Node.js guide](NODEJS_LOCAL_CI.md) and [the runnable example](../examples/node-app/README.md).
>>>>>>> Stashed changes

## Create the config in your app

```bash
cd /path/to/my-app
localcloud ci init --command npm test
```

Generated file:

```toml
version = 1

[ci]
command = ["npm", "test"]
services = ["s3", "sqs"]
iam = "soft"
timeout = 300
artifacts = "artifacts/localcloud"
```

The command array is passed directly, without a shell. The initializer refuses to overwrite an existing file. It creates no remote workflow files and does not change your application's code or install its dependencies.

Run:

```bash
localcloud ci
```

The runner creates a temporary database/backend on a free loopback port, runs backend smoke checks, starts your app's test command, writes diagnostics, and removes the backend/database. Your tests must assert application behavior; LocalCloud does not infer correctness from the app's source code.

## Prepare services, policies and app settings

```toml
version = 1

[ci]
command = ["npm", "test"]
services = ["s3", "sqs"]
iam = "enforce"
principal = "app"
timeout = 120
artifacts = "artifacts/localcloud"
export_state = false

[ci.seed]
buckets = ["app-documents"]
queues = ["app-jobs"]

[ci.policy_files]
app = "ci/policies/app.json"

[ci.env]
DOCUMENTS_BUCKET = "app-documents"
JOBS_QUEUE = "app-jobs"
```

Policy files use the limited identity-policy format already supported by LocalCloud. They replace that principal's policy before application tests run. Enforce mode requires matching permissions; default principal `test` does not automatically inherit the runner's internal smoke policy.

`services` declares dependencies and rejects unimplemented provider names. It does not disable the other supported provider or change the built-in S3/SQS smoke checks. `seed` creates missing resources; `env` supplies string-valued application settings. Reserved `LOCALCLOUD_`/`AWS_` variables cannot be overridden through `ci.env`.

## Connect your application

The test process receives:

| Variable                             | Meaning                                              |
| ------------------------------------ | ---------------------------------------------------- |
| `LOCALCLOUD_URL`, `AWS_ENDPOINT_URL` | Temporary backend origin; do not hardcode port 4566. |
| `LOCALCLOUD_PRINCIPAL`               | Caller configured by `ci.principal`; default test.   |
| `LOCALCLOUD_MANAGEMENT_TOKEN`        | Per-run management bearer token.                     |
| `LOCALCLOUD_CI`                      | 1 for this isolated test run.                        |
| `AWS_DEFAULT_REGION`                 | us-east-1.                                           |
| AWS access-key variables             | Dummy test values.                                   |

Existing real AWS key variables are replaced for the subprocess. Applications still need to honor the endpoint; no general network interception is performed. A compatible API client or the supplied example adapter is needed; broad AWS SDK compatibility is not claimed.

The runner manages the emulator. Your test framework manages your own app process/server and its teardown. Use a finite test command with assertions, not a server command that waits forever.

## Fixtures

Add `fixture = "ci/baseline.json"` inside `[ci]` to import a validated state bundle before setup. Config paths and the application's working directory are resolved relative to the config file's directory.

To create a fixture from an intended running development instance:

```bash
localcloud state export ci/baseline.json
```

Policies are applied after fixture import, then missing seed resources are created. Existing queue attributes/data are preserved. State transfer excludes fault rules, traces and named snapshot history. Each CI run has an independent database; the fixture and source instance are not modified.

## Run options and precedence

```bash
# Use the app config automatically.
localcloud ci

# Run from elsewhere using an explicit project path.
localcloud ci --config /path/to/my-app/localcloud.toml

# Override the configured command once.
localcloud ci -- npm run test:unit

# Override individual settings once.
localcloud ci --timeout 180 --artifacts artifacts/debug-run
localcloud ci --export-state
localcloud ci --no-export-state

# Ignore config and check only the backend.
localcloud ci --no-config
```

The config is discovered only in the current directory. CLI flags override config values. Paths written in the config are relative to its directory; explicitly supplied CLI paths are relative to the directory where you invoke the command. A trailing command after `--` replaces `ci.command`.

Without a config or explicit command, only backend smoke checks run. The report labels application tests `not_configured`; that run does not test an application automatically.

## Results

```text
artifacts/localcloud/report.json
artifacts/localcloud/traces.json
artifacts/localcloud/state.json   # only when export_state is enabled
```

The report includes `passed`, `checks`, `command_exit_code`, `application_tests`, `error`, duration, and the config path when one was used. The application command's nonnegative exit code is propagated. Startup failures/timeouts return a failure code. Cleanup and diagnostics run on failure too.

Default artifacts contain outcomes and request metadata. State capture includes object/message contents and policies; treat exported state as test data. Keep generated artifacts out of version control. Use separate artifact directories for results you want to preserve.

## Tokens

Normal local CI generates its own random management token. It does not need your Kubernetes Secret or your saved host CLI login. If necessary, supply a fixed token through shell variable `LOCALCLOUD_CI_TOKEN`; the runner passes it to the app as `LOCALCLOUD_MANAGEMENT_TOKEN`. Do not commit tokens to TOML, command arguments or fixtures.

This is a local management credential. There is no hosted token issuer, subscription or credits system involved.

## Kubernetes tests

The default CI backend has no Kubernetes executor. A Kubernetes workload check needs an actual cluster/instance; see the Node guide's [optional Kubernetes recipe](NODEJS_LOCAL_CI.md#11-optional-run-this-node-process-in-kubernetes) and [Kubernetes pack usage](KUBERNETES.md). Local app tests use S3/SQS and developer tools; they do not provision a cluster automatically.

## Validation

Config initialization/discovery, schema checks, overrides, working directories, policy/resource setup and fixture preservation are covered by Python tests. The runnable Node example has three unit tests and six integration scenarios. The six scenarios were verified against the real Python HTTP handler using a test-only stdio transport in the socket-restricted authoring environment. Normal `localcloud ci` uses real loopback HTTP; that environment-dependent run still needs execution on your machine.
