# REA: security-group packets, routes and Elastic IP forwarding

27 October 2026. This follow-up implements opt-in Docker packet enforcement in LocalCloud. The [usage guide](EC2_PACKET_NETWORKING.md) covers setup and CLI examples. It does not claim that LocalStack's reference implementation performs the same packet filtering.

## Step 1 — establish what the reference does

The normally licensed reference is LocalStack **2026.10.0**, build `738d58ee`, image:

```
localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872
```

The fixture reads the existing local license file privately and creates isolated guest/reference containers, API resources and addresses. It does not extract protected provider code or bypass entitlements. It reuses the earlier [networking observations](EC2_NETWORKING.md), then tests two guests and additional rule/route transitions.

Final reference evidence: `ev_607242228e75f531c7ccbb1259267d38583632f7ba4731bd32c3960a414fffa0`. See [report](research/rea/ec2-packet-reference-report.json), [index](research/rea/ec2-packet-reference-index.json) and [evidence bundle](research/rea/ec2-packet-reference-evidence.json.gz).

| Question | Observed result in this reference fixture |
| --- | --- |
| Does closed ingress block peer HTTP? | No. Peer HTTP returned 200 before ingress was allowed. |
| Does allow/revoke change peer reachability? | Peer HTTP remained 200 after both changes. |
| Does removing default egress block peer HTTP? | Peer HTTP remained 200. |
| Does adding/deleting an IGW/default-route record change the guests' Linux route tables? | Saved route tables remained unchanged across the tested transitions. Local peer HTTP continued working. |
| Does associating/reassociating a loopback EIP forward the custom application port? | That EIP/port refused connections for both target guests; direct Docker-address HTTP remained reachable. |

The route result is intentionally bounded: local VPC peer traffic would not normally need an IGW even on AWS, so peer reachability alone does not establish that all external routing is absent. The unchanged guest route tables and custom-port forwarding probes answer their specific questions; they are not a universal negative for other LocalStack backends, ports or releases.

All owned reference and guest containers and API resources were cleaned. No direct host firewall/route commands were used. Ordinary Docker operations still involve Docker's normal bridge/NAT management.

## Step 2 — define the independent local behavior

LocalCloud exposes `--ec2-networking enforce`, requiring Docker images and an iptables-enabled helper. It is opt-in to make the changed packet behavior explicit; default legacy networking retains its previous contract. New packet-enabled instances carry `PacketNetworking: true`. Existing legacy guests are not converted automatically.

The implementation has three parts:

1. **Pure policy compiler** (`ec2_packet_policy.py`). Validates primary IPv4 configuration, merges attached SG rules, expands same-VPC group references, selects the subnet's explicit or VPC main route table, and applies longest-prefix route reachability. An IGW route requires a current attachment. Unsupported route appliances, IPv6 rules and secondary-interface/address forwarding fail explicitly.
2. **Owned namespace runtime** (`ec2_packet_runtime.py`, `ec2_packet_helper.py`). Starts a helper with the modeled private address on an owned Docker VPC bridge. It installs default-drop IPv4 and IPv6 filter tables before the guest starts; the unprivileged guest shares that namespace. Updates use `iptables-restore` inside the helper, never host firewall commands. Actual container IDs plus ownership labels protect existing resources.
3. **Loopback EIP TCP forwarding**. A backend-local listener checks the original client against current ingress and route policy, then relays bytes into the owned guest's loopback socket. No host-gateway ingress exception is added. Only explicitly published container ports are eligible. Disassociation, replacement, stop and backend shutdown retire listeners and their active relays. Route gates are also checked while relaying data.

Normal TCP/UDP/ICMP filtering is stateful: established/related replies pass the SG layer, while route reachability is evaluated first. SG changes affect fresh flows; existing conntrack sessions can persist after a rule is revoked. Loopback and narrowly scoped control destinations are documented exemptions. If IAM metadata is enabled, the backend's IP/port exemption covers that destination, not only a particular HTTP path.

The EIP address is a local development endpoint, not a public internet allocation. This is TCP forwarding, not AWS-style general public-IP SNAT/DNAT. Its loopback listener belongs to the backend's network namespace; a backend inside a container does not make that loopback address appear on the host.

## Step 3 — connect API changes to real state

Engine transactions apply the desired packet configuration before saving API state. An ordinary failed apply attempts to restore the prior committed policy. Runtime failures attempt to lock down affected namespaces and close forwarding before recovery; the cache is invalidated so rollback really reinstalls the prior policy.

