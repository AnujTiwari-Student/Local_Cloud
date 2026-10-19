# Getting started with LocalCloud

Use the `localcloud` command for the implemented services and developer tools. No curl or AWS CLI installation is required.

## Enable the command

From the project checkout:

```bash
export PATH="$PWD/bin:$PATH"
localcloud --help
```

For a persistent launcher without pip:

```bash
python3 scripts/install-cli.py
export PATH="$HOME/.local/bin:$PATH"
```

## Connect to your existing Kubernetes instance

Keep the Service port-forward running in another terminal:

```bash
kubectl -n localcloud port-forward service/dev 4566:4566 --address=127.0.0.1
```

Then:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud auth login --kubernetes-secret localcloud-access --namespace localcloud
localcloud status
localcloud resources
```

Login reads the Secret through kubectl, verifies access and saves the token privately. It never displays the token. Your current backend does not need an image rebuild or restart to use the host CLI.

Alternatively, start a local backend using `localcloud start --iam soft`. Plain local backends without management authentication do not require login.

## Use S3 and SQS

```bash
localcloud s3 mb s3://assets
printf 'hello from LocalCloud\n' > hello.txt
localcloud s3 cp hello.txt s3://assets/hello.txt
localcloud s3 ls s3://assets
localcloud s3 cp s3://assets/hello.txt downloaded.txt

localcloud sqs create-queue --queue-name jobs
localcloud sqs send-message --queue-name jobs --message-body 'hello'
localcloud sqs receive-message --queue-name jobs
```

Receive hides the delivered message for its visibility timeout. Acknowledge it using the returned receipt:

```bash
localcloud sqs delete-message --queue-name jobs --receipt-handle 'PASTE_RECEIPT_HANDLE'
```

## Use developer tools

```bash
localcloud snapshots save baseline
localcloud snapshots list
localcloud state export baseline.json
localcloud traces list

localcloud jobs run --image localcloud:dev --command python3 -c 'print("Hello from Kubernetes")'
localcloud jobs list

localcloud ci
```

Jobs require the Kubernetes-backed executor; CI starts a separate temporary local backend. Restore/import replace resource state and policies, so use those commands deliberately.

## Complete references

<<<<<<< HEAD
- [CLI commands](CLI.md): every S3/SQS operation, profiles and token login, IAM, snapshots/state, traces, faults, jobs and lifecycle commands.
- [Kubernetes](KUBERNETES.md): initial cluster/operator setup, storage, workload limits and previews.
- [CI/CD](CI_CD.md): fixtures, runner settings, diagnostic artifacts and GitHub workflows.
- [HTTP API](HTTP_API.md): lower-level curl examples for direct integrations.
- [Feature coverage](FEATURES.md): implemented subsets and remaining gaps.

Use `localcloud sqs --help`, `localcloud s3 --help`, or `localcloud jobs run --help` for command-specific options. The client implements only the backend's supported service subset; it does not forward unknown operations to real AWS.
=======
<<<<<<< Updated upstream
Persistence is automatic. Stop Terminal 1 with Ctrl+C and restart the same command from the same directory. Existing resources return from `.localcloud/state.sqlite3`.

Use a different `--data` path for an independent environment. Do not run two processes against one database.

```bash
# Save a named point-in-time copy of resources, messages and IAM policies.
curl "$CLOUD_URL/_local/snapshots" \
  -H 'Content-Type: application/json' -d '{"name":"tutorial-baseline"}'

# List snapshots alongside other instance details.
curl "$CLOUD_URL/_local/resources"

# RESTORE REPLACES ALL CURRENT RESOURCE STATE AND POLICIES.
curl "$CLOUD_URL/_local/snapshots/restore" \
  -H 'Content-Type: application/json' -d '{"name":"tutorial-baseline"}'
```

Names are unique. Choose a new name to save a new version. Snapshot history, fault rules and traces remain on the instance after restoration.

## 5. Export, import and seed another instance

```bash
curl "$CLOUD_URL/_local/state" -o tutorial-state.json

# Replaces current resource state and policies after checksum/schema validation.
curl "$CLOUD_URL/_local/state/import" \
  -H 'Content-Type: application/json' --data-binary @tutorial-state.json

# Start a second, separate instance seeded from that bundle:
python3 -m localcloud --port 4567 \
  --data .localcloud/second.sqlite3 --load-state tutorial-state.json
```

Second dashboard: `http://127.0.0.1:4567/_local`.

Export from a stopped instance without opening a server:

```bash
python3 -m localcloud --data .localcloud/state.sqlite3 --export-state tutorial-state.json
```

Portable state is limited to 6 MiB and excludes named snapshot history, fault rules and traces. SHA-256 detects corruption; it does not authenticate a file's author.

## 6. IAM enforcement

Modes selected at startup:

| Mode            | Behavior                                                   |
| --------------- | ---------------------------------------------------------- |
| `--iam off`     | Service calls proceed without policy checks; default mode. |
| `--iam soft`    | Missing/denied permissions are logged; calls proceed.      |
| `--iam enforce` | Calls require an Allow and no matching Deny.               |

Stop the running backend and restart using `python3 -m localcloud --iam enforce`. Policies persist. Attach a policy:

```bash
curl "$CLOUD_URL/_local/policies" -H 'Content-Type: application/json' \
  -d '{"principal":"alice","policy":{"Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"*"},{"Effect":"Deny","Action":"s3:DeleteBucket","Resource":"*"}]}}'

# Allowed.
curl -i -X PUT "$CLOUD_URL/iam-example" -H 'X-Local-Principal: alice'

# Explicitly denied, even though s3:* is allowed.
curl -i -X DELETE "$CLOUD_URL/iam-example" -H 'X-Local-Principal: alice'

# Default principal is test, which has no policy unless you attached one.
curl -i "$CLOUD_URL/iam-example"
```

