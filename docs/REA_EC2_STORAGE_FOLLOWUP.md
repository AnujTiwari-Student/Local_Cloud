# EC2 storage follow-up: defaults, modification history and batch copies

Investigation date: 24 October 2026. This extends [the earlier storage work](REA_EC2_STORAGE.md). It compares normally licensed LocalStack 2026.10.0 API behavior and adds independently implemented LocalCloud functionality. It does not establish full EC2 parity.

Reference image:

```
localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872
```

## What the new reference probes establish

| Question | Observed result | Scope |
| --- | --- | --- |
| Get / enable / disable EBS encryption by default | HTTP 200, with a mutable Boolean flag | Control-plane setting |
| Does enabling the default encrypt a new implicit volume? | The new volume still reported `Encrypted: false` | No implicit encryption behavior established |
| Default KMS key lookup/reset | HTTP 501 | Not implemented in the tested reference/configuration |
| Modify default KMS key | HTTP 500 with both a fake key and a real key created through the same local KMS service | Not a usable path in this fixture |
| Explicit volume key with `Encrypted: false` | HTTP 400 `InvalidParameterDependency` | API validation |
| Restore encrypted snapshot with `Encrypted: false` | HTTP 200, still `Encrypted: true` | Metadata remains encrypted; actual cryptography not established |
| Resize / type / performance validation | Reference accepted shrink, IOPS 1, throughput 1 and an invalid type string; size 0 failed | Permissive mock metadata handling |
| Describe volume modifications | Returned every successful modification, with original/target fields and completed timestamps | History rather than only the latest change |
| `CopyTagsFromSource` SDK type | String option `"volume"` | The former LocalCloud Boolean handling was incompatible with the SDK |
| Source tag copying | Reference accepted `"volume"`, but did not copy verified source-volume tags | Accepted input is not proof of implemented tag copying |
| Explicit snapshot tags and boot exclusion | Both worked | Supported batch API behavior |
| `ExcludeDataVolumeIds` / invalid tag-copy string | Accepted but ignored by the reference | These calls do not establish working exclusion or validation |

Encryption and resize probes used the mock executor. They extend the earlier real Docker observations, where root/data API mappings did not introduce guest disks or mounts and root snapshot launch did not restore a confirmed root marker. Neither a KMS ARN nor an encryption flag demonstrates data-plane encryption.

## LocalCloud changes

### Encryption-default flags

Three operations now work through the management API, EC2 Query XML, AWS SDK and `localcloud` CLI:

```bash
localcloud ec2 get-ebs-encryption-by-default
localcloud ec2 enable-ebs-encryption-by-default
localcloud ec2 disable-ebs-encryption-by-default
```

They store an emulated Boolean setting, matching the observed reference behavior. The flag survives a backend restart and portable state export/import. IAM enforcement applies to mutations. This setting does not encrypt Docker files or change implicit `CreateVolume` encryption metadata. CLI help explains that boundary. Encrypted Docker volumes continue to fail explicitly because this backend does not provide at-rest encryption.

The wire model now contains **100 selected AWS operations**, plus LocalCloud's local `NameInstance` extension. The three new operation shapes came from the reference image's public Botocore installation; those shapes establish serialization, not provider behavior.

### Complete volume-modification history

Each successful modification now retains a completed history entry. Existing latest-only records remain readable and are migrated into the history on the next change. A failed request does not add a history entry or change the resource. History survives restart and portable state transfer.

```bash
localcloud ec2 modify-volume --volume-id vol-REPLACE --size 2
localcloud ec2 modify-volume --volume-id vol-REPLACE --volume-type gp3
localcloud ec2 modify-volume --volume-id vol-REPLACE --iops 4000 --throughput 250
localcloud ec2 describe-volumes-modifications --volume-ids vol-REPLACE
```

The response includes the successful changes rather than just the last one. Entries contain original and target properties for fields changed, with completion timestamps. A transition to gp3 supplies the supported 3000 IOPS / 125 throughput defaults when not specified. Read responses support the existing pagination parameters. The local history limit is 1000 entries per volume; reaching it fails explicitly.

LocalCloud retains stricter supported-size/type/performance validation than the permissive reference probe. Changing size, IOPS or throughput metadata does not resize a guest filesystem, enforce a disk quota or impose equivalent AWS performance.

### SDK-compatible snapshot tags

Use the SDK string option:

```bash
localcloud ec2 create-snapshots \
  --instance-specification '{"InstanceId":"i-REPLACE","ExcludeBootVolume":true}' \
  --copy-tags-from-source volume \
  --tag-specifications '[{"ResourceType":"snapshot","Tags":[{"Key":"Purpose","Value":"fixture"}]}]'
```

