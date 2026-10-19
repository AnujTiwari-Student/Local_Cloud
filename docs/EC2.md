# Local EC2 service

LocalCloud implements a small EC2 lifecycle provider. Mock mode creates durable instance records. Optional Docker mode runs real containers behind those records. Neither mode provides an AWS virtual machine, AWS networking or complete EC2 compatibility.

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

Mock instances do not execute code. State transitions are immediate. Instance records survive server restart; terminated records remain visible and cannot be restarted. Repeating stop/start/terminate for the current state is safe. Mock instances participate in snapshots and portable state alongside S3/SQS. Older state bundles without instances remain accepted.

Open `http://127.0.0.1:4566/_local` to see instance IDs, lifecycle state and backend in the Resource Browser. `localcloud resources` also includes instances.

## Execute containers through EC2

Run the backend on a host with Docker CLI installed and access to a running Docker daemon. The project's default server image does not include the Docker CLI or daemon access. First build a small long-running workload image:

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

Instances use a fixed non-root UID, read-only root filesystem, no network, dropped capabilities, no-new-privileges, 128-process limit, one CPU and a memory limit. The workload must support these restrictions. `t3.micro`, `t3.small` and `t3.medium` map to 128, 256 and 512 MiB respectively; these are local limits, not AWS instance specifications. No host directories, ports or Docker socket are passed to workloads.

Describe refreshes running/stopped state from Docker. Start/stop/terminate verify the container's ownership label. A stopped workload can restart; an exited application appears stopped. Missing containers produce runtime errors rather than silently recreating workloads. Termination removes the container. Stopping LocalCloud does not remove containers: terminate instances before stopping the server, and preserve its database and image mappings for subsequent management.

Docker operations and SQLite commits are not one distributed transaction. A process crash or Docker timeout can leave a container requiring manual inspection. There is no background recovery controller yet. Snapshots and state import are disabled in Docker mode; portable export rejects Docker records. Container filesystems are not captured by state bundles.

## APIs and access

Supported operations:

- `RunInstances`: `ImageId`, `InstanceType`, `MinCount`, `MaxCount`.
- `DescribeInstances`: optional `InstanceIds`.
- `StartInstances`, `StopInstances`, `TerminateInstances`: required `InstanceIds`.

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
