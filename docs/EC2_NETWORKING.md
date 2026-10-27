# EC2 networking, addresses and interfaces: REA findings and usage

Current checkpoint: [EC2 release and verification](EC2_RELEASE.md). The later
checkpoint expands the operation set described below to 97 selected AWS operations.

20 October 2026. This follows [the EC2 infrastructure milestone](EC2_INFRASTRUCTURE.md).
The provider now exposes **74 selected AWS operations plus NameInstance**.
Four operations were added: ModifyNetworkInterfaceAttribute,
DescribeNetworkInterfaceAttribute, AssignPrivateIpAddresses and
UnassignPrivateIpAddresses. Existing attachment, address association, filters,
IP reservation and dashboard operations were expanded.

## What the reference actually does

The reference is normally licensed LocalStack 2026.10.0, build 738d58ee, image
`localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`.
The existing token file was read internally. No token is included in the reports;
no encrypted provider source or entitlement bypass was used.

API records and actual packet behavior are different things:

| Area | Observed LocalStack behavior in these probes | LocalCloud behavior |
| --- | --- | --- |
| VPC/subnet/routes | CRUD and configuration records are available. | Existing CRUD plus an owned Docker bridge for explicit-subnet launches; it is not AWS routing. |
| Security groups | Guest HTTP8080 remained reachable directly through its Docker bridge IP with no ingress, after allow, and after revoke. Docker networks, ports and guest addresses stayed unchanged. | Rules persist. TCP ingress can supply launch-time localhost publications. Later changes do not install a live firewall. |
| Secondary ENI | Attach succeeded, but no additional Docker network, guest interface or guest address appeared. | Attach/detach is supported as control-plane state in mock and Docker modes. It does not add a real guest NIC. |
| Private IPs | API assignment/removal changes the interface record. | Explicit/count-based secondary assignment, removal, reservation, transfer and attached-instance views are supported. They do not configure guest addresses. |
| Elastic IP | A loopback address was allocated. Association left guest/Docker networking unchanged. HTTP8080 through the address refused; SSH22 succeeded through an existing wildcard port binding. | Loopback address records and ENI/private-IP associations are supported. Existing chosen localhost publications remain the way to reach an app. EIP association does not configure NAT or forward ports. |

The runtime probe used one Linux Docker guest and TCP8080/SSH22. It does not
establish behavior for every protocol, executor, network backend or future
LocalStack release. We did not install host routes or firewall rules. Reference
fixtures were removed; a preexisting shared link-local Docker network was preserved.

## Start and connect

