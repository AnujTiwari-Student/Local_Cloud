# EC2 packet filtering, routing and Elastic IP forwarding

LocalCloud has an opt-in **Docker packet enforcement** mode. Security-group and route changes then affect real guest traffic instead of only changing API records. Elastic IP associations can forward explicitly published TCP application ports through a backend-local loopback address.

This is a documented local implementation informed by REA observations. The tested LocalStack reference did not demonstrate these filtering/forwarding behaviors. See [the evidence report](REA_EC2_PACKET_NETWORKING.md) for the exact distinction.

## What runs where

Each instance owns a small network helper container. Docker gives that helper the instance's actual primary private IPv4 address on an owned VPC bridge. Its firewall is installed before the application container starts. The application joins this helper's network namespace and receives no network administration capability.

The helper has `NET_ADMIN` only inside its own namespace. LocalCloud does not run host `iptables`, `nft` or route commands. Normal Docker bridge and port-publishing operations still let the Docker daemon manage its own host bridge/NAT configuration.

| Feature | Behavior in enforcement mode |
| --- | --- |
| Ingress security groups | New inbound TCP/UDP/ICMP flows require a matching rule from any attached group. |
| Egress security groups | New outbound flows require a matching egress rule. Default group egress remains allow-all until revoked. |
| Stateful replies | Established/related replies pass the security-group gate. Revoking an SG rule does not necessarily terminate an already tracked connection. Test fresh connections after changing a rule. |
| Group references | Same-VPC group references expand to the active primary IPv4 addresses of matching instances. They do not become `0.0.0.0/0`. |
| Routes | The explicit subnet route table, or main VPC table, supplies longest-prefix reachability. Local VPC routes pass; an internet-gateway route requires its gateway to be attached. No route or a blackhole blocks traffic. |
| IPv6 | Filtered closed except loopback; IPv6 rule/route emulation is unsupported. |
| Elastic IP | IPv4 loopback TCP listeners on the backend forward the instance's explicitly published container ports. The original client source must pass SG and route checks. |

The route implementation gates packet reachability through the existing Docker topology. It does not create an AWS internet-gateway appliance or reproduce every Linux route/interface detail. All modeled subnets in a VPC share its Docker bridge; NACLs and physical subnet isolation are separate capabilities.

## Build and start

Build the current LocalCloud base image, then the optional helper with firewall tools:

```bash
docker build -t localcloud:ec2-runtime .
docker build -f Dockerfile.network -t localcloud:ec2-network .
```

`Dockerfile.network` installs `iptables` inside that image. Nothing is installed on your host. Use an application image with a web server listening on port 8080; the existing [EC2 infrastructure example](EC2_INFRASTRUCTURE.md) builds `localcloud-ec2-web:dev`.

Start the backend in one terminal:

```bash
export LOCALCLOUD_MANAGEMENT_TOKEN="$(cat ~/.config/localcloud/server-token)"
localcloud start --port 4567 --data .localcloud/packet-network.sqlite3 \
  --ec2-image ami-web=localcloud-ec2-web:dev \
  --ec2-helper-image localcloud:ec2-network \
  --ec2-networking enforce
```

