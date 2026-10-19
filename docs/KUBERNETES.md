# Kubernetes pack: installation, pod execution and previews

Prefer commands over curl? See the [CLI guide](CLI.md), including automatic token login from a Kubernetes Secret.

This is LocalCloud's own Kubernetes integration: a namespace-scoped operator, instance CRD, private Service, optional PVC, and Job-based workload executor. It does not implement the LocalStack Kubernetes Pack or AWS Lambda/ECS/EC2 APIs.

The Python code has no extra dependencies. Installation requires a working Kubernetes cluster, kubectl, and a LocalCloud container image reachable by that cluster. A persistent instance also needs a default StorageClass or an explicitly configured one.

## 1. Build an image and prepare a local cluster

From the project directory:

```bash
docker build -t localcloud:dev .
```

For a new local test cluster with Kind already installed:

```bash
kind create cluster --name localcloud
kind load docker-image localcloud:dev --name localcloud
kubectl config current-context
```

The context should be `kind-localcloud`. For an existing development cluster, use its intended context and load/publish the image appropriately. Local Docker images are not automatically visible to Kubernetes nodes. Image pulls use `IfNotPresent`; use a new image tag for an updated release.

The following installation writes a CRD, namespace, two namespaced service accounts/roles/bindings, a Job quota and an operator Deployment into the selected cluster. It requires permission to install a CRD and namespace. The running operator itself has only namespace-scoped permissions.

## 2. Install the operator

```bash
python3 -m localcloud.operator --namespace localcloud manifests \
  --image localcloud:dev > /tmp/localcloud-install.json

kubectl apply -f /tmp/localcloud-install.json
kubectl wait --for=condition=Established \
  crd/localcloudinstances.localcloud.dev --timeout=60s
kubectl -n localcloud rollout status deployment/localcloud-operator --timeout=180s
```

Create a management token and Secret. Keep this terminal's environment for the curl examples:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

python3 - <<'PY' | kubectl -n localcloud create -f -
import json, os
print(json.dumps({
    'apiVersion': 'v1', 'kind': 'Secret',
    'metadata': {'name': 'localcloud-access'}, 'type': 'Opaque',
    'stringData': {'token': os.environ['LOCALCLOUD_MANAGEMENT_TOKEN']}
}))
PY
```

The Secret must contain a `token` key. Pods receive that value through a Secret reference. The operator does not read Secret data. It does not generate or rotate secrets automatically.

## 3. Create a persistent emulator instance

```bash
python3 -m localcloud.operator --namespace localcloud example \
  --name dev --image localcloud:dev > /tmp/localcloud-dev.json
kubectl apply -f /tmp/localcloud-dev.json

kubectl -n localcloud get localcloudinstances
kubectl -n localcloud wait --for=jsonpath='{.status.phase}'=Ready \
  localcloudinstance/dev --timeout=180s
kubectl -n localcloud get deployments,services,pvc
```

The generated instance uses one replica, IAM soft mode, a 1 GiB PVC retained when the instance is deleted, and allows workload image `localcloud:dev`. SQLite runs in a single process; the operator uses a Recreate deployment strategy to avoid overlapping writers.

Inspect `/tmp/localcloud-dev.json` to change supported fields before applying:

| `spec` field                   | Behavior                                                                                 |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `image`                        | LocalCloud container image.                                                              |
| `managementTokenSecret`        | Existing Secret name; required, key `token`.                                             |
| `iamMode`                      | `off`, `soft` or `enforce`.                                                              |
| `workloadImages`               | Exact image allowlist; an empty list disables execution.                                 |
| `persistence.enabled`          | PVC when true, ephemeral emptyDir when false.                                            |
| `persistence.size`             | Positive `Mi`/`Gi` quantity, default `1Gi`.                                              |
| `persistence.storageClassName` | Optional StorageClass.                                                                   |
| `persistence.retain`           | Default true; PVC survives instance deletion. Set false initially for owner-GC deletion. |
| `ttlSeconds`                   | Optional instance lifetime, 60 seconds–7 days from CR creation. Expiry deletes the CR.   |

PVC size, storage class and retention cannot be changed by this operator after creation. Deployment/image/IAM/allowlist changes reconcile automatically. Existing unrelated resource names and different owner UIDs are rejected instead of adopted. Retained PVCs with matching managed labels can be reused when recreating the same instance name.

If your cluster has no storage provisioner, use an ephemeral instance:

```bash
python3 - <<'PY'
import json
p = '/tmp/localcloud-dev.json'
spec = json.load(open(p))
spec['spec']['persistence']['enabled'] = False
with open(p, 'w') as f:
    json.dump(spec, f, indent=2)
