# Run EC2-style instances in Kubernetes

LocalCloud can run its supported EC2 instance APIs using real Kubernetes workloads. An AMI alias maps to a container image, an instance owns a Deployment, ports use a ClusterIP Service, and data volumes use persistent volume claims (PVCs). The backend supports actual lifecycle changes and checks Pod readiness before reporting `running`.

This is distinct from the existing Kubernetes operator, generic Job executor and an emulated EKS service. It runs containers inside your existing cluster; it does not create virtual machines or an EKS control plane.

## Requirements

- A working Kubernetes cluster and namespace.
- An image registry reachable by the nodes, or images loaded into a local kind cluster.
- A default StorageClass when using PVC-backed volumes.
- LocalCloud's existing management token.
- A backend with Kubernetes API access: in-cluster service account, or a loopback `kubectl proxy` for a host process.

Use a separate SQLite state file for each EC2 executor/namespace. The state contains runtime ownership and resource UIDs, so preserve it when restarting. It is not a portable fixture for another cluster.

## Try it with kind

The commands below use an existing kind cluster named `localcloud`. The existing [EC2 infrastructure guide](EC2_INFRASTRUCTURE.md) shows how to build the `localcloud-ec2-web:dev` demo image, which listens on port 8080. Your own image can replace it.

```bash
kubectl --context kind-localcloud create namespace ec2-sandbox
kind load docker-image localcloud-ec2-web:dev --name localcloud
```

In one terminal, expose the Kubernetes API locally:

```bash
kubectl --context kind-localcloud proxy --address 127.0.0.1 --port 8001
```

In another terminal, start LocalCloud with the existing token and a fresh backend state file:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN="$(cat ~/.config/localcloud/server-token)"
localcloud start --port 4567 \
  --data .localcloud/ec2-kubernetes.sqlite3 \
  --ec2-executor kubernetes \
  --ec2-image ami-web=localcloud-ec2-web:dev \
  --ec2-kube-image-pull-policy Never \
  --kube-api-server http://127.0.0.1:8001 \
  --kube-namespace ec2-sandbox
```

`Never` uses the node's local image cache. For registry images, choose `IfNotPresent` or `Always` and use a fully qualified image in the `--ec2-image` mapping. An image-pull failure leaves the instance pending with a reason; creating a Deployment alone does not mean that the guest is running.

In your application terminal, select the same endpoint. A named profile keeps this example separate from other saved endpoints:

```bash
localcloud configure --profile kube-ec2 --endpoint-url http://127.0.0.1:4567
cat ~/.config/localcloud/server-token | localcloud auth login --profile kube-ec2 --token-stdin
localcloud --profile kube-ec2 status
```

If `LOCALCLOUD_MANAGEMENT_TOKEN` is set in this terminal, it must match the server token because it overrides the saved login.

## Launch an application with persistent data

Prepare user data that initializes a file without overwriting it on the next Pod:

```bash
cat > /tmp/ec2-kube-init.sh <<'SH'
#!/bin/sh
set -eu
if [ ! -f /data/marker ]; then
  printf 'saved application data\n' > /data/marker
fi
echo 'initialization complete'
SH

localcloud --profile kube-ec2 ec2 run-instances \
  --image-id ami-web --name web \
  --publish 0:8080 \
  --user-data file:///tmp/ec2-kube-init.sh \
  --block-device-mappings '[{"DeviceName":"/data","Ebs":{"VolumeSize":1,"DeleteOnTermination":false}}]'

localcloud --profile kube-ec2 ec2 describe-instances --name web
```

The launch returns `pending`. Repeat describe until it reports `running`. Management output includes `KubernetesName`, `KubernetesNamespace`, `PodName`, `PodUid` and service `Endpoints`. A Pod must be Running and Ready, and the Deployment must have observed the latest generation. Image pulls, failed initialization and scheduling problems appear in `RuntimeStatusReason`.

The same EC2 operations can be sent through an AWS SDK using the LocalCloud endpoint and existing management authentication. Names and Kubernetes details are local extensions; AWS Query responses retain the supported AWS wire shapes.

### Open the application in your browser

`--publish 0:8080` creates a Kubernetes service on container port 8080. It does not bind a host port. Copy `KubernetesName` from describe and run:

```bash
kubectl --context kind-localcloud -n ec2-sandbox \
  port-forward service/KUBERNETES_NAME 8088:8080
