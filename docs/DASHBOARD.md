# Connect to an instance and manage resources

A **LocalCloud instance** is a running emulator server, with its own endpoint and database. An **EC2 instance** is a resource inside that server. Start or reach the LocalCloud server first, connect to it, then create EC2 instances, S3 buckets and SQS queues through the CLI or dashboard. Both interfaces operate on the same resources when they use the same endpoint.

A saved CLI profile does not start a server. The server can run in your terminal, Docker or Kubernetes; it must remain reachable while you manage resources. The dashboard cannot start a stopped backend. If the server stops while the dashboard is open, requests fail and the dashboard disables resource controls until you reconnect. Stopping the server does not automatically stop Docker workloads.

## 1. Start a named LocalCloud server

From the repository root, for mock EC2, S3 and SQS:

```bash
localcloud start --instance-name dev --port 4566 --data .localcloud/dev.sqlite3
```

For Docker-backed EC2 web applications, build the sample image if needed:

```bash
docker build -t localcloud-ec2-web:dev examples/ec2-web
```

Use your existing saved server token. If you have never created one, generate it **once**:

```bash
mkdir -p ~/.config/localcloud
if [ ! -s ~/.config/localcloud/server-token ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/localcloud/server-token)
fi
```

Start the Docker-enabled server instead of the mock command above:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN="$(cat ~/.config/localcloud/server-token)"
localcloud start --instance-name dev --port 4566 \
  --data .localcloud/ec2-docker.sqlite3 \
  --ec2-image ami-web=localcloud-ec2-web:dev
```

Keep this terminal running. If you already have instances, keep their original database and image mappings. If port 4566 is occupied, stop its existing listener or choose another port and use that same port in the dashboard and CLI. See [EC2 runtime limits](EC2.md).

## 2. Connect from the dashboard

Open **http://127.0.0.1:4566/_local** and hard-refresh after upgrading the project.

1. Leave **Instance endpoint** set to the server's origin.
2. Enter the running server's management token, if configured. Its value is the contents of `~/.config/localcloud/server-token`, not the filename or variable name. You can display it privately with `cat ~/.config/localcloud/server-token`.
3. Leave **IAM test principal** as `test`, or choose a principal with the policies you want to test.
4. Click **Connect**. The sidebar displays the server's instance name and endpoint; resource controls become available only after the authenticated connection succeeds.

The dashboard holds the token in page memory; it does not save it in browser storage or URLs. Reloading requires connecting again. Disconnect clears the token and displayed resource details. Entering a different origin opens that server's dashboard; authenticate there separately. No cross-origin proxy or automatic credential forwarding is provided.

The **Overview** shows the connection endpoint, advertised endpoint, supported services, EC2 runtime, IAM mode and resource counts. The LocalCloud server name `dev` is separate from any EC2 resource name such as `my-web`.

## 3. Manage resources

| Page              | Available controls                                                                                                                                                                                                       |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| EC2               | Launch with name, image ID, instance type and optional localhost TCP ports. Search, open details, inspect full/container IDs and endpoints, rename, stop, start and terminate. Open a running app's configured endpoint. |
| EC2 details       | After stopping the instance, change its type among `t3.micro`, `t3.small` and `t3.medium`. Docker mode updates the container's local memory/swap limits; these are local resource presets, not AWS capacity.             |
| S3                | Create/list buckets, inspect objects, upload a file with an object key, download binary data, delete objects and delete empty buckets. Maximum dashboard upload is 5 MiB.                                                |
| SQS               | Create/list queues, inspect URL/ARN/attributes, modify the visibility timeout, send messages with optional delay, receive up to ten messages, acknowledge/delete received messages, purge and delete queues.             |
| Snapshots & state | Save/restore mock state, export/import validated JSON bundles. Docker-mode snapshot/import restrictions are shown in the UI; exports containing Docker records also fail explicitly.                                     |
| IAM policies      | Save supported identity policies and generate proposals from successful calls or violations. The connected test principal is sent with service operations.                                                               |
| Request Inspector | Filter retained API metadata and inspect timing, status and IAM evaluations.                                                                                                                                             |
| Fault injection   | Add/remove supported service/operation error and latency rules, including EC2.                                                                                                                                           |
| Kubernetes jobs   | Submit allowed-image workloads and inspect logs/delete jobs when the server has a Kubernetes executor enabled.                                                                                                           |

EC2 example: choose `ami-web`, name `my-web`, and ports `8088:8080`. Click **Launch instance**, then open its details and click the application endpoint. The app runs on the Docker host; a remote host may need an explicit tunnel. Changing the instance type requires stopping it first. Port/image changes, security groups, volumes and arbitrary AWS attributes are not implemented.

S3 example: create `app-assets`, choose a file, set an object key such as `documents/hello.txt`, upload and download it. Delete the objects before deleting the bucket.

SQS example: create `app-jobs` with a 60-second visibility timeout, send a message, click **Receive up to 10**, then **Acknowledge / delete** before the receipt expires. Receiving affects queue visibility; it is not a passive peek. Changing the queue timeout affects subsequent receives and does not extend existing receipt handles.

Resource mutations use the backend's existing persistence, IAM checks, traces and faults. Operational errors appear in the status area; the dashboard does not silently emulate success. Destructive actions ask for confirmation. This UI exposes the implemented service subsets, not the entire AWS console.

## 4. Connect from the CLI

For a token-protected server, in a second terminal:

```bash
unset LOCALCLOUD_MANAGEMENT_TOKEN
cat ~/.config/localcloud/server-token | localcloud connect \
  --endpoint-url http://127.0.0.1:4566 --token-stdin
