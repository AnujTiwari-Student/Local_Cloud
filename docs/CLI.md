# LocalCloud commands: no curl required

The CLI uses familiar AWS-style service names and flags for the implemented S3/SQS subset. Developer tools have their own command groups. Unsupported AWS services/options remain unsupported; this is not the full AWS CLI.

## 1. Enable the command

From your checkout:

```bash
export PATH="$PWD/bin:$PATH"
localcloud --help
```

This works immediately without pip. To install a persistent launcher for this checkout:

```bash
python3 scripts/install-cli.py
export PATH="$HOME/.local/bin:$PATH"
localcloud --help
```

The installer creates `~/.local/bin/localcloud`. Keep the source checkout in place: the launcher imports its current code, so edits are immediately available. It does not edit your shell configuration; if the directory is not already in PATH, add it to your shell configuration yourself or export it in each new shell. The installer refuses to overwrite an unrelated command. Use `--bin-dir DIRECTORY` for a different destination.

Alternatively, in a Python environment with pip and build tooling:

```bash
python3 -m pip install -e .
```

Or run `python3 -m localcloud.cli` in place of `localcloud`, with no installation.

## 2. Connect to your existing Kubernetes instance

Keep the existing port-forward running in another terminal:

```bash
kubectl -n localcloud port-forward service/dev 4566:4566 --address=127.0.0.1
```

Then configure the CLI and load its token directly from the Secret:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud auth login --kubernetes-secret localcloud-access --namespace localcloud
localcloud auth status
localcloud status
localcloud resources
```

The login command invokes kubectl using your current context, reads Secret key `token`, verifies management access, and only then saves it. It does not display the token. Add `--context kind-localcloud` to select that kubectl context explicitly.

**No image rebuild or pod restart is needed to use the host CLI with the existing backend.** All client commands use the existing APIs. Newly built images also include the CLI launcher for workloads that need it.

For a plain local backend instead:

```bash
localcloud start --iam soft
```

When no management token is configured on that backend, login is unnecessary. `LOCALCLOUD_MANAGEMENT_TOKEN` is used automatically when set. To log in manually, use `localcloud auth login` and paste the token into its hidden prompt, or pipe a token into `localcloud auth login --token-stdin`.

Saved profiles default to `~/.config/localcloud/config.json` (or `$XDG_CONFIG_HOME/localcloud/config.json`), written atomically with owner-only file permissions. Tokens are plaintext credentials in that file, so do not commit or share it. `LOCALCLOUD_CONFIG` or `--config-file` selects another path.

## 3. S3 commands

```bash
# Create and list buckets.
localcloud s3 mb s3://assets
localcloud s3 ls

# Upload and inspect a file.
printf 'hello from LocalCloud\n' > hello.txt
localcloud s3 cp hello.txt s3://assets/hello.txt
localcloud s3 ls s3://assets

# Download or copy an object.
localcloud s3 cp s3://assets/hello.txt downloaded.txt
localcloud s3 cp s3://assets/hello.txt s3://assets/backup.txt

# Prefix filtering.
localcloud s3 ls s3://assets/back

# Delete objects, then the empty bucket.
localcloud s3 rm s3://assets/hello.txt
localcloud s3 rm s3://assets/backup.txt
localcloud s3 rb s3://assets
```

Uploads and downloads preserve binary bytes. A bucket-only/trailing-slash upload destination appends the source filename. A local directory download destination appends the object's basename. S3-to-S3 copying performs a download followed by an upload. Files are limited to 8 MiB. Listings are flat with optional prefix filtering. Recursive copies, sync, multipart uploads and force-removal of nonempty buckets are not implemented; unsupported flags return an error.

API-style equivalents:

```bash
localcloud s3api create-bucket --bucket assets
localcloud s3api list-buckets
localcloud s3api put-object --bucket assets --key hello.txt --body hello.txt
localcloud s3api list-objects-v2 --bucket assets --prefix hello
localcloud s3api get-object --bucket assets --key hello.txt downloaded.txt
localcloud s3api delete-object --bucket assets --key hello.txt
localcloud s3api delete-bucket --bucket assets
```

The simple upload command reports a locally computed MD5 ETag for the uploaded body. API commands output JSON. High-level `s3` commands print readable status/listings; add `--output json` for structured output.

## 4. SQS commands

```bash
localcloud sqs create-queue --queue-name jobs --attributes VisibilityTimeout=300
localcloud sqs list-queues
localcloud sqs list-queues --queue-name-prefix job
localcloud sqs get-queue-url --queue-name jobs
localcloud sqs get-queue-attributes --queue-name jobs --attribute-names All

