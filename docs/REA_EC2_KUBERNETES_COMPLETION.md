# EC2 Kubernetes: recovery and reconciliation audit

Investigation date: 25 October 2026. This extends the [working Kubernetes executor](EC2_KUBERNETES.md) and its [earlier REA comparison](REA_EC2_EXECUTORS.md). It closes the tested provisioning-crash and resource-reconciliation gaps. It does not establish equivalence for every LocalStack/Kubernetes feature.

## Reference identity and comparison

The normal-license reference remains LocalStack 2026.10.0, pinned to:

```
localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872
```

The new audit corroborates the cached node image against the host pin using all 23 root filesystem layers and the image configuration. The node/container image IDs use different digest representations, so a differing displayed ID alone was not treated as a version difference.

The reference guest image needed the previously documented, explicit cached-image `imagePullPolicy: Never` intervention. Results following that intervention describe a controlled configuration rather than an unmodified stock launch.

| Behavior | Reference observation | LocalCloud behavior |
| --- | --- | --- |
| Stop/start | HTTP 200; previous/current state remained running; one Deployment replica remained | Desired running/stopped state is persisted; actual scale and Pod readiness determine reported state |
| Reboot | HTTP 200; same guest Pod UID in the bounded three-second observation window | A Deployment rollout replaces the guest Pod, verified in the earlier live test |
| Reference controller restart with persistence | Tagged EC2 record and guest Pod survived the tested restart | Committed Deployment/PVC UIDs survive backend restart |
| Guest Deployment deleted outside EC2 API | Describe still reported running; start returned `InvalidInstanceID.NotFound`; reboot did not recreate it | Describe/resources reconcile missing owned runtime objects and update recorded UIDs |
| Instance metadata | Correct namespace Service `localstack:4510` enabled token issuance (200); instance-ID requests returned 400, with bootstrap and token-enforcement semantics unresolved in the controlled-image fixture | Pod-local metadata and IMDSv2 work in the earlier 12-check LocalCloud fixture; bootstrap ordering remains a separate limitation |
| Provisioning interrupted after Kubernetes accepted a create | No reference recovery guarantee established by these captures | Durable intent journal compensates incomplete creates/updates and defers deletes until state commit |

The metadata follow-up corrected a fixture error: the reference sidecar forwarded to `localstack.<namespace>.svc.cluster.local:4510`, so a `localstack-main:4566` Service did not satisfy it. Correcting that service made token transport work. Remaining instance-identity responses may depend on the cached-image/Pod-IP mapping, so the 400 responses are not proof of globally absent token enforcement or metadata support. The [corrected metadata report](research/rea/ec2-kube-completion-reference-metadata-diagnostic-report.json) retains the distinction.

These observations are bounded to the pinned image, normal entitlement, isolated namespace and captured calls. HTTP success is not evidence that a Pod stopped, restarted, became Ready or mounted a disk. Stronger LocalCloud recovery is independently implemented, rather than reconstructed proprietary source.

## Durable Kubernetes effects

Kubernetes and SQLite cannot share a normal database transaction. The adapter now records external effects in a separate durable SQLite journal **before** making Kubernetes requests.

For a main database such as:

```
.localcloud/ec2-kubernetes.sqlite3
```

the journal is:

```
.localcloud/ec2-kubernetes.sqlite3.ec2-kube-journal.sqlite3
```

Keep both files. While a backend is running, SQLite WAL/SHM files may also contain current data; stop/checkpoint cleanly before copying state. A journal in `:memory:` cannot survive process loss.

### Commit and recovery sequence

1. An EC2 state transaction begins and obtains a transaction ID.
2. Each Kubernetes create or update records its intent, ownership labels, operation nonce and applicable UID. Creates/updates can then execute.
3. Deletes are recorded but **not executed** before the state transaction commits.
4. The EC2 JSON state, PVC UID bookkeeping and a transaction commit marker commit together in the main database.
5. Once SQLite commits or rolls back, recovery examines the commit marker. Startup also runs recovery.
6. Committed creates/updates are retained. Uncommitted creates are removed only if ownership/nonce/UID checks match. Uncommitted updates use three-way rollback to preserve unrelated concurrent changes.
7. Committed deletes execute with UID preconditions and remain pending until Kubernetes confirms that the object is absent. Uncommitted delete intents are discarded.
8. Settled journal entries and unused commit markers are pruned.

