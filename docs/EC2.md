# Local EC2 service

Current checkpoint: [EC2 release and verification](EC2_RELEASE.md). The later
checkpoint expands the operation set described below to 97 selected AWS operations.

LocalCloud implements a small EC2 lifecycle provider. Mock mode creates durable instance records. Optional Docker mode runs real containers behind those records. Neither mode provides an AWS virtual machine, AWS networking or complete EC2 compatibility.

The latest [EC2 networking, storage, IMDS and SSH guide](EC2_INFRASTRUCTURE.md)
contains the expanded setup and evidence. The earlier
[EC2 reference coverage and CLI walkthrough](EC2_REFERENCE.md) covers
tags, filters, image discovery, launch idempotency, user-data scripts and SDK
access, with captured LocalStack behavior and explicit remaining limits.

## Start and use mock instances

From the LocalCloud checkout, start the backend:

```bash
export PATH="$PWD/bin:$PATH"
localcloud start
```

In another terminal, use the installed `localcloud` command, or set the same PATH:

```bash
localcloud ec2 run-instances --image-id ami-local --instance-type t3.micro --count 1
localcloud ec2 describe-instances
```

Copy the returned `InstanceId`, replacing `i-REPLACE` below:

```bash
localcloud ec2 stop-instances --instance-ids i-REPLACE
localcloud ec2 start-instances --instance-ids i-REPLACE
localcloud ec2 terminate-instances --instance-ids i-REPLACE
localcloud ec2 describe-instances --instance-ids i-REPLACE
```

Mock instances do not execute code. Lifecycle responses report transient states (pending, stopping, shutting-down) and settle immediately in stored state; describe returns the final state. Instance records survive server restart; terminated records remain visible and cannot be restarted. Repeating stop/start/terminate for the current state is safe. Mock instances participate in snapshots and portable state alongside S3/SQS. Older state bundles without instances remain accepted.

Open `http://127.0.0.1:4566/_local` to see instance IDs, lifecycle state and backend in the Resource Browser. `localcloud resources` also includes instances.

## Names and other instance identifiers

Give a new instance a persistent, unique name:

```bash
localcloud ec2 run-instances --image-id ami-web --name my-web \
  --publish 8088:8080 --timeout 60
localcloud ec2 describe-instances --name my-web
localcloud ec2 stop-instances --name my-web --timeout 60
localcloud ec2 start-instances --name my-web --timeout 60
localcloud ec2 terminate-instances --name my-web --timeout 60
```

This Docker example requires the image mapping in the web deployment walkthrough below. Mock launches also accept names, without port publishing. A named launch creates exactly one instance. Names use 1–80 letters, numbers, underscores or hyphens, are case sensitive and must be unique in the database, including terminated records. Rename an old record if you want to free its name. Names are a LocalCloud extension, not complete AWS tagging support.

Each lifecycle command and describe accepts exactly one selector:

| CLI flag | Selects |
| --- | --- |
| `--instance-ids i-...` | Existing EC2 IDs; retains multi-ID support where the backend permits it. |
| `--name my-web` | LocalCloud instance name. |
| `--container-id FULL_OR_SHORT_ID` | Docker container ID, with at least 12 hex characters; ambiguous prefixes fail. |
| `--container-name localcloud-i-...` | Exact Docker container name. |
| `--host-port 8088` | Published host TCP port of a non-terminated managed instance. |

`localcloud ec2 describe-instances` lists everything without requiring a remembered ID. Docker records include `ContainerId` and `ContainerName`, alongside the EC2 ID, optional Name, port mappings and endpoints. For older records, describe fills in a missing container ID from Docker after checking ownership. `localcloud resources` is also useful, although old records may need describe first to populate container IDs.

To recover and name an already-running web app after stopping the LocalCloud server:

1. Restart the backend with **the same database and saved token**, plus its image mappings. Stopping the server does not stop Docker workloads.
2. Configure the CLI to that backend's actual port and authenticate as described below.
3. List your instances or select the app by its published host port:

```bash
localcloud ec2 describe-instances
# Replace 8088 with the host port where your app responds:
localcloud ec2 name-instance --host-port 8088 --new-name my-web
localcloud ec2 stop-instances --name my-web --timeout 60
localcloud ec2 start-instances --name my-web --timeout 60
```

You can also name or rename an existing instance through `name-instance --instance-ids i-... --new-name my-web`, `--container-id`, `--container-name`, or its current `--name`. Renaming changes only LocalCloud metadata; the Docker container keeps its original name. Names persist across server restarts and mock state snapshots/imports. Container selectors never adopt unrelated Docker containers. An instance ID is required if a selector matches multiple records; do not select by an ambiguous shared image name or an application container port.