localcloud sqs send-message --queue-name jobs --message-body 'hello from producer'
localcloud sqs receive-message --queue-name jobs --max-number-of-messages 1
```

Receive returns a `ReceiptHandle`. Copy its value into the acknowledgement command:

```bash
localcloud sqs delete-message --queue-name jobs --receipt-handle 'PASTE_RECEIPT_HANDLE'
```

For scripting without manually copying a receipt:

```bash
RECEIPT=$(localcloud sqs receive-message --queue-name jobs \
  | python3 -c 'import json,sys; m=json.load(sys.stdin).get("Messages",[]); print(m[0]["ReceiptHandle"] if m else "")')
if [ -n "$RECEIPT" ]; then
  localcloud sqs delete-message --queue-name jobs --receipt-handle "$RECEIPT"
fi
```

If you already received the only message in the earlier command, it remains invisible until its timeout expires; a second receive may return an empty list. Use its original receipt before expiry, or wait for redelivery and use the new receipt.

Other operations:

```bash
# Make a currently delivered message immediately available again.
localcloud sqs change-message-visibility --queue-name jobs \
  --receipt-handle 'PASTE_CURRENT_RECEIPT_HANDLE' --visibility-timeout 0

# Delayed delivery and file input.
localcloud sqs send-message --queue-name jobs --message-body 'deliver later' --delay-seconds 5
localcloud sqs send-message --queue-name jobs --message-body file://hello.txt

# Purge removes all messages; delete removes the queue.
localcloud sqs purge-queue --queue-name jobs
localcloud sqs delete-queue --queue-name jobs
```

AWS-style `--queue-url` is also accepted anywhere the convenience `--queue-name` selector is supported:

```bash
localcloud sqs send-message \
  --queue-url http://dev.localcloud.svc:4566/000000000000/jobs \
  --message-body hello
```

The CLI sends requests to its configured/forwarded endpoint, using QueueUrl only as a resource identifier. You do not need to resolve in-cluster DNS from the host. `--queue-name` constructs that identifier directly, without an extra GetQueueUrl permission check.

Attributes accept shorthand (`VisibilityTimeout=30`), inline JSON or `file://attributes.json`. Only backend-supported attributes are accepted. No FIFO, batching, DLQs, retention scheduler or long polling is added by this CLI.

## 5. Snapshots, state and traces

```bash
localcloud snapshots save baseline
localcloud snapshots list
localcloud snapshots restore baseline

localcloud state export baseline.json
localcloud state import baseline.json

localcloud traces list
localcloud traces list --user-name alice
localcloud traces clear
```

Restore/import **replace current resource state and policies**. Snapshot names are unique. State exports use owner-only file permissions. Existing limits and exclusions for resource state, trace history, fault rules and snapshot history still apply.

## 6. IAM policy tools

Create `policy.json`:

```json
{ "Statement": [{ "Effect": "Allow", "Action": "s3:*", "Resource": "*" }] }
```

Then:

```bash
localcloud iam put-policy --user-name alice --policy-document file://policy.json
localcloud iam list-principals
localcloud --principal alice s3 mb s3://alice-assets
localcloud iam generate-policy --user-name alice --source successful
localcloud iam generate-policy --user-name alice --source violations
```

`--principal` selects the caller; `--user-name` selects the principal whose policy/proposal is managed. `put-policy` replaces the local principal's whole policy. These are local testing tools, not the full AWS IAM CLI or named inline-policy system. Proposals are not automatically applied. Choose enforcement mode when starting the backend or setting the instance spec.