For SQS, add `-H 'X-Local-Principal: alice'` to the helper and grant Alice the required `sqs:` actions. Only Action/Resource identity policies are supported. Management APIs remain accessible to configure policies and recovery. Headers select local testing identities; no passwords, access-key authentication or SigV4 verification are implemented.

## 7. Request Inspector and policy proposals

Use the dashboard or:

```bash
curl "$CLOUD_URL/_local/traces"
curl "$CLOUD_URL/_local/traces?principal=alice"

# Successful observed operations.
curl "$CLOUD_URL/_local/policy-stream?principal=alice&source=successful"

# Proposals covering denied operations.
curl "$CLOUD_URL/_local/policy-stream?principal=alice&source=violations"

# Clear the retained history before capturing a fresh workflow.
curl "$CLOUD_URL/_local/traces/clear" \
  -H 'Content-Type: application/json' -d '{}'
```

Policy generation fills the dashboard editor; it does not apply the result. Review, then click Save policy. Generation sees only the last 1,000 engine calls; trace bodies contain metadata, timings, status and IAM decisions rather than object/message payloads.

## 8. Fault injection

For this exercise, restart with `--iam soft`, or grant your caller the required S3 actions.

```bash
curl -X PUT "$CLOUD_URL/fault-example"

# Fail every PutObject after adding 200ms latency.
curl "$CLOUD_URL/_local/faults" -H 'Content-Type: application/json' \
  -d '{"name":"tutorial-outage","service":"s3","operation":"PutObject","probability":1,"latency_ms":200,"error":{"code":"ServiceUnavailable","status":503}}'

# Returns HTTP 503; no object is stored.
curl -i -X PUT "$CLOUD_URL/fault-example/hello" --data-binary 'hello'

# Management endpoints stay available during faults.
curl "$CLOUD_URL/_local/faults"
curl "$CLOUD_URL/_local/faults/delete" -H 'Content-Type: application/json' \
  -d '{"name":"tutorial-outage"}'

# Now succeeds.
curl -i -X PUT "$CLOUD_URL/fault-example/hello" --data-binary 'hello'
```

For latency only, omit `error`. Probability 0.5 triggers approximately half the eligible calls; latency is capped at 5,000 ms. Rules persist across restart. The first triggered rule in name order wins. Only S3/SQS and their operation names are supported; this is not AWS FIS or network-level fault injection.

## 9. Troubleshooting and application usage

- Connection refused: start the backend, keep Terminal 1 open and check the configured port.
- Dashboard resource counts are empty: create resources through the API; a listed service is not seeded automatically.
- AccessDenied: check `--iam` mode, policy and `X-Local-Principal` header.
- ServiceUnavailable/ThrottlingException: list and remove configured fault rules.
- ReceiptHandleIsInvalid: receive again and use the newest unexpired receipt.
- Snapshot conflict: use a new snapshot name.
- Operation not supported: consult the S3/SQS coverage above; changing a setting cannot enable an unimplemented operation.

Your application can call these same HTTP endpoints. AWS CLI, awslocal and SDK drop-in compatibility have not been verified, so the curl examples are the supported usage path. This is a local development emulator; the management interface should stay on loopback. It can require a bearer token using `LOCALCLOUD_MANAGEMENT_TOKEN` or `--management-token-env`; see the Kubernetes guide.

Run the verification suite:

```bash
python3 -m unittest discover -s tests -v
```


## 10. Kubernetes pack

See [Kubernetes usage](KUBERNETES.md) for the complete image build, operator installation, Secret setup, instance CR, persistent storage, pod execution, log/status/cancel operations and TTL previews. Kubernetes Jobs can also be managed from the dashboard once the executor is enabled.

## 11. CI/CD

See [CI/CD usage](CI_CD.md) for the isolated runner, application test commands, fixture imports, per-run management tokens, diagnostics, GitHub Actions and preview cleanup. Quick start:

```bash
python3 -m localcloud.ci
```

This starts its own temporary backend on a free port. No manually running instance or LocalStack token is needed.
=======
- [CLI commands](CLI.md): every S3/SQS operation, profiles and token login, IAM, snapshots/state, traces, faults, jobs and lifecycle commands.
- [Kubernetes](KUBERNETES.md): initial cluster/operator setup, storage, workload limits and previews.
- [CI/CD](CI_CD.md): project-root localcloud.toml, application tests, resource setup, fixtures and diagnostics.
- [Node.js app example](NODEJS_LOCAL_CI.md): local setup from start to finish, with code, tests and every supported service/tool.
- [HTTP API](HTTP_API.md): lower-level curl examples for direct integrations.
- [Feature coverage](FEATURES.md): implemented subsets and remaining gaps.

Use `localcloud sqs --help`, `localcloud s3 --help`, or `localcloud jobs run --help` for command-specific options. The client implements only the backend's supported service subset; it does not forward unknown operations to real AWS.

## Port already in use

Port 4566 may already belong to a LocalCloud server or your Kubernetes Service port-forward. To inspect the listener on Linux, run `ss -ltnp 'sport = :4566'`. Stop the relevant process or port-forward in its original terminal if you intend to replace it. Restarting the source server loads the latest source code; an existing Kubernetes image must be rebuilt and redeployed to include changes.

To run a separate local instance alongside that listener, use a different port and database:

```bash
localcloud start --port 4567 --data .localcloud/state-4567.sqlite3
```

In another terminal:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4567
localcloud status
```

Open `http://127.0.0.1:4567/_local`. This instance has separate resources from the existing server. LocalCloud reports an occupied port without stopping or replacing the listener.
>>>>>>> Stashed changes
>>>>>>> 28aa52d (Feature: Add ec2 service)
