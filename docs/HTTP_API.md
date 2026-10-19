# HTTP API reference and original curl examples

LocalCloud runs independently on your machine. No LocalStack account, auth token, Docker, pip packages or real AWS credentials are needed. Use Python 3.11+ and curl. Run all commands below from the project directory unless indicated otherwise.

## 1. Start the backend

Terminal 1:

```bash
cd '/run/media/anujtiwari/DevMount/LocalStack Pro'
python3 -m localcloud --iam soft
```

Keep this terminal running. Open `http://127.0.0.1:4566/_local` for the dashboard. Terminal 2:

```bash
export CLOUD_URL=http://127.0.0.1:4566
curl "$CLOUD_URL/_local/health"
curl "$CLOUD_URL/_local/resources"
```

Expected health response: `{"status":"running","services":["sqs","s3"]}`.

The dashboard shows resources, counts, snapshots, IAM policies, traces and fault controls. Create and manipulate queues/objects through the APIs below; resource editors are not implemented in the dashboard.

If port 4566 is occupied, start with `--port 4567`, set `CLOUD_URL=http://127.0.0.1:4567`, and open that dashboard port instead. The same backend serves both APIs and dashboard.

## 2. S3: every implemented operation

Run in Terminal 2:

```bash
# Create a bucket.
curl -i -X PUT "$CLOUD_URL/tutorial-assets"

# List buckets (XML).
curl "$CLOUD_URL/"

# Upload a text object. Repeating PUT replaces it.
curl -i -X PUT "$CLOUD_URL/tutorial-assets/hello.txt" --data-binary 'hello world'

# Read the object.
curl "$CLOUD_URL/tutorial-assets/hello.txt"

# List objects (XML).
curl "$CLOUD_URL/tutorial-assets?list-type=2"

# Upload a real local file, including binary files.
# Replace ./photo.png with an existing file before running:
# curl -i -X PUT "$CLOUD_URL/tutorial-assets/photo.png" --data-binary @./photo.png
# curl "$CLOUD_URL/tutorial-assets/photo.png" -o ./downloaded.png

# Delete an object.
curl -i -X DELETE "$CLOUD_URL/tutorial-assets/hello.txt"

# Delete the bucket after deleting all its objects.
curl -i -X DELETE "$CLOUD_URL/tutorial-assets"
```

Expected errors: unknown bucket/key returns 404; deleting a nonempty bucket returns 409. Empty DELETE success is normal. This is path-style S3 only; no multipart, versioning or metadata support. Requests are limited to 8 MiB.

## 3. SQS: every implemented operation

Define this Bash helper once in Terminal 2:

```bash
sqs() {
  curl -sS "$CLOUD_URL/" \
    -H "X-Amz-Target: AmazonSQS.$1" \
    -H 'Content-Type: application/x-amz-json-1.0' \
    -d "$2"
}
```

Create, find and inspect a queue:

```bash
sqs CreateQueue '{"QueueName":"tutorial-jobs","Attributes":{"VisibilityTimeout":"30"}}'
sqs ListQueues '{}'
sqs ListQueues '{"QueueNamePrefix":"tutorial"}'
sqs GetQueueUrl '{"QueueName":"tutorial-jobs"}'

export QUEUE_URL="$CLOUD_URL/000000000000/tutorial-jobs"
sqs GetQueueAttributes "{\"QueueUrl\":\"$QUEUE_URL\",\"AttributeNames\":[\"All\"]}"
```

Send, receive and acknowledge a message:

```bash
sqs SendMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"MessageBody\":\"hello from producer\"}"

RESPONSE=$(sqs ReceiveMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"MaxNumberOfMessages\":1,\"VisibilityTimeout\":300}")
printf '%s\n' "$RESPONSE"
RECEIPT=$(printf '%s' "$RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Messages"][0]["ReceiptHandle"])')

# This returns no messages while the delivered message is invisible.
sqs ReceiveMessage "{\"QueueUrl\":\"$QUEUE_URL\"}"

# Acknowledge using the current receipt before its visibility timeout expires.
sqs DeleteMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"ReceiptHandle\":\"$RECEIPT\"}"
```

Test visibility changes and redelivery:

```bash
sqs SendMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"MessageBody\":\"retry this\"}"
RESPONSE=$(sqs ReceiveMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"VisibilityTimeout\":300}")
RECEIPT=$(printf '%s' "$RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Messages"][0]["ReceiptHandle"])')

# Make the delivered message available immediately.
sqs ChangeMessageVisibility "{\"QueueUrl\":\"$QUEUE_URL\",\"ReceiptHandle\":\"$RECEIPT\",\"VisibilityTimeout\":0}"

# Redelivery returns a NEW receipt. The old receipt can no longer delete it.
sqs ReceiveMessage "{\"QueueUrl\":\"$QUEUE_URL\",\"VisibilityTimeout\":300}"

# Remove all messages from this queue, then delete it.
sqs PurgeQueue "{\"QueueUrl\":\"$QUEUE_URL\"}"
sqs DeleteQueue "{\"QueueUrl\":\"$QUEUE_URL\"}"
```

If a receipt expires while following the commands, receive the message again and capture its new receipt. Delayed sends accept `DelaySeconds` from 0 to 900. `MaxNumberOfMessages` is 1–10; visibility is 0–43,200 seconds. Long polling, FIFO, batch operations and message attributes are unsupported.

## 4. Persistence and snapshots

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