## 7. Fault injection

```bash
localcloud faults add --name s3-outage --service s3 --operation PutObject \
  --probability 1 --latency-ms 200 --error-code ServiceUnavailable --status-code 503
localcloud faults list
localcloud faults remove s3-outage

# Latency only; quote * so your shell does not expand it.
localcloud faults add --name delay --service sqs --operation '*' --latency-ms 100
localcloud faults remove delay
```

Management commands continue to work during injected service faults.

## 8. Kubernetes workload jobs

Against your connected Kubernetes-backed instance:

```bash
localcloud jobs run --image localcloud:dev \
  --timeout-seconds 120 --ttl-seconds 600 \
  --command python3 -c 'print("Hello from a Kubernetes pod")'

localcloud jobs list
localcloud jobs status lc-run-REPLACE_WITH_RETURNED_NAME
localcloud jobs logs lc-run-REPLACE_WITH_RETURNED_NAME
localcloud jobs delete lc-run-REPLACE_WITH_RETURNED_NAME
```

`--command` must come last: everything after it belongs to the workload command, including flags such as `-c`. Add environment variables before it using repeated `--env KEY=VALUE` flags. The backend's image allowlist, ownership checks and limits still apply. Logs print as text; add `--output json` before `--command` for a job submission, or to a log command for structured output.

Existing operator and preview commands are available under `localcloud kube`:

```bash
localcloud kube --namespace localcloud manifests --image localcloud:dev
localcloud kube --namespace localcloud example --name dev --image localcloud:dev

# Requires the loopback kubectl proxy described in KUBERNETES.md.
localcloud kube --api-server http://127.0.0.1:8001 --namespace localcloud \
  preview create --name pr-42 --image localcloud:dev --ttl 3600
localcloud kube --api-server http://127.0.0.1:8001 --namespace localcloud preview list
localcloud kube --api-server http://127.0.0.1:8001 --namespace localcloud preview delete --name pr-42
```

Initial cluster creation, CRD/RBAC installation and port-forwarding still use Kind/kubectl. The client replaces curl, not those cluster administration tools.

## 9. CI and backend lifecycle

```bash
localcloud start --iam soft
localcloud start --port 4567 --data .localcloud/second.sqlite3

localcloud ci init --command npm test  # run this in your app directory
localcloud ci
localcloud ci run --artifacts artifacts/localcloud -- python3 -m unittest discover -s tests -v
```

`start`, `ci` and `kube` delegate to their original parsers. Put their own options after the command; HTTP profile/global flags are for the client groups, not the delegated lifecycle commands. Existing `python3 -m localcloud`, `.ci` and `.operator` entry points remain compatible. `localcloud ci` automatically reads `localcloud.toml` in the current application directory; see [the complete app integration guide](NODEJS_LOCAL_CI.md).

## 10. Profiles, overrides and help

```bash
localcloud configure --profile dev --endpoint-url http://127.0.0.1:4566
localcloud configure --profile preview --endpoint-url http://127.0.0.1:4567
localcloud auth login --profile preview --kubernetes-secret localcloud-access --namespace localcloud
localcloud --profile preview resources
localcloud configure --profile preview --list
localcloud auth logout --profile preview

localcloud sqs --help
localcloud sqs send-message --help
localcloud jobs run --help
```