PY
kubectl apply -f /tmp/localcloud-dev.json
```

## 4. Access the dashboard and service APIs

In another terminal:

```bash
kubectl -n localcloud port-forward service/dev 4566:4566 --address=127.0.0.1
```

Open `http://127.0.0.1:4566/_local`. Enter the management token in the dashboard's password field and click **Connect**. It stays in page memory and is not saved in browser storage.

From the terminal where the token is set:

```bash
export CLOUD_URL=http://127.0.0.1:4566
curl "$CLOUD_URL/_local/health"
curl "$CLOUD_URL/_local/resources" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN"

# S3/SQS are the same APIs described in USAGE.md.
curl -X PUT "$CLOUD_URL/kube-assets"
curl -X PUT "$CLOUD_URL/kube-assets/hello" --data-binary 'hello from Kubernetes'
curl "$CLOUD_URL/kube-assets/hello"
```

Only health and the dashboard HTML are public among management routes. Other management APIs require the token. S3/SQS still use the emulator's local test identities and IAM mode, not management bearer-token authentication. Keep the Service private; this is not a hardened multi-tenant AWS endpoint.

QueueUrl values advertise the in-cluster Service origin, such as `http://dev.localcloud.svc:4566`. Host curl calls should POST to the port-forward origin; QueueUrl can be passed unchanged as an identifier. SDKs that connect directly to the returned cluster DNS name need cluster networking/DNS. SDK drop-in compatibility remains unverified.

## 5. Submit and inspect a real workload pod

```bash
curl "$CLOUD_URL/_local/executions" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"image":"localcloud:dev","command":["python3","-c","import os,urllib.request; print(urllib.request.urlopen(os.environ[\"LOCALCLOUD_URL\"]+\"/_local/health\").read().decode())"],"timeoutSeconds":120,"ttlSeconds":600}'
```

The response contains a generated `name`, for example `lc-run-...`. Set that exact value:

```bash
export JOB_NAME=lc-run-REPLACE_WITH_RETURNED_NAME

curl "$CLOUD_URL/_local/executions" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN"
curl "$CLOUD_URL/_local/executions/$JOB_NAME" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN"
curl "$CLOUD_URL/_local/executions/$JOB_NAME/logs" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN"

# Cancel an active job or remove a completed job.
curl -X DELETE "$CLOUD_URL/_local/executions/$JOB_NAME" \
  -H "Authorization: Bearer $LOCALCLOUD_MANAGEMENT_TOKEN"
```

The dashboard also provides image/command submission, job status, log viewing and deletion.

A request accepts `image`, a nonempty `command` array, optional string `env`, `timeoutSeconds` (1–3,600; default 300), and cleanup `ttlSeconds` (30–86,400; default 600). Commands are passed as an argv array, without an implicit shell. Unapproved images and overrides of reserved `AWS_`/`LOCALCLOUD_` environment variables are rejected.

Workload pods receive:

- `LOCALCLOUD_URL` and `AWS_ENDPOINT_URL`: the instance's in-cluster endpoint.
- `AWS_DEFAULT_REGION=us-east-1` and dummy AWS credentials.
- Your additional allowed environment variables.

