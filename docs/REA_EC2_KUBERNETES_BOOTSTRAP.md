# EC2 Kubernetes metadata during user-data initialization

This follow-up closes LocalCloud's startup-order gap: when an EC2 helper image is configured, user data can read instance metadata before the guest application starts. It is an independent implementation informed by licensed runtime observations, not recovered proprietary source or a claim of full LocalStack compatibility.

## Scope and reference

The reference is LocalStack 2026.10.0, build `738d58ee`, image `localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`, started with the user's normal local license token. This reuses the pinned image from the earlier [executor recovery investigation](REA_EC2_KUBERNETES_COMPLETION.md), rather than claiming to test an unpinned latest release.

All reference guests required a declared cached-image pull-policy intervention. The reference's generated guest starts user data in its main container alongside an `imds-proxy-sidecar`; it does not use LocalCloud's native init-sidecar implementation. Its proxy forwards to `localstack.<namespace>.svc.cluster.local:4510`. The fixture supplies that namespace-local Service.

## What REA observed in LocalStack

The final licensed reference run is `ev_c480cec58a84a49ac1a6e9be8b81b65396e227e54c2d81c6f5594dd293c59957`. The [reference report](research/rea/ec2-kube-bootstrap-reference-report.json), [reference index](research/rea/ec2-kube-bootstrap-reference-index.json) and [full evidence](research/rea/ec2-kube-bootstrap-reference-evidence.json.gz) retain the exact fixture and limitations.

| Question | Observed result |
| --- | --- |
| Can bootstrap reach metadata? | Yes in the controlled fixture: 16 connection-refused attempts, followed by HTTP 200 containing that guest's instance ID. |
| Is metadata ready when user data begins? | Not in this run. User data and the regular proxy sidecar started concurrently; the script needed retries. |
| Why did earlier instance-ID requests return 400? | The server response named missing LocalStack identity headers. A successful one-Pod launch subsequently made ordinary header-free guest requests return 200. The earlier fixture did not establish completed launch identity setup. |
| Does IMDSv2 transport work? | Token PUT and authenticated instance-ID GET both returned 200. |
| Was required-token enforcement established? | No. Immediately after requesting required tokens, an unauthenticated GET still returned 200 in this bounded observation. This does not prove behavior across other backends or later times. |
| Were user-data/IAM credential endpoints supported? | In this reference fixture both returned 404 with an endpoint-not-implemented response. |

The successful bootstrap probe deliberately supplies `x-localstack-imds-account-id`, `x-localstack-imds-region-name` and `x-localstack-imds-instance-id` headers, with the instance ID derived from its own Pod hostname. This is a declared diagnostic workaround, not proof that an untouched bootstrap request needs or automatically gets those headers. Later ordinary guest requests succeeded without them. The fixture changes the generated Deployment to cached-image `Never` and `Recreate` before launch completion to avoid an unavailable registry pull and duplicate guest Pods. Those interventions are recorded separately from stock behavior.

The first new probe is retained as preliminary evidence (`ev_fa96add8f8a7249bda33e920ac29ffa76d85cd16070b29e73809a10791d14774`): its ten bootstrap attempts all encountered connection refusal, while later requests exposed the missing-header error. The final follow-up answers the startup-availability question; it does not establish full LocalStack API or token-enforcement parity.

LocalCloud deliberately uses native sidecar startup ordering instead of reproducing the observed race. Its existing stricter required-token behavior and supported user-data endpoint remain documented differences.

## LocalCloud implementation

In `localcloud/ec2_kubernetes.py`, the optional metadata helper is now the first `initContainers` entry, with `restartPolicy: Always`. Its startup probe checks the Pod-local listener at `127.0.0.1:1338`. Kubernetes waits for that probe before starting the next init container, `user-data`. The helper then remains alive for both initialization and the main guest.

Both user data and the guest receive `AWS_EC2_METADATA_SERVICE_ENDPOINT` and `LOCALCLOUD_METADATA_URL`. They point to the same Pod-local endpoint. No privileged networking, host access, management bearer token or Kubernetes service-account credential is added. The helper does not mount guest data PVCs. Volume reconfiguration updates only the user-data init container's mounts.