A timed-out or lost create response does not require receiving its UID to recover: the durable operation nonce identifies the newly created object. If the UID was received, recovery also verifies it. Resources replaced by another owner or identity are preserved, with pending diagnostics rather than blind deletion.

Update rollback changes only fields the operation changed. An unrelated concurrent annotation can survive rollback; a conflicting change to the same field is held for investigation. Active operations in another backend are skipped using process/session identity. Failed recovery does not replace the original request error or turn an already committed state change into a false request failure.

## Reconciliation contract

Reconciliation runs when EC2 instances are described and when the management resource/dashboard view is refreshed. Journal recovery runs at backend startup and after settled state transactions. This is not a separate continuously running Kubernetes operator.

- `KubernetesDesiredState` preserves the requested running/stopped/terminated state independently of an observation of the current Pod.
- Missing ConfigMaps are reconstructed from the saved user data and metadata options.
- Missing Services are reconstructed from saved port mappings.
- Missing Deployments are reconstructed from the allowlisted image, saved type, mounts and desired state. The new UID is recorded; a stopped instance is recreated at zero replicas.
- Controlled specification and replica drift is reconciled. Configuration fingerprints roll templates when their saved bootstrap/metadata changes.
- Existing same-name objects must still have the expected owner labels and UID. A replacement is not adopted or overwritten.
- Missing or deleting PVC data is **not recreated as an empty replacement**. The instance reports pending with a missing-data reason.
- `RuntimeStatusReason` explains pending readiness, missing data or blocked recovery. The dashboard resource view reports a failed refresh as pending rather than showing a stale healthy record.

The adapter repairs only recorded instances and named runtime objects. It does not scan arbitrary namespace resources and delete objects merely because they look similar.

### Termination is asynchronous

`TerminateInstances` now reports `shutting-down` while external resources or their deletion intents remain. It reaches `terminated` after the owned workload/configuration/service resources and delete-on-termination PVCs are confirmed absent. Retained volumes remain available for explicit handling. Describe the instance until completion; do not infer finished deletion from the initial accepted request.

```bash
localcloud ec2 terminate-instances --name web
localcloud ec2 describe-instances --name web
localcloud resources
```

A service account must keep its required permissions during recovery. Restore API access and restart or issue another describe/resources request to retry pending work.

## Minimal-role and actual crash verification

The new live fixture uses the generated namespace-scoped executor Role through a short-lived service-account token and the direct Kubernetes HTTPS API. It does not route provider requests through an administrator's proxy. A Secrets request is explicitly denied with HTTP 403.

Namespace/RBAC setup and file-byte inspection use the fixture's administrative kubectl context; those are test setup/inspection actions, not provider API calls.

**13 live checks passed**, covering:

- Minimum service-account access and denied Secrets access.
- Process death immediately after a successful PVC POST, before the adapter receives its UID.
- Process death immediately after a successful Deployment POST during a launch with a real PVC.
- Recovery of uncommitted resources while preserving a foreign sentinel object.
- Real Ready guest, data bytes, logs and committed UID preservation across restart.
- Stop/start with retained PVC bytes.
- Service, ConfigMap and Deployment repair, and replica drift correction.
- Preservation of a foreign same-name Service replacement.
- Missing-data detection without silently creating empty storage.
- Completed journaled termination and fixture cleanup.