LocalCloud copies source-volume tags and applies explicit snapshot tags afterward, so explicit values win. This useful tag-copy behavior intentionally exceeds the tested reference, which accepted the option but ignored the source tags. Invalid tag-copy strings fail explicitly. Data-volume exclusion remains unsupported rather than silently ignored.

### One pause window across a multi-volume batch

Previously, each volume copy independently paused and resumed the guest. Application writes could occur between copies. `CreateSnapshots` now holds one guest pause across the entire selected batch:

1. Record a durable parent quiesce operation and verify guest ownership/identity.
2. Pause a running guest, preserving any preexisting caller pause.
3. Create each selected snapshot under its own journal entry and operation nonce.
4. Resume only guests paused by the batch after all copies finish.
5. Commit the snapshot records and settle recovery entries.

If copying or resuming fails, compensation attempts every created snapshot while preserving the original error. If the backend dies mid-batch, recovery settles child helpers/copies before releasing the parent pause. A child that cannot be safely recovered keeps its parent pending. Recovery does not remove foreign resources or resume a replaced guest.

This is a stable file-copy window relative to writers inside the guest. Flush application buffers before snapshotting for application consistency. External/host writers are outside the pause boundary, and pausing does not create a transactional filesystem or an application-level atomic commit across volumes. The tested reference does not establish such a data-plane batch guarantee; this is a LocalCloud implementation choice.

## Verification and evidence

New normal-license reference probes:

```bash
python3 scripts/ec2-storage-encryption-reference.py
python3 scripts/ec2-storage-batch-reference.py
```

LocalCloud SDK/CLI and real Docker checks:

```bash
python3 scripts/ec2-storage-wire-check.py
python3 scripts/ec2-storage-batch-live.py
python3 -m unittest discover -s tests -q
```

The Docker checks use the existing cached `localcloud-ec2-web:dev` and `localcloud:ec2-runtime` images and a temporary SQLite database. They do not change saved CLI profiles or user server state.

- Encryption / KMS / resizing reference: `ev_7d1ac237d54c3ee34ee2771780a60cd3f9efcd469eb9d088836887d1886a660b`; [report](research/rea/ec2-storage-encryption-reference.json), [index](research/rea/ec2-storage-encryption-index.json), [full bundle](research/rea/ec2-storage-encryption-evidence.json.gz).
- Batch API reference: `ev_c3039b9f2464728b7f924f1de903e7050907daede8ef0067926349bff94fcc6a`; [report](research/rea/ec2-storage-batch-reference.json), [index](research/rea/ec2-storage-batch-index.json), [full bundle](research/rea/ec2-storage-batch-evidence.json.gz).
- Public SDK shape extraction: `ev_d609225560565d4f8f6512b67d2d28d78094748af30a295763f44b7d1c731420`.
- LocalCloud SDK/CLI: `ev_c1162bdb20b2a16c0667aaa47ea951758fba9b2e915e3cd8ad1c8ca90ff1783b`; [result](research/rea/ec2-storage-followup-wire-localcloud.json).
- Real Docker batch and interrupted-copy recovery: `ev_3c83c780c7a727b77537d672d941c45e6b56d108fdf05d51939e7660266236ac`; [result](research/rea/ec2-storage-followup-batch-localcloud.json).
- [LocalCloud results and source hashes](research/rea/ec2-storage-followup-index.json), [full REA bundle](research/rea/ec2-storage-followup-evidence.json.gz).

**Validation:** 226 unit tests passed, plus five SDK/CLI checks and five real Docker batch/recovery checks. Unit evidence: `ev_4ffc460d7e1e3971b6320529fee100da6e97816702a6a0df6a5f1661bd47ee3c`.

The live batch test verifies actual bytes from both snapshot volumes, exactly one pause/copy/copy/resume sequence, preserved caller pauses, recovery after killing a backend during a child copy, preserved committed snapshots and zero remaining owned helper/volume resources or pending operations.

## Remaining storage boundaries

Raw EBS devices, bootable root-volume restoration through an EBS snapshot, real filesystem resizing, enforced capacity/IOPS quotas and at-rest encryption remain unsupported in LocalCloud. The new reference probes do not establish those capabilities in the tested LocalStack executor either. Guest root AMI commits remain a separate workflow. Application transactions, host power-loss guarantees, arbitrary external writers and Kubernetes storage require separate implementations and evidence.

Earlier evidence indexes remain historical checkpoints: their recorded source hashes and operation/test counts describe the source at capture time. Use this follow-up index for the current changes; a registered operation name is not a claim of its complete AWS behavior.