Launch creates the filtered namespace before the application. Stop retains the namespace required by Docker's `container:` reference but removes EIP listeners. Start reapplies current policy before starting the application. Termination removes the guest's owned helper. VPC deletion removes its owned packet bridge. Restarting the backend with the same database/configuration reapplies policy and reconstructs eligible listeners.

This is bounded update rollback. There is no new durable cross-process provisioning journal or guarantee of atomicity across Docker, sockets and SQLite during a hard crash. A missing/replaced helper is rejected rather than adopted. Ordinary kernel changes are atomic per filter-table restore, not simultaneously across all instances.

## Step 4 — verify actual traffic

The dedicated `scripts/ec2-packet-live.py` fixture uses uniquely tagged current-source helpers and a unique modeled VPC CIDR. It starts two web guests and a controlled test server. It uses real sockets and packets, not just Describe output.

The checks cover:

- A fresh TCP connection blocked with no ingress, then allowed by CIDR and blocked after revocation.
- Same-VPC SG-reference traffic and union behavior without accidental allow-all CIDRs.
- Actual UDP permission changes.
- Egress restrictions independent of server ingress.
- A controlled external destination blocked without its route, allowed with an attached IGW route, then blocked when removed.
- Loopback EIP forwarding to the correct guest, original-client filtering and reassociation.
- Disassociation removing forwarding and IGW route removal blocking the EIP path.
- Guest stop/start and backend restart restoring the expected filter/forwarding configuration.
- Rejection and preservation of a foreign same-name holder.

A preliminary stop/start check expected a test identity file under `/tmp` to survive. Docker's existing tmpfs contract clears that file on restart. The corrected fixture recreates its test marker after restart and verifies real filter/forwarding behavior; it does not claim tmpfs persistence.

## Final verification and artifacts

**19 live checks passed** against the final source: `ev_793ee4cec0a34af02d1cccc7aa555d0f09ff88d64117ae6ee8cd08eefe1a433a`. The [live report](research/rea/ec2-packet-live-report.json), [live index](research/rea/ec2-packet-live-index.json) and [complete live evidence bundle](research/rea/ec2-packet-live-evidence.json.gz) include the source hashes, preliminary attempts and final results.

The final fixture also occupies a host publication port before launch. The failed Docker launch leaves no orphan namespace helper or committed instance record. Startup cleanup requires a per-launch nonce plus ownership labels and removes the exact inspected container ID, including when Docker created a stopped container without returning its ID. A foreign replacement fails those checks and is preserved.

Cleanup evidence `ev_c9705cb006dab98465dbe07d23e369f0b2a85d300d79fe73190eebbb4747efeb` confirms no fixture guests, packet namespaces, packet networks or dependency-image tag remained; [cleanup report](research/rea/ec2-packet-cleanup-report.json).

**336 unit tests passed**, evidence `ev_bcb9db4dc35e4c311a577aeebc466afc3c332c0ef3dce0fe7172c719f868da56`. New tests cover pure route/SG compilation, group-reference normalization and validation, state rollback, helper ownership, launch failures, listener collision handling, route checks and unsupported packet configurations. The [validation index](research/rea/ec2-packet-validation-index.json) and [validation bundle](research/rea/ec2-packet-validation-evidence.json.gz) retain the unit capture and source binding.

## Reproduce

```bash
python3 scripts/ec2-packet-reference.py
python3 scripts/ec2-packet-live.py
python3 -m unittest discover -s tests -q
```

The live fixture installs firewall dependencies inside a uniquely tagged container image, copies current LocalCloud source into its final helper, and removes its owned containers, bridges, listeners and image tags. It does not install host packages. The reports distinguish preliminary failures from final successful runs.

## Scope still open

- Kubernetes NetworkPolicy/CNI enforcement.
- Secondary-interface/private-address packet traffic, IPv6, NACLs and cross-VPC networking.
- NAT/ENI/transit/peering route appliances and full AWS routing semantics.
- Elastic IP UDP/ICMP and general public-IP NAT.
- Distributed/crash-atomic networking provisioning and recovery.
- Full LocalStack behavior equivalence across protocols/executors/versions.

The implementation provides tested local functionality where this pinned reference only demonstrated API records. It should be assessed as that explicit reconstruction and extension, not recovered proprietary code.
