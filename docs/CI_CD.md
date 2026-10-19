# CI/CD: isolated runs, fixtures, diagnostics and Kubernetes previews

Prefer commands over curl? See the [CLI guide](CLI.md), including automatic token login from a Kubernetes Secret.

LocalCloud now has a CI runner, GitHub Actions workflows and a Kubernetes integration smoke test. There is no hosted billing/credit service or externally issued CI license token; these tools run in your own CI infrastructure.

## 1. Run live HTTP smoke checks locally

From the project directory:

```bash
python3 -m localcloud.ci
```

No manually started backend is required. Each invocation:

1. Creates a temporary SQLite database and a backend on a free loopback port.
2. Protects management routes with a per-run random bearer token.
3. Checks health, binary S3 upload/download, SQS delivery, visibility and acknowledgement over real HTTP.
4. Removes its smoke resources.
5. Saves `report.json` and `traces.json` in `artifacts/localcloud`.
6. Stops the backend and removes temporary data, including when checks fail.

Success exits 0; smoke/startup failures exit 1. The runner never connects to the manually running instance on port 4566.

This command requires permission to open local listening sockets. In a sandbox that denies sockets it correctly fails and writes a failure report; that is not a passing integration test.

## 2. Run application tests against the temporary backend

```bash
python3 -m localcloud.ci --artifacts artifacts/my-run --timeout 300 -- \
  python3 -m unittest discover -s tests -v
```

The example runs this repository's tests after the live smoke checks. Replace the command after `--` with your application's test command. Arguments are passed directly, with no implicit shell. Its exit code is propagated; timeouts and startup failures fail the run.

The child process receives:

| Environment variable | Value |
|---|---|
| `LOCALCLOUD_URL` | The temporary backend URL; use this in HTTP clients. |
| `AWS_ENDPOINT_URL` | Same URL; SDK compatibility still depends on supported protocols. |
| `LOCALCLOUD_MANAGEMENT_TOKEN` | Bearer token for management routes only. |
| `AWS_DEFAULT_REGION` | `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Dummy `test` values. |

Existing real AWS credentials in those variables are replaced for this subprocess. Applications must honor the supplied endpoint; LocalCloud does not intercept unrelated network requests.

Use `LOCALCLOUD_URL` in your tests instead of assuming port 4566. For example, a Python test can use `urllib.request.urlopen(os.environ['LOCALCLOUD_URL'] + '/_local/health')`. For protected management calls add `Authorization: Bearer <LOCALCLOUD_MANAGEMENT_TOKEN>`.

## 3. Seed fixtures and enable policy testing

Export a fixture from an existing instance:

```bash
curl http://127.0.0.1:4566/_local/state -o baseline.json
```

If that instance uses a management token, include its Authorization header.

Then:

```bash
python3 -m localcloud.ci --fixture baseline.json --iam soft -- \
  python3 -m unittest discover -s tests -v
```

The fixture is fully validated and imported before the backend starts. Each run has its own database; changes do not affect the source instance or fixture file. Named snapshot history, trace history and fault rules are not imported.

`--iam` supports off, soft (default), and enforce. The runner installs a testing policy for its own `ci-smoke` principal. Enforced application tests must supply policies for their own local principals in the fixture or through management APIs; the default `test` principal does not automatically inherit the smoke policy.

## 4. CI tokens and artifacts

By default a new random management token is generated for each run. If your own pipeline needs a fixed secret, set `LOCALCLOUD_CI_TOKEN` through your CI secret store. The runner uses it as its management token and passes it to the test subprocess as `LOCALCLOUD_MANAGEMENT_TOKEN`. Do not put it into command arguments, committed fixtures or logs.

This token is a LocalCloud management credential, not a LocalStack subscription token. It grants management access to this temporary backend; it does not introduce multiple users, role-scoped tokens or a token-issuance service.

Inspect diagnostics:

```bash
cat artifacts/localcloud/report.json
cat artifacts/localcloud/traces.json
```

By default artifacts contain outcomes and request metadata, not resource contents or tokens. For explicit resource-state capture:

```bash
python3 -m localcloud.ci --export-state --artifacts artifacts/debug-run
```

This adds `state.json`, which can contain object/message contents and policies. Handle it as test data and choose artifact retention/access appropriately. Use separate artifact directories for runs you want to preserve.

To launch an environment from that bundle later:

```bash
python3 -m localcloud --load-state artifacts/debug-run/state.json
```

## 5. GitHub Actions

`.github/workflows/ci.yml` is ready to run when this project is committed and pushed to your GitHub repository with Actions enabled. No GitHub workflow has been executed or repository published by the authoring agent.

It runs on pushes, pull requests and manual dispatch, with read-only repository permissions:

| Job | Checks |
|---|---|
| `python` | Python 3.11/3.13 unit tests plus isolated live backend smoke checks. |
| `container` | Builds the Dockerfile, starts the image, verifies HTTP readiness and S3/SQS behavior, then removes the container. |
| `kubernetes` | Creates a disposable Kind cluster, loads the image, installs the operator and verifies an instance and a real workload Job. |

Python and Kubernetes diagnostics are uploaded even when checks fail, with seven-day retention. These jobs use temporary test infrastructure and need no LocalStack account or cloud secrets. The Kubernetes job uses the Helm project's Kind action to provision its disposable cluster.

To use LocalCloud in another repository, copy the project or make its source available in a prior checkout step, then run:

```yaml
- name: Test against an isolated LocalCloud instance
  run: python3 -m localcloud.ci --artifacts artifacts/localcloud -- python3 -m unittest discover -s tests -v
```

Substitute your own test command and artifact-upload step. Runtime dependencies such as pytest belong to your application setup, not this emulator.

## 6. Persistent cluster previews for CI branches

See [Kubernetes setup](KUBERNETES.md) first. In a trusted CI job that already has access to your development cluster, use an existing namespace/operator, a cluster-accessible image tag and a pre-created management Secret.

For a CLI running outside the cluster, start a loopback-only `kubectl proxy`. Then:

```bash
python3 -m localcloud.operator --api-server http://127.0.0.1:8001 \
  --namespace localcloud preview create --name pr-42 \
  --image localcloud:dev --secret localcloud-access --ttl 3600
```

Wait for readiness with kubectl, run tests from inside the cluster or via a loopback Service port-forward, and delete the preview in the CI system's always/finally cleanup:

```bash
python3 -m localcloud.operator --api-server http://127.0.0.1:8001 \
  --namespace localcloud preview delete --name pr-42
```

Use a unique DNS-label name per concurrent build, at most 40 characters. An existing name is rejected; create does not overwrite it. The operator also removes expired previews if explicit pipeline cleanup is missed. It must remain running for TTL cleanup.

This provides isolated private cluster previews. It does not publish internet endpoints, post PR comments, mint cluster credentials, manage cloud subscriptions or provide a hosted quota/credits platform. Configure trusted CI identity and network access in your own platform. Do not expose cluster credentials to untrusted fork workflows; the included pull-request workflow avoids this by using a disposable local Kind cluster.

## Validation boundary

The unit/contract suite checks runner failure cleanup, exit codes, child environment, diagnostics, token gates, Kubernetes resource generation, reconciliation and Job ownership. The HTTP smoke routine is also exercised against the real handler using an in-memory connection. A live runner attempt in the authoring environment failed with socket permissions; Docker access was also denied. Actual container, Kind and cloud-hosted CI runs remain unverified until executed in a capable environment.
