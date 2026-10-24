# EC2 storage: observed behavior and LocalCloud differences

Investigation date: 23 October 2026. This report covers a bounded, normally licensed runtime comparison, not full EC2 compatibility.

## Reference and method

The reference is LocalStack 2026.10.0, pinned to:

```
localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872
```

The probe used `EC2_VM_MANAGER=docker`, the cached Ubuntu guest `ami-61ad6e59d7b0`, two API volumes, and one snapshot. It inspected only its own guest. It did not read encrypted provider code or change host block devices, filesystems, firewall rules, or routes. Credentials came from the existing token file and were not retained in the report.

The earlier [storage API probe](research/rea/ec2-storage-reference.json) used the mock executor. It establishes API behavior only. This investigation adds actual Docker and guest observations.

## Results

| Question | Observed LocalStack Docker behavior | Meaning |
|---|---|---|
| Can a volume be created? | `CreateVolume` returned HTTP 200 and `creating` | The control-plane record exists |
| Can encryption be requested? | HTTP 200, `Encrypted: true`, generated KMS ARN | At-rest encryption was **not established** |
| Can a running guest attach `/dev/sdf`? | HTTP 200, `attaching`; guest container ID, Docker mounts and guest mount/device output unchanged | This call did not introduce a usable disk in the tested guest |
| Does attaching while stopped change that? | HTTP 200; after start, same container and Docker mounts | No new Docker volume or bind mount was observed |
| Does stop/start preserve the root filesystem? | A marker in `/tmp` survived | The tested guest retained its writable container root across stop/start |
| Can a volume grow? | `ModifyVolume` returned HTTP 200 and target size 2 GiB | Guest disk growth and quota enforcement were not established |
| Can a snapshot be created? | Create returned `pending`, 60%; describe returned `completed`, 100% | Snapshot API state works; byte restoration was not tested on this reference |
| Does detach change guest storage? | HTTP 200; same Docker mounts | No runtime storage change was observed |

The baseline guest had a Docker socket bind mount and no named EBS volume mount. Host-device names visible through `lsblk` are not evidence that an emulated EBS device was attached. These observations apply to this image, configuration, guest and call sequence; they do not prove that every executor or launch-time mapping behaves the same way.

## How LocalCloud currently works

LocalCloud has real **file-backed** data volumes implemented with owned Docker named volumes. A filesystem mount is different from a raw block device.

1. `CreateVolume` creates an owned named volume and initializes its filesystem permissions using the configured helper image.
2. A launch mapping such as `/data` mounts that volume inside the application container. A mapping called `/dev/sdf` is exposed at `/mnt/ebs/sdf`; it does not create a block device.
3. Stop the instance before changing attachments. LocalCloud recreates the stopped container with the new mount list.
4. `CreateSnapshot` pauses attached running Docker guests, copies files into a separate owned snapshot volume, and resumes only guests it paused. Existing caller pauses are preserved. Flush application buffers first for application consistency.
5. Creating a volume from that snapshot copies its files into a new named volume.
6. Termination handles `DeleteOnTermination`; otherwise delete retained volumes explicitly.

The live LocalCloud smoke test wrote `persistent-volume` into `/data/marker`, stopped the guest, took a snapshot, restored a different volume, detached the original, attached the restored one, restarted the guest, and read the original bytes successfully. It also checked the web endpoint and IMDS v1/v2. All four checks and cleanup passed.

These useful data operations are an intentional LocalCloud implementation choice. They are **not an exact reconstruction** of the reference's observed metadata-only attachment behavior. LocalCloud also rejects encrypted Docker volumes rather than treating an encryption flag as proof of encryption. Volume size/type/IOPS metadata does not impose equivalent AWS storage performance or capacity limits.

## Reproduce

Run from the repository directory. The reference probe requires Docker, the pinned LocalStack image, the cached Ubuntu guest, and a valid normal LocalStack token at `~/.config/localstack/rea-auth-token`.

```bash
python3 scripts/ec2-storage-runtime-reference.py
```

For the LocalCloud live check, build or provide the existing `localcloud:ec2-runtime` helper and `localcloud-ec2-web:dev` demo images, then run:

```bash
python3 scripts/ec2-docker-smoke.py
```

The scripts create isolated instances and attempt cleanup in `finally` blocks. The reference script preserves the preexisting shared LocalStack link-local network. They do not need the user's LocalCloud server to run and do not change the saved CLI endpoint or token.

## Evidence

- Reference runtime: `ev_d656ac610804dc1d9bdd293648d8fc527f9154c25ef17875bea86f61ab7c0f09`.
- Retained artifact comparison and digest: `ev_03f2f0c12d5cb2a8d05b3394a497ec6d45bccf83e92bd9682c0fd2a5bbf93d81`.
- LocalCloud live smoke: `ev_b5a976c166053c064137bd3fb1843257837e8ab8aff5a408a0713b4010428eb8`.
- [Reference results](research/rea/ec2-storage-runtime-reference.json), [evidence index](research/rea/ec2-storage-runtime-index.json), and [full REA bundle](research/rea/ec2-storage-runtime-evidence.json.gz).

The reference guest and controller were removed; the captured guest-container check was empty. REA observes terminal output and sampled processes; it does not provide syscall or packet tracing here. The retained comparison binds the result file by SHA-256.

## Follow-up: storage gap verification and fixes

The follow-up used the same pinned reference. These results supplement the first probe rather than replacing its narrower evidence.

| Former gap | New reference observation | LocalCloud result |
| --- | --- | --- |
| Root/data mappings at launch | Custom root and data mappings create attached API records; no guest data disks or mounts appeared | Filesystem mappings remain real named-volume mounts. A `/dev/sda1` mapping is a directory at `/mnt/ebs/sda1`, not the container boot filesystem |
| Root snapshot restoration | A confirmed, synced root marker was absent in a new guest launched with the root snapshot | Do not treat EBS root snapshot metadata as a Docker boot-root backup. Guest AMI root commits are a separate feature |
| Delete-on-termination | `true` volumes disappeared from the API; `false` remained available with no attachments | Live checks verified both policies, including actual retained volume files |
| Restart persistence | With `PERSISTENCE=1`, volume/snapshot/encryption records survived graceful reference restart | SQLite records, runtime ownership, running guest and restored file bytes survived LocalCloud backend restart |
| Runtime after reference restart | No matching guest container remained, although DescribeInstances reported running; reference logged IMDS initialization failure | LocalCloud's live test verified both the actual guest and bytes, not just a running record |
| Snapshot copy isolation | Root marker was not restored in the reference root-snapshot launch | LocalCloud restored a 256 KiB binary file and a relative symlink through snapshot → copied snapshot → new volume; later source writes and deleting snapshots did not change restored bytes |
| Abrupt backend termination | Reference restart experiment was graceful only | An acknowledged volume creation survived SIGKILL; a second test killed the backend mid-copy and recovered the helper, paused guest and uncommitted destination on restart |
| Failed copy / launch | Reference API success does not establish rollback safety | Ownership- and operation-labeled helpers are cleaned up before destination removal; existing/foreign volumes are preserved; failed launch/batch cleanup attempts every item without replacing the original error |
| Snapshot during guest writes | Reference snapshot byte consistency was not demonstrated | Attached running guests are paused during a file copy and resumed in `finally`; preexisting pauses are preserved; pause/copy/resume failures have focused tests |

The snapshot quiescing and stronger helper cleanup are LocalCloud design choices, not a claim that proprietary implementation code was recovered. A resume failure fails the snapshot request and attempts to remove its newly created copy. The original error is preserved if cleanup also fails. Failed requests do not commit resource or idempotency records to SQLite. A separate durable SQLite journal records Docker copy and pause intent before external effects. Startup and settled-transaction recovery use committed resource references to retain successful volumes and remove uncommitted copies. Recovery checks ownership, per-operation nonce and guest container identity; it does not sweep unrelated resources.

The new live test passed **nine checks** and left zero volume/snapshot records, zero owned Docker volumes/helpers and zero pending journal entries. The full unit suite passed **212 tests**. Focused failure tests cover daemon timeouts, cleanup failure, destination ownership races, permission mismatches, pause failure, copy failure, resume failure and launch rollback.