Follow [the server/image/token setup](EC2_INFRASTRUCTURE.md#start-from-a-stopped-server).
Keep the server running. Use the same database and token on restart. For a quick
API-only exercise, a normal mock server is enough:

```bash
localcloud start --port 4566 --data .localcloud/networking.sqlite3
```

In a separate terminal:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud status
```

If the server has a management token, authenticate using that server's saved
token as described in the setup guide. Docker mode requires authentication and
an explicit image allowlist. You can use the same operations below in either mode.

## Create a network and interface

Commands return JSON. Copy the returned IDs into subsequent commands wherever
`*-REPLACE` appears. These are resource IDs; an interface name is not an instance
selector. Describe commands recover forgotten IDs.

```bash
localcloud ec2 create-vpc --cidr-block 10.86.0.0/16
localcloud ec2 create-subnet --vpc-id vpc-REPLACE \
  --cidr-block 10.86.1.0/24 --availability-zone us-east-1a
localcloud ec2 create-security-group --vpc-id vpc-REPLACE \
  --group-name app --description 'Application networking'
localcloud ec2 create-network-interface --subnet-id subnet-REPLACE \
  --private-ip-address 10.86.1.10 --groups sg-REPLACE \
  --description 'Application secondary interface' --client-token app-interface-1
localcloud ec2 describe-network-interfaces
```

Omitting `--groups` selects the subnet VPC's default security group. ClientToken
retries return the same interface; a changed request with the same token fails.
Groups must belong to the same VPC. Reserved, out-of-subnet and already allocated
IPs are rejected. Existing primary **and secondary** IPs reserve their addresses.

## Assign private IPs and change attributes

```bash
localcloud ec2 assign-private-ip-addresses --network-interface-id eni-REPLACE \
  --private-ip-addresses 10.86.1.11 10.86.1.12
localcloud ec2 assign-private-ip-addresses --network-interface-id eni-REPLACE \
  --secondary-private-ip-address-count 2
localcloud ec2 describe-network-interfaces --network-interface-ids eni-REPLACE
localcloud ec2 modify-network-interface-attribute --network-interface-id eni-REPLACE \
  --description '{"Value":"Updated description"}'
localcloud ec2 modify-network-interface-attribute --network-interface-id eni-REPLACE \
  --source-dest-check '{"Value":false}'
localcloud ec2 describe-network-interface-attribute --network-interface-id eni-REPLACE \
  --attribute sourceDestCheck
localcloud ec2 modify-network-interface-attribute --network-interface-id eni-REPLACE \
  --groups sg-REPLACE
localcloud ec2 unassign-private-ip-addresses --network-interface-id eni-REPLACE \
  --private-ip-addresses 10.86.1.12
```

Modify one attribute per call. `SourceDestCheck` is stored configuration; it is
not a packet-filter switch. Secondary IP transfers use `--allow-reassignment`;
primary addresses cannot be transferred. Local limits are 30 secondary IPs and
five security groups per modification. Multi-address failures roll back the
whole state mutation.

## Attach to an instance

Create an instance as shown in the infrastructure guide, in the same VPC and
availability zone. Mock mode uses `ami-local`; Docker mode uses your allowed AMI.
Then:

```bash
localcloud ec2 attach-network-interface --network-interface-id eni-REPLACE \
  --instance-id i-REPLACE --device-index 1
localcloud ec2 describe-network-interfaces \
  --filters '[{"Name":"attachment.instance-id","Values":["i-REPLACE"]}]'
localcloud ec2 describe-instances --instance-ids i-REPLACE
```

Device indices 1–7 must be unused. The primary interface at index 0 cannot be
detached. The attached-interface view appears in DescribeInstances. To request
secondary-interface deletion on instance termination:

```bash
localcloud ec2 modify-network-interface-attribute --network-interface-id eni-REPLACE \
  --attachment '{"AttachmentId":"eni-attach-REPLACE","DeleteOnTermination":true}'
```

This attachment changes emulated state, not the container's actual networks.

## Allocate and associate an elastic IP

```bash
localcloud ec2 allocate-address --domain vpc
localcloud ec2 associate-address --allocation-id eipalloc-REPLACE \
  --network-interface-id eni-REPLACE --private-ip-address 10.86.1.11
localcloud ec2 describe-addresses
localcloud ec2 describe-network-interfaces --network-interface-ids eni-REPLACE
localcloud ec2 describe-addresses \
  --filters '[{"Name":"network-interface-id","Values":["eni-REPLACE"]}]'
```

Use either `--network-interface-id` or `--instance-id`. The instance selector
chooses its primary interface. The requested private IP must actually belong to
the interface. Omitting it selects the primary IP. Association details are kept
consistent between DescribeAddresses, interface private-IP records and instance
views. A secondary-interface association does not replace the instance's primary
public-IP field.

New allocations use the bounded loopback pool of 4,096 addresses under `127.240.0.0/16`; older database
allocations retain their original addresses. These addresses are identifiers,
not newly bound host listeners. Use the instance's published `Endpoints` for
application access. LocalCloud keeps loopback-only publishing; it does not copy
the reference's wildcard SSH exposure.

Repeating an existing association returns its ID. Reassociation is allowed by
default; pass `--no-allow-reassociation` to reject moving an already associated
allocation. Disassociate before removing its private IP or deleting its ENI.

```bash
localcloud ec2 disassociate-address --association-id eipassoc-REPLACE
localcloud ec2 release-address --allocation-id eipalloc-REPLACE
localcloud ec2 detach-network-interface --attachment-id eni-attach-REPLACE
localcloud ec2 delete-network-interface --network-interface-id eni-REPLACE
```

Deleting a primary interface requires terminating its instance first. Secondary
interfaces survive termination unless DeleteOnTermination is true. EIP allocations
survive termination, with association fields cleared.

## Dashboard

Connect to the running server and open EC2 → EC2 infrastructure. Resource type
now includes **Network interfaces** and **Elastic IPs**. Create/list/details/delete
are available; attached secondary interfaces have a Detach action and associated
addresses have Disassociate. The network-action form supports attachment,
secondary-IP assignment/removal, EIP association, description and source/destination
check changes. It explicitly identifies these as emulated network configuration.

## Verification and deliberate differences

The [main evidence index](research/rea/ec2-network-index.json) links the
[main REA archive](research/rea/ec2-network-evidence.json.gz). The separately
captured Docker reference and its cleanup are retained in the
[runtime index](research/rea/ec2-network-runtime-index.json) and
[runtime archive](research/rea/ec2-network-runtime-evidence.json.gz). Earlier
failed captures and broad session unknowns remain in the archive; selected
record IDs identify the relevant observations.

- [API reference](research/rea/ec2-network-reference.json), REA
  `ev_0e41504cfaf62cce09f6aa4d5c06985181996758a1c1a1d50f3a07a83e800413`.
- [LocalCloud Query replay](research/rea/ec2-network-localcloud.json), REA
  `ev_7b253ef144ee3366855628fca956d95a18d3b7a3e59c8b5001f38d7b656bf66a`.
  Both have 33 non-cleanup cases, with 30 matching HTTP statuses.
  The [bounded comparison](research/rea/ec2-network-comparison.json) also compares
  nine semantic projections: six match, and three reflect the documented
  secondary-IP association and primary-removal differences.
- [Actual LocalStack Docker networking](research/rea/ec2-network-runtime-reference.json),
  REA `ev_16864817f2bcd8fee5bdd9452a0cb92358c56e8a8e2bad6eb2da67d00d7ec264`.
- [LocalCloud Docker check](research/rea/ec2-network-docker-live.json), REA
  `ev_00bdccd6ea42c2f1b96d7eaed4c20832a35073ce54563c6bba7d0c1d5ca29ff5`.
- [Browser check](research/rea/ec2-network-browser.json): real creation, allocation
  and attribute modification; backend independently confirmed results. All
  browser actions completed and no JavaScript page errors were recorded.

LocalStack accepted duplicate private IPs, primary-address removal and deletion
of an attached ENI in this probe. LocalCloud deliberately rejects all three.
The reference also ignored the requested secondary private IP during EIP
association, reported an empty instance ID and put the association on the whole
interface. LocalCloud retains the requested secondary IP and its owning instance
and places the association on that address. These are documented response and
validation differences, not exact equality.

The final audit passed **162 tests and 11 live regression checks**. The rebuilt
server image also exercised interface creation and secondary-IP allocation as
its normal nonroot user.

Tests cover rollback, reservation/reuse, secondary transfer, primary protection,
attributes, attachment lifecycles, association consistency, portability and Query/
CLI encoding. Reproduce with:

```bash
python3 -m unittest discover -s tests -q
python3 scripts/ec2-network-reference.py
python3 scripts/ec2-network-candidate.py
python3 scripts/ec2-network-docker-smoke.py
```

The reference commands need Docker, the pinned local image and the existing
licensed token file. The Docker smoke test needs `localcloud-ec2-web:dev` from the
infrastructure guide. All use isolated state. Full AWS networking, packet-level
security-group enforcement, actual additional guest NICs and EIP NAT remain
outside this implementation; they were not demonstrated by the tested reference.

## Later packet-enforcement implementation

The [27 October packet networking guide](EC2_PACKET_NETWORKING.md) supersedes the LocalCloud metadata-only filtering/forwarding limits above for newly launched Docker instances using `--ec2-networking enforce`. The historical LocalStack observations remain bounded reference evidence. The new implementation is an independently tested local extension, not a claim that the reference performed the same filtering. See [the follow-up audit](REA_EC2_PACKET_NETWORKING.md).