For HTTP client commands, global flags can appear before the service, after the service or after its operation (except after the job's `--command`). Endpoint precedence is explicit `--endpoint-url`, `LOCALCLOUD_URL`, `LOCALCLOUD_ENDPOINT_URL`, saved profile, then `http://127.0.0.1:4566`. Caller precedence is `--principal`, `LOCALCLOUD_PRINCIPAL`, profile, then `test`. Profile defaults to `LOCALCLOUD_PROFILE` or `default`.

`LOCALCLOUD_MANAGEMENT_TOKEN` overrides a profile token. If it is stale, unset it before using the saved login. Logout removes the saved credential; it cannot unset a variable in your parent shell. `configure --list` and auth commands never display the token.

`--output json` supports scripts; `--output text` gives scalar/tab-separated output, for example `localcloud sqs get-queue-url --queue-name jobs --output text`. Full AWS `--query`/JMESPath filtering is not implemented. Region is fixed to `us-east-1`; other `--region` values are rejected.

Failures produce a concise stderr message and nonzero exit code. Authentication errors suggest login; connection errors suggest checking the backend/port-forward. Unknown services/options are rejected rather than forwarded to real AWS.

## EC2 lifecycle

See [EC2 setup and complete usage](EC2.md) for mock and Docker modes.

```bash
localcloud ec2 run-instances --image-id ami-local --instance-type t3.micro
localcloud ec2 describe-instances
# Replace i-REPLACE with the returned InstanceId:
localcloud ec2 stop-instances --instance-ids i-REPLACE
localcloud ec2 start-instances --instance-ids i-REPLACE
localcloud ec2 terminate-instances --instance-ids i-REPLACE
```

Docker-backed EC2 web apps can publish localhost TCP ports:

```bash
localcloud ec2 run-instances --image-id ami-web --publish 8088:8080 --timeout 60
```

Requires an image mapping on the backend. See [complete web app deployment](EC2.md#deploy-a-web-application).

### EC2 names and Docker identifiers

```bash
localcloud ec2 run-instances --image-id ami-web --name my-web --publish 8088:8080
localcloud ec2 describe-instances
localcloud ec2 stop-instances --name my-web
localcloud ec2 start-instances --host-port 8088
localcloud ec2 stop-instances --container-name localcloud-i-REPLACE
localcloud ec2 start-instances --container-id REPLACE_WITH_DOCKER_ID
localcloud ec2 name-instance --host-port 8088 --new-name renamed-web
localcloud ec2 terminate-instances --name renamed-web
```

Choose one selector per command. Full EC2 IDs remain supported. Docker ID prefixes require at least 12 hex characters and must match exactly one managed instance. See [recovery and naming rules](EC2.md#names-and-other-instance-identifiers).

## Verify an instance connection

```bash
cat ~/.config/localcloud/server-token | localcloud connect --endpoint-url http://127.0.0.1:4566 --token-stdin
localcloud resources
```

The server must already be running. Connection is verified before saving the profile; use `--profile dev` for a named profile and on subsequent commands. See [the full CLI/dashboard connection workflow](DASHBOARD.md).

Supported resource settings can be modified through either interface:

```bash
localcloud ec2 modify-instance-attribute --name my-web --instance-type t3.small
localcloud sqs set-queue-attributes --queue-name app-jobs --attributes VisibilityTimeout=60
```

EC2 type changes require a stopped instance; only the three documented local presets are supported. SQS settings currently support only `VisibilityTimeout`.

## Engine source reference and routing

EC2 now also supports `describe-images`, `create-tags`, `delete-tags`,
`describe-tags`, `describe-instance-attribute`, JSON `--filters`, and launch
`--client-token`, `--tags` and `--user-data`. See the complete
[EC2 examples and SDK setup](EC2_REFERENCE.md).

```bash
localcloud engine status
```

Requires the running server's management access. Shows the pinned upstream architecture reference, provider adapter states and handler stages. It explicitly reports that exact LocalStack parity has not been established. See [source evidence and migration boundaries](ENGINE_RE.md).

## Expanded EC2 commands

The [EC2 infrastructure walkthrough](EC2_INFRASTRUCTURE.md) covers VPC/subnet/SG,
route/gateway, volume/snapshot, key-pair, metadata, reboot/status and console
commands. Run `localcloud ec2 --help` to list commands and each command with
`--help` for supported flags. Structured infrastructure arguments accept JSON
or `file://PATH`; string lists accept space-separated values.

For ENI attributes, private-IP assignment, interface attachment and elastic-IP
association commands, see [the networking walkthrough](EC2_NETWORKING.md).
These manage emulated records; published ports provide actual app access.

The [latest EC2 checkpoint](EC2_RELEASE.md) adds image lifecycle, snapshot copying
and launch-template/version commands. `run-instances --launch-template JSON`
can supply image/type defaults; explicit flags override them.