## Execute containers through EC2

Run the backend on a host with Docker CLI installed and access to a running Docker daemon. The server image includes the Docker CLI, but daemon access must be configured separately; host execution is simplest. First build a small long-running workload image:

```bash
mkdir -p /tmp/localcloud-ec2-demo
cat > /tmp/localcloud-ec2-demo/Dockerfile <<'EOF'
FROM alpine:3.21
USER 65534:65534
CMD ["sleep", "86400"]
EOF
docker build -t localcloud-ec2-demo:dev /tmp/localcloud-ec2-demo
```

Stop the previous backend. Create a private token and use a separate database for Docker mode:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
localcloud start --data .localcloud/ec2-docker.sqlite3 \
  --ec2-image ami-demo=localcloud-ec2-demo:dev
```

In another terminal, run `localcloud auth login` and enter that token from the first terminal's environment. The variable's name is not the token value. Alternatively, clients launched with the same exported token use it automatically.

```bash
localcloud ec2 run-instances --image-id ami-demo --instance-type t3.micro
localcloud ec2 describe-instances
```

Use the same start/stop/terminate commands from mock mode. Docker operations may take up to 30 seconds; add `--timeout 60` to CLI commands when necessary. Docker mode permits one instance per launch and one instance per lifecycle mutation. Only explicitly mapped images are accepted; repeat `--ec2-image AMI=IMAGE` to allow additional images. Inspect the corresponding container with:

```bash
docker ps -a --filter name=localcloud-i-
```

Instances use a fixed non-root UID, read-only root filesystem, dropped capabilities, no-new-privileges, 128-process limit, one CPU and a memory limit. The workload must support these restrictions. `t3.micro`, `t3.small` and `t3.medium` map to 128, 256 and 512 MiB respectively; these are local limits, not AWS instance specifications. No host directories or Docker socket are passed to workloads. By default networking is disabled. An explicit `--publish` enables Docker bridge networking and publishes only the requested TCP ports on host loopback; bridge networking also permits outbound traffic using the host Docker configuration.

Describe refreshes running/stopped state from Docker. Start/stop/terminate verify the container's ownership label. A stopped workload can restart; an exited application appears stopped. Missing containers produce runtime errors rather than silently recreating workloads. Termination removes the container. Stopping LocalCloud does not remove containers: terminate instances before stopping the server, and preserve its database and image mappings for subsequent management.

Docker operations and SQLite commits are not one distributed transaction. A process crash or Docker timeout can leave a container requiring manual inspection. There is no background recovery controller yet. Snapshots and state import are disabled in Docker mode; portable export rejects Docker records. Container filesystems are not captured by state bundles.

## Deploy a web application

The LocalCloud backend port and the app's published port are separate. For example, use 4567 for LocalCloud and 8088 for your app. Configuring the CLI endpoint does not change the server's listening port; pass `--port` when starting the server.

From the repository root, build the included sample:

```bash
docker build -t localcloud-ec2-web:dev examples/ec2-web
```

Stop an existing server on 4567 in its original terminal before restarting it with the image mapping. Keep the same database if it already contains Docker instances you want to manage. In terminal A:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
localcloud start --port 4567 --data .localcloud/ec2-docker.sqlite3 \
  --ec2-image ami-web=localcloud-ec2-web:dev
```