They do not receive a Kubernetes service-account token or management bearer token. Each pod runs as UID 10001, with a read-only root filesystem, writable bounded `/tmp`, no added capabilities, and CPU/memory limits. Images must support this environment. Use an IAM policy for default principal `test` when enabling enforced mode; dummy AWS credentials do not map to local principals automatically.

Jobs do not retry (`backoffLimit=0`). Status is Pending, Running, Complete or Failed. Logs return at most 200 lines/64 KiB from the workload container. Failed image pulls may stay Pending until the active deadline. Finished Jobs are garbage-collected by Kubernetes after their TTL; fetch logs before that. A namespace quota caps existing Jobs at 20, including completed Jobs awaiting TTL cleanup. Deleting an instance also garbage-collects its owned Jobs.

## 6. Create preview instances with expiry

The operator must remain running for instance TTL cleanup. On your host, expose the current context's API through a loopback-only proxy in another terminal:

```bash
kubectl proxy --address=127.0.0.1 --port=8001
```

Then:

```bash
python3 -m localcloud.operator --api-server http://127.0.0.1:8001 \
  --namespace localcloud preview create --name pr-42 \
  --image localcloud:dev --ttl 3600

python3 -m localcloud.operator --api-server http://127.0.0.1:8001 \
  --namespace localcloud preview list

kubectl -n localcloud wait --for=jsonpath='{.status.phase}'=Ready \
  localcloudinstance/pr-42 --timeout=180s
kubectl -n localcloud port-forward service/pr-42 4567:4566 --address=127.0.0.1
```

Preview dashboard: `http://127.0.0.1:4567/_local`. The default Secret is `localcloud-access`; pass `--secret OTHER_NAME` to select another existing Secret. Each preview gets its own process, Service and emptyDir state. This is a private cluster endpoint, not public cloud hosting or an automatically published PR URL.

Delete early:

```bash
python3 -m localcloud.operator --api-server http://127.0.0.1:8001 \
  --namespace localcloud preview delete --name pr-42
```

This command refuses to delete an instance lacking the preview label. Kubernetes owner references clean up the Service, Deployment and Jobs; emptyDir data disappears with the pods. Secrets are shared references and are not deleted by instance cleanup. TTL starts at CR creation, not readiness. Expired instances are deleted when the operator next reconciles; downtime delays cleanup until it resumes.

## 7. Validation and troubleshooting

Unit/contract tests:

```bash
python3 -m unittest discover -s tests -v
```

Live tests against a **disposable development cluster**, with `localcloud:ci` already loaded:

```bash
python3 scripts/kubernetes_smoke.py --image localcloud:ci
```

This installs the operator in `localcloud-ci`, creates a temporary token/instance, verifies S3/SQS and a Job calling the emulator endpoint, collects artifacts, and deletes its own instance and Secret. The namespace/operator/CRD remain; the GitHub Actions Kind cluster is discarded by its action at job end.

Diagnostics:

```bash
kubectl -n localcloud get localcloudinstances,pods,pvc,jobs
kubectl -n localcloud describe localcloudinstance/dev
kubectl -n localcloud logs deployment/localcloud-operator
kubectl -n localcloud describe pod -l localcloud.dev/instance=dev
kubectl -n localcloud describe pvc dev-data
```

- Pending PVC: configure a StorageClass or use emptyDir.
- ImagePullBackOff: make the exact image tag available to the nodes.
- CreateContainerConfigError: check the Secret name and `token` key.
- Executor quota error: remove completed Jobs or wait for TTL cleanup.
- HTTP 401: supply the correct management token.
- Operator status Error: inspect its message; conflicts and immutable storage changes need explicit resolution.

Delete a persistent instance using `kubectl -n localcloud delete localcloudinstance dev`. Its PVC is retained by default. Delete `pvc/dev-data` separately only when its data is no longer needed. Removing the entire namespace also removes retained PVCs and Secrets.

Local validation covers generated resources, reconciliation, ownership, TLS/token client behavior and HTTP contracts. The authoring sandbox blocks Docker/socket access, so live image/cluster execution remains to be verified by the included CI jobs or your development cluster.