The final regression passed the existing **12-check lifecycle/metadata fixture** and the new **13-check minimal-role/recovery fixture**, for **25 live checks**. Final lifecycle evidence: `ev_ab7e87522947fbd81630912e6dbb850f45cd51f7be7cc85b90873cf476ab0cf5`; [report](research/rea/ec2-kube-completion-basic-report.json). Final recovery evidence: `ev_964c550cb54972367ec96cfa3604f443906ad26396f2bfab931c22a4f5257f6a`; [report](research/rea/ec2-kube-completion-recovery-report.json). A preliminary regression fixture incorrectly waited for termination after stop; its failure is retained separately in the main evidence bundle and was corrected before the final lifecycle run.

The full unit suite passed **271 tests**, including journal commit/rollback, unknown response outcomes, UID/nonce conflicts, unavailable APIs, concurrent update protection, state/rendering and reconciliation cases.

## Evidence and reproduction

Normal-license reference audit:

```bash
python3 scripts/ec2-kube-completion-reference.py
```

Actual LocalCloud minimal-role/crash/reconciliation fixture:

```bash
python3 scripts/ec2-kube-recovery-live.py
python3 -m unittest discover -s tests -q
```

The scripts require the existing `kind-localcloud` context and cached images described in the earlier guides. Reference credentials are read from the existing private token file. The fixtures use unique namespaces, temporary state and unique image aliases; they preserve user workloads, CRDs, images and CLI profiles.

- Reference: `ev_81f3af12f5d39ec31d0320241666d3815dfda6c7e2b09fab4e5dc9021c3de5dc` (lifecycle) and `ev_be71fe5f243febb13c97359bee796edae7b4fdc000aa2c60a0343079b6df7f7a` (corrected metadata); [reference results](research/rea/ec2-kube-completion-reference-report.json), [index](research/rea/ec2-kube-completion-reference-index.json), [full bundle](research/rea/ec2-kube-completion-reference-evidence.json.gz).
- LocalCloud direct-role/crash/drift fixture: `ev_ab2accb704e5bb5c9f165b4bf604eaa8b01e5aa1e7fc1c1d168612130620734e`; [report](research/rea/ec2-kube-recovery-report.json), [index](research/rea/ec2-kube-recovery-index.json), [full bundle](research/rea/ec2-kube-recovery-evidence.json.gz).
- Unit suite: `ev_771cf4921343f9267ed29ede66378e465e6ca9fca2fbf45277395bd375fb0398`.
- [Current source/evidence index](research/rea/ec2-kube-completion-index.json) and [full main-session evidence](research/rea/ec2-kube-completion-evidence.json.gz).

The fixture cleanup verified no owned namespace, persistent volumes or temporary host/node image tags remained. Preliminary reference fixture failures remain separate from the corrected observations; record counts are not parity percentages.

## Boundaries after this pass

The tested provisioning-crash recovery, role authorization and resource reconciliation gaps are implemented and verified. This is not a claim of arbitrary distributed atomicity or full LocalStack EC2 Kubernetes equivalence.

- Host power loss, permanently unavailable APIs and conflicting administrator changes are not automatically solved; recovery stays pending when safety checks fail.
- A missing data PVC is a data-loss condition that needs an explicit recovery decision. Recreating its metadata cannot restore its bytes.
- CSI snapshots/copies/restores, raw disks, real volume resizing, encryption and IAM/STS credential delivery remain separate capabilities.
- The current metadata sidecar starts after user-data initialization; user data cannot rely on that Pod-local endpoint during bootstrap. The endpoint is localhost port 1338 rather than privileged link-local rewriting.
- EKS, VM/Windows execution and full AWS networking are outside this executor's contract.

Earlier evidence indexes remain historical checkpoints. Their source hashes and counts describe the versions captured then; the completion index binds this pass’s current source and fixtures.

## Later bootstrap metadata follow-up

The metadata startup limitation recorded above is superseded by the [26 October bootstrap metadata audit](REA_EC2_KUBERNETES_BOOTSTRAP.md). With a helper configured, LocalCloud starts a native restartable metadata init sidecar, waits for its startup probe, then runs user data with metadata endpoint variables. The observations and counts above remain the historical recovery checkpoint.