A socket startup probe intentionally avoids requiring a successful metadata HTTP request: `HttpTokens=required` must not block startup, and `HttpEndpoint=disabled` must still let the guest run. Existing metadata authorization applies to requests: required-token requests without a valid token return 401, and disabled metadata returns 404. Metadata includes the existing identity values, identity document and user-data bytes; IAM/STS profile credentials remain unimplemented.

Without a helper image, no metadata environment or sidecar is injected. This feature requires Kubernetes native sidecar support (`SidecarContainers`, enabled by default since 1.29, stable since 1.33). See [Kubernetes sidecar lifecycle and startup probes](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/).

## Using it

Follow the [complete setup and runnable initialization example](EC2_KUBERNETES.md#optional-instance-metadata). Build a helper from current code, load it into the cluster, configure `--ec2-helper-image`, then launch your guest with `--user-data file:///path/to/init.sh`. Your script can request an IMDSv2 token and write `/data/instance-id` before the application reads it.

The configured guest image must contain `/bin/sh` and any program used by your initialization script. The documented example uses Python 3. User data reruns for each new Pod; keep generated configuration and data updates idempotent. Failed helper startup leaves user data waiting and the instance pending rather than reporting a usable guest.

## Validation

All **274 unit tests passed**, including explicit ordering, endpoint propagation, helper-without-user-data, no-helper behavior, and preservation of metadata container isolation during volume reconfiguration. Unit REA evidence: `ev_86710ab143b36f8d2f9783ab5c6728130391ea644055ab233db359d4f0d76b3a`; [validation index](research/rea/ec2-kube-bootstrap-validation-index.json), [evidence bundle](research/rea/ec2-kube-bootstrap-validation-evidence.json.gz).

The dedicated live fixture passed **18 checks on Kubernetes v1.37.0**: `ev_a9c84e1078e06ba230cc7df58fa5aa0dc7e91e25372a228986333554c7cac14b`. See the [live report](research/rea/ec2-kube-bootstrap-live-report.json), [live index](research/rea/ec2-kube-bootstrap-live-index.json) and [live evidence bundle](research/rea/ec2-kube-bootstrap-live-evidence.json.gz).

The checks run actual user data that obtains an IMDSv2 token, verifies unauthenticated requests fail with 401, reads its instance ID/type/region and identity document, hashes its user-data response, and writes the results into a PVC for the guest to inspect. Stop/start and reboot run initialization again. A disabled endpoint returns 404 during initialization while the application still becomes ready. A missing helper image leaves metadata in `ErrImageNeverPull`, with user data and the guest not started and EC2 pending. The fixture also checks application reachability, console output, preserved PVC bytes, backend restart and termination cleanup. All owned namespaces and image tags were removed; cleanup evidence is `ev_ba8f54d32ebe8ffb6ee1842581418aaf2d21425b6dc8b67c48d31e7b718f9d37`.

The dedicated live fixture is `scripts/ec2-kube-bootstrap-live.py`. It uses the explicit `kind-localcloud` context, a uniquely labeled namespace, temporary databases, unique image tags and owned proxy/port-forward processes. Its helper copies current source over cached runtime dependencies without package downloads. This host fixture uses the caller's Kubernetes permissions through a local proxy; it does not retest the separate minimal-service-account recovery fixture.

Reproduce with an existing cluster and the cached guest/runtime images described in the setup:

```bash
python3 scripts/ec2-kube-bootstrap-live.py
python3 scripts/ec2-kube-bootstrap-reference.py
python3 -m unittest discover -s tests -q
```

## Boundaries

The listener-ready startup gate establishes initial availability, not uninterrupted availability after a later helper crash. Applications still need normal request timeouts and error handling. No link-local rewriting, IAM credentials, general cloud-init compatibility, or broader EC2 parity is added by this change. Existing storage, networking and account/region limitations remain separate.