### Reproduce the follow-up

Normal-license reference probes:

```bash
python3 scripts/ec2-storage-edges-reference.py
python3 scripts/ec2-storage-persistence-reference.py
```

LocalCloud file-integrity, restart and deletion checks:

```bash
python3 scripts/ec2-storage-integrity.py
python3 -m unittest discover -s tests -q
```

The live test uses the same cached demo/helper images as the earlier smoke test. It starts isolated temporary backends, never uses the saved CLI endpoint/token, and does not need the user's server to be running.

### Additional evidence

- Root/data/deletion reference: `ev_4d7067b026e102f121bbb2bad369ae9fc63bfdccdb6f91a49fd4cb6308e98a9c`; [report](research/rea/ec2-storage-edges-reference.json), [index](research/rea/ec2-storage-edges-index.json), [full bundle](research/rea/ec2-storage-edges-evidence.json.gz).
- Persistence reference: `ev_4ee767a799e97dda7fab2158fc70efd4517378195146ffb1992bb8b4b0e04521`; [Docker report](research/rea/ec2-storage-persistence-docker.json), [mock report](research/rea/ec2-storage-persistence-mock.json), [index](research/rea/ec2-storage-persistence-index.json), [full bundle](research/rea/ec2-storage-persistence-evidence.json.gz).
- LocalCloud final nine-check live probe: `ev_2856a7c775f6a558f94b1986da70a163f2ee6ec2d96a9cdef493b848cb23336f`; [result](research/rea/ec2-storage-completion-localcloud.json), [completion index](research/rea/ec2-storage-completion-index.json), [full bundle](research/rea/ec2-storage-completion-evidence.json.gz).

### Operating recovery

Restart with the **same** `--data` SQLite file after an interrupted operation.
Recovery runs on backend startup and after a state transaction settles; it needs
Docker access but does not require a helper image just to remove a leftover
helper or resume a guest. Use `localcloud resources` and inspect
`storageRecovery`: `pending`, `recovered`, `errors`, and `durable` describe the
last recovery attempt. A pending/error result means recovery has not completed.
Restore Docker access and restart the backend to retry. Keep the state file so
its ownership and recovery records remain available.

A journal entry does not authorize deleting a foreign or replaced resource.
Ownership/identity mismatches remain pending for investigation. Jobs belonging
to another live backend are skipped rather than interrupted.

## Later follow-up

See the [24 October storage follow-up](REA_EC2_STORAGE_FOLLOWUP.md) for EBS default flags, complete modification history, SDK-compatible tag copying and one pause window across multi-volume batches. The counts and evidence above describe the earlier checkpoint.

## Boundaries that remain

This completes the listed **file-backed storage checks and recoverable-failure fixes**, not a raw EBS implementation or full LocalStack parity.

- Raw block devices, real filesystem growth, capacity/IOPS quotas and at-rest encryption remain unsupported in LocalCloud's Docker backend. The reference's API flags and sizes do not establish these data-plane guarantees either. LocalCloud continues to reject encrypted Docker volume requests explicitly.
- Guest root backups use the existing guest AMI workflow; a filesystem mapping called `/dev/sda1` does not replace the image root.
- Pausing a guest gives a stable file tree relative to its writers, not an application transaction or a single atomic snapshot across volumes. Host/external writers are outside that boundary.
- SIGKILL recovery was tested during an intentionally delayed copy and after an acknowledged completed write. This is process-crash recovery, not a guarantee against host power loss, broken storage media or an unavailable Docker daemon. A paused guest resumes when a backend opens the same persistent database and recovery can reach Docker. File-backed databases provide a durable journal; `:memory:` databases cannot survive process loss.
- Cleanup is best-effort when the Docker daemon is unavailable; failure tests prove error preservation and attempted cleanup, not cleanup during permanent daemon loss.
- Kubernetes storage and lifecycle are a separate backend; see [executor findings](REA_EC2_EXECUTORS.md).