```

Open `http://127.0.0.1:8088`. The service endpoint returned by LocalCloud is directly usable inside the cluster. Explicit nonzero host publishing is rejected by the Kubernetes backend.

### Stop, start, reboot and inspect logs

```bash
localcloud --profile kube-ec2 ec2 get-console-output --name web
localcloud --profile kube-ec2 ec2 stop-instances --name web
localcloud --profile kube-ec2 ec2 describe-instances --name web
```

Stop scales the Deployment to zero. It reports `stopping` until the owned Pods disappear, then `stopped`. Once stopped, you can change the supported instance type or attach/detach filesystem volumes.

```bash
localcloud --profile kube-ec2 ec2 modify-instance-attribute --name web --instance-type t3.small
localcloud --profile kube-ec2 ec2 start-instances --name web
localcloud --profile kube-ec2 ec2 describe-instances --name web
localcloud --profile kube-ec2 ec2 reboot-instances --name web
```

Start creates a new Pod; reboot rolls the Deployment. Both return to pending until the new guest is ready. A former Ready Pod does not establish readiness for the new generation. PVC data persists, while the writable container root and `/tmp` are not durable storage. A port-forward attached to an old Pod may need restarting.

Console output includes user-data initialization and guest logs. Available console output is retained before stop/termination. The HTTP `--timeout` controls the request timeout; it is not a wait-until-ready option.

## Storage and initialization contract

- Each supported volume creates a real `ReadWriteOnce` PVC with the requested size. Binding/capacity behavior depends on the cluster's StorageClass.
- `/data` and `/mnt/NAME` mappings are filesystem mounts. `/dev/sdf` is exposed as `/mnt/ebs/sdf`, not a raw disk.
- Attachment changes require a stopped Deployment with no remaining Pods. The next Pod mounts the updated volume list.
- `DeleteOnTermination: true` requests deletion of the PVC on termination; Kubernetes may finish deletion asynchronously. False retains an available volume for explicit deletion or reattachment.
- CSI snapshots, volume copying/restoring, resizing and performance modification are not implemented. Those requests fail explicitly rather than returning fake successful data operations.
- User data is an init-container script and runs on every new Pod, including stop/start and reboot. Make it idempotent. Put long-running applications in the image's normal command rather than starting background daemons in the init script.
- Guests run as UID/GID 65534 with a read-only root, dropped capabilities, no privilege escalation, a writable `/tmp`, and `fsGroup: 65534` for supported PVC writes. The image must support this contract. User data requires `/bin/sh` in the guest image.
- Supported instance types are `t3.micro`, `t3.small` and `t3.medium`, with local memory limits of 128/256/512 MiB and one CPU. These are local resource choices, not AWS machine specifications.

## Optional instance metadata

Build a LocalCloud helper image from the current project and make it available on the nodes:

```bash
docker build -t localcloud:ec2-kube .
kind load docker-image localcloud:ec2-kube --name localcloud
```

Restart the same backend configuration with:

```bash
--ec2-helper-image localcloud:ec2-kube
```

New guests then include an unprivileged metadata sidecar. Their environment contains:

```
AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:1338
LOCALCLOUD_METADATA_URL=http://127.0.0.1:1338
```

The sidecar supports the existing metadata values and IMDSv2 token protocol. It does not rewrite link-local networking or provide IAM/STS role credentials. Configure guest AWS applications' service endpoints and credentials explicitly. The Docker `--ec2-imds` and `--ec2-guest-mode` flags are rejected for this executor.