localcloud resources
localcloud ec2 describe-instances
```

`connect` checks health and authenticated resource access before saving the profile. A failed connection does not replace a working profile. Without a server token:

```bash
localcloud connect --endpoint-url http://127.0.0.1:4566
```

Named profiles can represent separate servers:

```bash
localcloud connect --profile dev --endpoint-url http://127.0.0.1:4566
localcloud resources --profile dev
localcloud sqs list-queues --profile dev
```

Use the same profile flag on subsequent commands, or export `LOCALCLOUD_PROFILE=dev`. Environment endpoint/token overrides still take precedence over saved values; explicit `--endpoint-url` takes precedence over environment endpoints. A verified connection is a saved client configuration, not a replacement for keeping the server running. Existing `configure` and `auth login` commands remain supported.

Modification commands are also available from the CLI:

```bash
localcloud ec2 stop-instances --name my-web --timeout 60
localcloud ec2 modify-instance-attribute --name my-web --instance-type t3.small --timeout 60
localcloud ec2 start-instances --name my-web --timeout 60
localcloud sqs set-queue-attributes --queue-name app-jobs --attributes VisibilityTimeout=60
```

## Troubleshooting

- **No page loads:** start the backend or Kubernetes port-forward first. Open its actual port and `/_local` path.
- **Controls disabled:** enter the correct management token and click Connect. Check the displayed connection error.
- **401 after CLI login:** browser login is separate from CLI profiles. Enter the running server's token in the dashboard. In CLI terminals, an old `LOCALCLOUD_MANAGEMENT_TOKEN` can override the saved login.
- **AccessDenied:** the chosen IAM test principal lacks a policy for that operation/resource when enforcement is enabled.
- **Image not allowed:** restart the server with the appropriate `--ec2-image AMI=IMAGE` mapping. The dashboard displays configured image suggestions.
- **App not reachable:** allow startup time, verify published ports and make the app listen on `0.0.0.0` inside its container. A configured endpoint is not an application health check.
- **Old UI after updating:** restart the backend and hard-refresh the browser. Rebuild/redeploy an existing Docker or Kubernetes image to include the new assets.

## Validation boundaries

HTTP tests exercise the real handler/engine for authenticated S3/SQS CRUD, binary data, queue settings, malformed requests and IAM denial. CLI tests verify connection profile success/failure and modification commands. Browser checks use fixture API responses to exercise connection gating, service controls, downloads, invalid tokens, offline/reconnect behavior, safe payload rendering and responsive layouts. Live browser-to-Docker execution remains to be verified on the host.