In another terminal:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4567
cat ~/.config/localcloud/server-token | localcloud auth login --token-stdin
localcloud status
```

The mode requires Docker, an image allowlist, and a helper containing the firewall tools. It rejects Kubernetes and root-capable `--ec2-guest-mode`. Kubernetes NetworkPolicy/CNI enforcement is not implemented by this step. Without the flag, existing networking behavior stays available and does not claim packet enforcement. Restart a packet-enabled database with the same flag and configuration. Only newly launched instances receive the packet namespace; existing legacy Docker instances are not retrofitted. Recreate them in this mode before expecting packet filtering or EIP forwarding. Describe output marks enabled instances with `PacketNetworking: true`.

## Step 1: create a local VPC and security group

These commands capture returned IDs using Python's JSON parser. Pick a CIDR that does not overlap an existing host or Docker network. Docker rejects conflicting network allocations rather than silently choosing another address range.

```bash
VPC=$(localcloud ec2 create-vpc --cidr-block 10.251.0.0/24 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Vpc"]["VpcId"])')
SUBNET=$(localcloud ec2 create-subnet --vpc-id "$VPC" --cidr-block 10.251.0.16/28 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Subnet"]["SubnetId"])')
SG=$(localcloud ec2 create-security-group --vpc-id "$VPC" \
  --group-name web --description 'Web packet test' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["GroupId"])')
```

## Step 2: launch with closed ingress

```bash
localcloud ec2 run-instances --image-id ami-web --name packet-web \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --private-ip-address 10.251.0.20 --publish 8088:8080
```

Try `http://127.0.0.1:8088` in a browser. The port exists, but the new connection is blocked because the security group has no ingress rule. The application has its actual private IP `10.251.0.20`; the API's address is no longer merely a fictional guest address in this mode.

Allow new TCP connections on the application's port:

```bash
localcloud ec2 authorize-security-group-ingress --group-id "$SG" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":8080,"ToPort":8080,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
```

Reload the browser. A fresh connection should reach the application. Docker's host publication may translate the source to the VPC bridge gateway; use an appropriate CIDR for that actual source when restricting published-port access. The Elastic IP path below checks the original loopback client's source instead.

Revoke the same permission to block **new** connections:

```bash
localcloud ec2 revoke-security-group-ingress --group-id "$SG" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":8080,"ToPort":8080,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
```

Browsers may reuse established connections. Close the connection or use a fresh client when testing revocation.

## Step 3: configure external routes

Private VPC peer traffic uses the local route. Traffic outside the VPC additionally needs an active route and matching egress permissions. Attach an internet gateway and create a default route:

```bash
IGW=$(localcloud ec2 create-internet-gateway \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["InternetGateway"]["InternetGatewayId"])')
localcloud ec2 attach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC"
RTB=$(localcloud ec2 describe-route-tables \
  --filters "[{\"Name\":\"vpc-id\",\"Values\":[\"$VPC\"]}]" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["RouteTables"][0]["RouteTableId"])')
localcloud ec2 create-route --route-table-id "$RTB" \
  --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW"
```

Removing this default route blocks traffic that depends on it:

```bash
localcloud ec2 delete-route --route-table-id "$RTB" --destination-cidr-block 0.0.0.0/0
```

Recreate the route before the Elastic IP example. More-specific routes take precedence. Detaching an internet gateway makes routes through it unreachable even if their API record still exists. Unsupported ENI/NAT/transit/peering forwarding is rejected in enforced packet policies instead of silently allowing traffic.

DNS on Docker's loopback resolver and loopback metadata remain infrastructure paths. If optional IMDS role credentials are enabled, the configured LocalCloud backend IP/port is a scoped control exemption. The firewall cannot distinguish the broker's HTTP path from another request to that same destination port; that destination remains reachable independently of application egress rules. This is not a general internet exemption.

## Step 4: forward an Elastic IP

Permit the backend's loopback client and ensure the default IGW route from Step 3 exists:

```bash
localcloud ec2 authorize-security-group-ingress --group-id "$SG" \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":8080,"ToPort":8080,"IpRanges":[{"CidrIp":"127.0.0.1/32"}]}]'
EIP=$(localcloud ec2 allocate-address --domain vpc \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["AllocationId"])')
INSTANCE=$(localcloud ec2 describe-instances --name packet-web \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Reservations"][0]["Instances"][0]["InstanceId"])')
localcloud ec2 associate-address --allocation-id "$EIP" --instance-id "$INSTANCE"
localcloud ec2 describe-addresses --allocation-ids "$EIP"
```

Open `http://PUBLIC_IP_FROM_OUTPUT:8080`. The Elastic IP listener uses the **container port** 8080, while the separate Docker host publication uses 8088. Only the ports explicitly published on the instance are eligible; associating an address does not expose every listening service.

The local address pool is `127.240.*.*`. No external/public address is claimed and no host IP alias is added. The listener exists in the backend process's network namespace: if the backend itself runs in a container, that loopback address is inside that container. This first implementation's host/browser EIP example runs the backend directly on the host. It is not a public internet endpoint or a Kubernetes load balancer.

Forwarding evaluates the original source (normally `127.0.0.1`) before opening a byte relay into the owned guest's loopback socket. It does not add an ingress exception for the host gateway that could bypass security-group checks on direct private-IP traffic. Reassociation points new connections at the replacement instance and closes old forwarding sessions. Disassociation removes the listener; route removal prevents continued forwarding.

If a listener already occupies an Elastic IP/port, association fails and rolls back; LocalCloud does not stop the other listener.

## Stop, restart and clean up

```bash
localcloud ec2 stop-instances --name packet-web
localcloud ec2 start-instances --name packet-web
localcloud ec2 terminate-instances --name packet-web
```

Stop removes the instance's EIP listeners while retaining its namespace for the stopped Docker guest's namespace reference. Start reapplies policy before starting the guest and restores eligible forwarding. Namespace identity is stored alongside the instance database and checked against the actual container ID and ownership labels.

When the backend closes, its EIP listeners close, but persistent guest/helper containers remain for resume. Restarting with the same database, image mapping and `--ec2-networking enforce` reapplies policies and reconstructs forwarding. Termination removes the owned guest namespace helper. Disassociate/release addresses and remove remaining interfaces/subnet/VPC as usual; deleting the VPC removes its owned packet bridge.

## Current limits

This is primary-interface IPv4 packet enforcement. Secondary interfaces/private addresses, IPv6 networking, NACLs, arbitrary route appliances and cross-VPC forwarding remain unimplemented. Elastic IP forwarding currently supports TCP only on explicit container ports; UDP/ICMP traffic is filtered between guests but is not forwarded through an Elastic IP. There is no public-IP SNAT, full AWS NAT behavior, bandwidth emulation or global routing infrastructure.

Changes are applied to external filters before committing API state. Ordinary update failures attempt restoration of the previous policy; a failed apply attempts to lock down affected namespaces and closes forwarding before recovery. This is bounded rollback, not a durable cross-process transaction or a guarantee against a crash during initial provisioning. Preserve the database and inspect owned runtime resources after an interrupted launch. Missing/replaced helpers are not silently adopted.