Use your existing token instead of generating another if other clients already depend on it. In terminal B:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4567
localcloud auth login
```

Enter the value of terminal A's token at the hidden prompt. You can obtain that value with `printenv LOCALCLOUD_MANAGEMENT_TOKEN` in terminal A; do not paste it into shared logs or commit it. Then launch:

```bash
localcloud ec2 run-instances --image-id ami-web --publish 8088:8080 --timeout 60
localcloud ec2 describe-instances
```

Open **http://127.0.0.1:8088** on the Docker host. The response includes `PortMappings` and `Endpoints`; an endpoint is a configured address, not a readiness guarantee. Give the application time to start. The app must listen on `0.0.0.0:8080` inside its container, not container loopback. Its port may differ from the published host port.

For your own application, build its image, add an `--ec2-image ami-myapp=myapp:dev` mapping at server startup, then launch it with the appropriate `--publish` mapping. It must run under the documented non-root, read-only filesystem and resource restrictions.

Repeat `--publish` for up to eight TCP mappings. Both ports must be integers from 1024 to 65535. Host IP selection, UDP, host networking and automatic free-port allocation are unsupported. Publishing is Docker-only; mock launches reject it. Ports remain assigned in LocalCloud's metadata while an instance is stopped. Terminate that instance before reusing its host port for another instance. Docker reports collisions with unrelated host listeners as runtime errors; choose another host port. Check the workload's startup logs using `docker logs localcloud-INSTANCE_ID` if the browser cannot connect.

Use the returned instance ID for cleanup:

```bash
localcloud ec2 stop-instances --instance-ids i-REPLACE --timeout 60
localcloud ec2 start-instances --instance-ids i-REPLACE --timeout 60
localcloud ec2 terminate-instances --instance-ids i-REPLACE --timeout 60
```

Stopping makes the app unavailable; starting keeps its configured port mapping; terminating removes the container and releases its ports. Localhost refers to the Docker host: a remote Docker daemon or a backend running on another machine requires access to that host. Kubernetes Service port-forwarding to LocalCloud does not forward these application ports.

## Modify an instance type

Stop an instance before changing its local resource preset:

```bash
localcloud ec2 stop-instances --name my-web --timeout 60
localcloud ec2 modify-instance-attribute --name my-web --instance-type t3.small --timeout 60
localcloud ec2 start-instances --name my-web --timeout 60
```

Only `t3.micro`, `t3.small` and `t3.medium` are supported. Docker mode applies the documented memory limit and a swap limit of twice that amount through `docker update`, after ownership and stopped-state checks. A runtime failure leaves the recorded type unchanged. Other attributes are not supported. The dashboard offers the same control in instance details.

## APIs and access

Supported operations:

- `RunInstances`: `ImageId`, `InstanceType`, `MinCount`, `MaxCount`, plus LocalCloud-specific JSON `Name` and `PortMappings` extensions.
- `DescribeInstances`: optional `InstanceIds` or one LocalCloud selector (`InstanceName`, `ContainerId`, `ContainerName`, `HostPort`).
- `StartInstances`, `StopInstances`, `TerminateInstances`: `InstanceIds` or one LocalCloud selector.
- `NameInstance`: exactly one selected instance and the new `Name` (LocalCloud extension).
- `ModifyInstanceAttribute`: exactly one selected stopped instance and an `InstanceType` preset. Local JSON accepts the type as a string; other AWS attribute shapes are unsupported.

The LocalCloud CLI uses JSON at `POST /_local/ec2`, protected by the management token when configured. The backend also accepts EC2 Query protocol form POST requests at `/` with `Action`, `Version=2016-11-15` and `InstanceId.N` fields, returning XML. Docker mode requires the management bearer token on Query requests too. Normal AWS clients do not automatically send that custom token, so use the LocalCloud CLI for Docker mode. Full AWS SDK interoperability has not been validated. Unsupported parameters and operations fail explicitly.

IAM testing uses `ec2:OperationName`, the existing caller header and local instance ARNs, for example `arn:aws:ec2:us-east-1:000000000000:instance/i-...`. Launch and unfiltered describe authorize against `*`. Every explicitly selected instance is checked before mutation. These are test identities; AWS signatures and credentials are not authenticated. Use this service on trusted local endpoints.

EC2 requests appear in `localcloud traces list` and support existing IAM policy generation and fault rules:

```bash
localcloud faults add --help
localcloud traces list
```

Limits: 100 non-terminated instances per database and at most 10 mock instances per launch. No AMI discovery, tags, filters, pagination, client-token idempotency, user-data, SSH, public IPs, VPC/security groups, EBS, EC2-backed EKS or provisioning integration in project CI configuration yet. Images and runtimes may require initial downloads.

## Validation

Automated tests exercise persistent lifecycle transitions, IAM denial across multiple IDs, snapshots/import, Query XML, CLI payloads, token protection, traces/faults and Docker command construction/failure/ownership checks. Docker tests use a mocked subprocess boundary; live daemon execution must be verified on the host using the walkthrough above.

### Live verification through REA MCP

The authenticated [EC2/Kubernetes comparison](REA_EC2_KUBERNETES.md) now includes
LocalStack 2026.10.0 mock and Docker lifecycle captures. The AWS Query endpoint
accepts `InstanceId` and `InstanceType.Value` for ModifyInstanceAttribute. This
fix does not add full AWS SDK coverage, transition-state parity or Kubernetes EC2
execution; those gaps are listed in the comparison report.

On 20 October 2026, the REA process runner successfully exercised the sample web image through real Docker launch, loopback HTTP access, stop, type modification, restart, termination and removal, using an isolated temporary backend. This supersedes the earlier lack of host execution evidence for this tested path. It does not establish LocalStack EC2 parity. See [the complete audit and evidence](REA_SERVICES.md).