```bash
localcloud --profile kube-ec2 ec2 modify-instance-metadata-options --name web --http-tokens required
```

Changing metadata options rolls the Pod so that its sidecar loads the updated payload. Metadata is available after application containers start; the user-data init container runs before the sidecar and cannot rely on it. Adding a helper flag to a backend does not retrofit existing guest templates; configure it before launch.

## Terminate and clean up

```bash
localcloud --profile kube-ec2 ec2 terminate-instances --name web
localcloud --profile kube-ec2 ec2 describe-volumes
localcloud --profile kube-ec2 ec2 delete-volume --volume-id vol-RETAINED
```

Terminate removes the owned Deployment, service and ConfigMap, then handles data volumes according to their deletion policy. Use the retained volume ID from the launch/describe result. Preserve the backend state database until cleanup is complete so ownership and resource UIDs remain available.

## Operator integration

The existing `LocalCloudInstance` spec now accepts an EC2 section:

```yaml
spec:
  image: localcloud:ec2-kube
  managementTokenSecret: localcloud-access
  ec2:
    enabled: true
    images:
      ami-web: localcloud-ec2-web:dev
    pullPolicy: Never
    helperImage: localcloud:ec2-kube
```

Add this section to a complete existing instance manifest. Apply the newly generated installation manifests to obtain the updated CRD schema and namespace Role before using it. The generic `workloadImages` Job executor configuration remains independent.

The executor Role uses namespace-scoped Deployment, ReplicaSet, Pod/log, Service, ConfigMap and PVC permissions. Guests do not mount the backend service-account token. It does not require a cluster-admin role, host networking, privileged Pods or Docker access inside the backend.

## Evidence, comparison and limits

The [REA executor report](REA_EC2_EXECUTORS.md) retains the normal-license LocalStack reference captures. In the tested reference, stock guest image pulling failed, a separately marked cached-image intervention ran a guest, volume attachment changed metadata without a guest disk, and stop/start remained running at one replica. LocalCloud reuses the observed Deployment/Pod structure but independently implements meaningful scaling, readiness, mapped container images and PVC data mounts. These improvements are not a claim of exact reconstruction of proprietary source.

Current live results: `ev_e791cc0b04cd526489a336d3ac3f29571c6ffc9c6330b36a9396fe2aa58b0aaa` — 12 live checks passed; [final report](research/rea/ec2-kubernetes-final-report.json). Unit validation: `ev_e2f1d445289d4f24730f5fa68f9404256b8495bf6b8a1fcc0fde12d4c1a2a59a` — 247 tests passed. See [the evidence index](research/rea/ec2-kubernetes-live-index.json) and [full live REA bundle](research/rea/ec2-kubernetes-live-evidence.json.gz). The [final validation index](research/rea/ec2-kubernetes-final-index.json) and [final REA bundle](research/rea/ec2-kubernetes-final-evidence.json.gz) bind the current source and tests. Reproduce in an existing local kind cluster with:

```bash
python3 scripts/ec2-kubernetes-live.py
python3 scripts/ec2-kubernetes-live.py --with-metadata
python3 -m unittest discover -s tests -q
```

The fixture uses its own namespace, temporary backend state, local proxy/port-forward processes and unique image tags. Its metadata helper copies the current source over cached runtime dependencies without downloading packages; an offline build of the production Dockerfile lacked cached APT packages. The proxy uses the caller’s Kubernetes permissions, so the minimal generated service-account Role was inspected/tested as manifests rather than exercised as the live fixture identity. Cleanup preserves preexisting cluster resources and images.

This is an implemented subset. It does not provide EKS, VMs/Windows, SSH/root mode, raw EBS, physical AWS networking, IAM credentials, CSI snapshots or full AWS compatibility. Successful backend restart preserves recorded Kubernetes/PVC identities. Kubernetes mutations and SQLite commits are not one atomic transaction: abrupt process loss during provisioning can leave resources that require reconciliation. The Docker storage journal does not provide Kubernetes provisioning crash recovery.
