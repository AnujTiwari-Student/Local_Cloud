"""Pure IPv4 packet-policy compiler for an owned EC2 guest namespace.

This module does not execute commands or alter host networking. The caller must
install the resulting filter tables only inside its owned guest network
namespace. Route gates constrain reachability, not Linux forwarding devices.
"""
import ipaddress

from .engine import Error


PROTOCOLS = {'6': 'tcp', '17': 'udp', '1': 'icmp', '-1': '-1',
             'tcp': 'tcp', 'udp': 'udp', 'icmp': 'icmp'}


def invalid(message):
    raise Error('InvalidParameterValue', message)


def unsupported(message):
    raise Error('UnsupportedOperation', message, 501)


def network(value):
    if not isinstance(value, str):
        invalid('Expected an IPv4 CIDR string for packet networking')
    try:
        result = ipaddress.ip_network(value, strict=False)
    except (ValueError, TypeError):
        invalid('Expected an IPv4 CIDR for packet networking')
    if result.version != 4:
        unsupported('IPv6 packet networking is not implemented')
    return result


def address(value):
    if not isinstance(value, str):
        invalid('Expected an IPv4 address string for packet networking')
    try:
        result = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        invalid('Expected an IPv4 address for packet networking')
    if result.version != 4:
        unsupported('IPv6 packet networking is not implemented')
    return str(result)


def _active(instance):
    return instance.get('State', {}).get('Name') not in ('terminated', 'shutting-down')


def _permissions(state, instance, permissions):
    resources = state.get('ec2', {})
    groups = resources.get('groups', {})
    output = []
    for permission in permissions:
        if not isinstance(permission, dict):
            invalid('Invalid packet-network security-group permission')
        if permission.get('Ipv6Ranges') or permission.get('PrefixListIds'):
            unsupported('IPv6 and prefix-list packet rules are not implemented')
        protocol = PROTOCOLS.get(str(permission.get('IpProtocol')))
        if protocol is None:
            unsupported('Packet rules support TCP, UDP, ICMP and all protocols only')
        rule = {'protocol': protocol}
        if protocol in ('tcp', 'udp'):
            low, high = permission.get('FromPort'), permission.get('ToPort')
            if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 65535:
                invalid('TCP/UDP packet ports must be integers from 0 through 65535')
            rule.update(from_port=low, to_port=high)
        elif protocol == 'icmp':
            kind, code = permission.get('FromPort'), permission.get('ToPort')
            if type(kind) is not int or type(code) is not int or not -1 <= kind <= 255 or not -1 <= code <= 255 or (kind == -1 and code != -1):
                invalid('ICMP type/code must be -1 or 0 through 255; any type requires any code')
            rule.update(from_port=kind, to_port=code)
        blocks = []
        ranges = permission.get('IpRanges', [])
        peers = permission.get('UserIdGroupPairs', [])
        if not isinstance(ranges, list) or not isinstance(peers, list):
            invalid('Packet permission ranges and group peers must be lists')
        for value in ranges:
            if not isinstance(value, dict) or 'CidrIp' not in value:
                invalid('Packet permission needs an IPv4 CIDR')
            blocks.append(str(network(value['CidrIp'])))
        for value in peers:
            if not isinstance(value, dict) or set(value) - {'GroupId', 'GroupName', 'UserId', 'Description'} or not value.get('GroupId'):
                unsupported('Packet group references require a local GroupId')
            if value.get('UserId', '000000000000') != '000000000000':
                unsupported('Cross-account packet group references are not implemented')
            peer = groups.get(value['GroupId'])
            if peer is None or peer.get('VpcId') != instance['VpcId']:
                unsupported('Packet group references require a group in the same VPC')
            for candidate in state.get('instances', {}).values():
                if _active(candidate) and candidate.get('VpcId') == instance['VpcId'] and any(g.get('GroupId') == value['GroupId'] for g in candidate.get('SecurityGroups', [])):
                    blocks.append(address(candidate['PrivateIpAddress']) + '/32')
        # Empty references do not become allow-all. AWS SG rules are a union.
        for block in sorted(set(blocks), key=lambda value: (int(network(value).network_address), network(value).prefixlen)):
            output.append({**rule, 'cidr': block})
    seen = set()
    unique = []
    for rule in output:
        identity = tuple(sorted(rule.items()))
        if identity not in seen:
            seen.add(identity)
            unique.append(rule)
    return sorted(unique, key=lambda rule: (rule['protocol'], rule['cidr'], rule.get('from_port', -1), rule.get('to_port', -1)))


def _routes(resources, instance, vpc):
    tables = [table for table in resources.get('route_tables', {}).values() if table.get('VpcId') == instance['VpcId']]
    explicit = [table for table in tables if any(a.get('SubnetId') == instance['SubnetId'] and a.get('AssociationState', {}).get('State', 'associated') == 'associated' for a in table.get('Associations', []))]
    main = [table for table in tables if any(a.get('Main') and a.get('AssociationState', {}).get('State', 'associated') == 'associated' for a in table.get('Associations', []))]
    selected = explicit or main
    if len(selected) != 1:
        invalid('Packet networking requires one effective subnet route table')
    result = []
    destinations = set()
    for route in selected[0].get('Routes', []):
        if route.get('DestinationIpv6CidrBlock') or route.get('DestinationPrefixListId'):
            unsupported('IPv6 and prefix-list packet routes are not implemented')
        destination = str(network(route.get('DestinationCidrBlock')))
        if destination in destinations:
            invalid('Packet route table has duplicate destinations')
        destinations.add(destination)
        targets = [field for field in ('GatewayId', 'NetworkInterfaceId', 'NatGatewayId', 'TransitGatewayId', 'VpcPeeringConnectionId', 'EgressOnlyInternetGatewayId', 'InstanceId') if route.get(field)]
        if targets != ['GatewayId']:
            unsupported('Packet routing supports local and attached internet-gateway targets only')
        target = route['GatewayId']
        if target == 'local':
            if destination != str(network(vpc['CidrBlock'])):
                unsupported('Packet local routes must match the VPC CIDR')
            reachable = route.get('State', 'active') == 'active'
        else:
            gateway = resources.get('gateways', {}).get(target)
            if gateway is None:
                # A deleted/detached IGW route is a blackhole, never allow-all.
                reachable = False
            else:
                reachable = route.get('State', 'active') == 'active' and any(a.get('VpcId') == instance['VpcId'] and a.get('State') == 'available' for a in gateway.get('Attachments', []))
        result.append({'cidr': destination, 'allow': reachable, 'target': target})
    result.sort(key=lambda value: (-network(value['cidr']).prefixlen, int(network(value['cidr']).network_address)))
    return selected[0]['RouteTableId'], result


def _exemptions(values):
    output = []
    for item in values or []:
        if not isinstance(item, dict) or set(item) != {'address', 'protocol', 'port'}:
            invalid('Control exemptions require address, protocol and port')
        if item['protocol'] not in ('tcp', 'udp') or type(item['port']) is not int or not 1 <= item['port'] <= 65535:
            invalid('Control exemptions require TCP/UDP and one valid port')
        value = {'address': address(item['address']), 'protocol': item['protocol'], 'port': item['port']}
        if value not in output:
            output.append(value)
    return sorted(output, key=lambda item: (item['address'], item['protocol'], item['port']))


def snapshot(state, instance, exemptions=None):
    """Validate one primary IPv4 interface and return a deterministic policy.

    ``exemptions`` are narrowly scoped infrastructure endpoints (for example a
    resolver IP/53 or credential-broker IP/port), never broad CIDR bypasses.
    Loopback and link-local IMDS traffic on loopback are exempt in ``render``.
    """
    resources = state.get('ec2', {})
    vpc = resources.get('vpcs', {}).get(instance.get('VpcId'))
    subnet = resources.get('subnets', {}).get(instance.get('SubnetId'))
    if vpc is None or subnet is None or subnet.get('VpcId') != instance.get('VpcId'):
        invalid('Packet networking requires a valid VPC and subnet')
    vpc_cidr, subnet_cidr = network(vpc['CidrBlock']), network(subnet['CidrBlock'])
    ip = address(instance.get('PrivateIpAddress'))
    if not subnet_cidr.subnet_of(vpc_cidr) or ipaddress.ip_address(ip) not in subnet_cidr:
        invalid('Packet guest address must belong to its VPC subnet')
    attached = [interface for interface in resources.get('interfaces', {}).values()
                if interface.get('Attachment', {}).get('InstanceId') == instance['InstanceId']]
    if len(attached) > 1 or any(interface.get('Attachment', {}).get('DeviceIndex', 0) != 0 for interface in attached):
        unsupported('Packet networking supports one primary interface; secondary-interface traffic is not implemented')
    if any(len(interface.get('PrivateIpAddresses', [])) > 1 for interface in attached):
        unsupported('Packet networking supports the primary private address only')
    if any(interface.get('SourceDestCheck', True) is not True for interface in attached):
        unsupported('Packet networking does not implement instance-based router forwarding')
    groups = []
    for reference in instance.get('SecurityGroups', []):
        group = resources.get('groups', {}).get(reference.get('GroupId'))
        if group is None or group.get('VpcId') != instance['VpcId']:
            invalid('Packet security groups must belong to the instance VPC')
        groups.append(group)
    if not groups:
        invalid('Packet networking requires a security group')
    table, routes = _routes(resources, instance, vpc)
    return {'version': 1, 'instance_id': instance['InstanceId'], 'private_ip': ip,
            'vpc_id': instance['VpcId'], 'vpc_cidr': str(vpc_cidr), 'subnet_cidr': str(subnet_cidr),
            'route_table_id': table, 'routes': routes,
            'ingress': _permissions(state, instance, [rule for group in groups for rule in group.get('IpPermissions', [])]),
            'egress': _permissions(state, instance, [rule for group in groups for rule in group.get('IpPermissionsEgress', [])]),
            'exemptions': _exemptions(exemptions)}


def _rule(chain, rule, source):
    match = ['-A', chain, '-s' if source else '-d', rule['cidr']]
    protocol = rule['protocol']
    if protocol != '-1':
        match += ['-p', protocol]
    if protocol in ('tcp', 'udp'):
        low, high = rule['from_port'], rule['to_port']
        match += ['--dport', str(low) if low == high else str(low) + ':' + str(high)]
    elif protocol == 'icmp' and rule['from_port'] != -1:
        kind, code = rule['from_port'], rule['to_port']
        match += ['--icmp-type', str(kind) if code == -1 else str(kind) + '/' + str(code)]
    return ' '.join([*match, '-j', 'ACCEPT'])


def render(policy):
    """Return one atomic ``iptables-restore`` filter-table transaction.

    Full table ownership is safe only in a dedicated namespace; never run this
    on the host or in a namespace shared with unrelated workloads.
    """
    lines = ['*filter', ':INPUT DROP [0:0]', ':FORWARD DROP [0:0]', ':OUTPUT DROP [0:0]',
             ':LC_ROUTE_IN - [0:0]', ':LC_ROUTE_OUT - [0:0]',
             ':LC_SG_IN - [0:0]', ':LC_SG_OUT - [0:0]',
             '-A INPUT -i lo -j ACCEPT', '-A OUTPUT -o lo -j ACCEPT']
    for exemption in policy['exemptions']:
        ip, protocol, port = exemption['address'], exemption['protocol'], str(exemption['port'])
        lines.append(f'-A OUTPUT -d {ip}/32 -p {protocol} --dport {port} -j ACCEPT')
        lines.append(f'-A INPUT -s {ip}/32 -p {protocol} --sport {port} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT')
    lines += ['-A INPUT -m conntrack --ctstate INVALID -j DROP',
              '-A OUTPUT -m conntrack --ctstate INVALID -j DROP',
              '-A INPUT -j LC_ROUTE_IN', '-A OUTPUT -j LC_ROUTE_OUT']
    for route in policy['routes']:
        verdict_in = 'LC_SG_IN' if route['allow'] else 'DROP'
        verdict_out = 'LC_SG_OUT' if route['allow'] else 'DROP'
        lines.append(f'-A LC_ROUTE_IN -s {route["cidr"]} -j {verdict_in}')
        lines.append(f'-A LC_ROUTE_OUT -d {route["cidr"]} -j {verdict_out}')
    lines += ['-A LC_ROUTE_IN -j DROP', '-A LC_ROUTE_OUT -j DROP',
              '-A LC_SG_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
              '-A LC_SG_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT']
    lines.extend(_rule('LC_SG_IN', rule, True) for rule in policy['ingress'])
    lines.extend(_rule('LC_SG_OUT', rule, False) for rule in policy['egress'])
    lines += ['-A LC_SG_IN -j DROP', '-A LC_SG_OUT -j DROP', 'COMMIT', '']
    return '\n'.join(lines)


def render_ipv6_lockdown():
    """Keep an unimplemented IPv6 path from bypassing IPv4 security groups."""
    return '*filter\n:INPUT DROP [0:0]\n:FORWARD DROP [0:0]\n:OUTPUT DROP [0:0]\n-A INPUT -i lo -j ACCEPT\n-A OUTPUT -o lo -j ACCEPT\nCOMMIT\n'


def route_allows(policy, ip):
    """Pure longest-prefix lookup useful for diagnostics and tests."""
    value = ipaddress.ip_address(address(ip))
    return next((route['allow'] for route in policy['routes'] if value in network(route['cidr'])), False)


def allows(policy, direction, peer_ip, protocol='tcp', port=None, icmp_type=None, icmp_code=None):
    """Check a new flow against routes and SG rules, without control exemptions.

    EIP forwarding can use this to evaluate the original client rather than a
    userspace relay's transport address. Existing conntrack sessions are handled
    by the installed kernel policy, not this new-flow diagnostic.
    """
    if direction not in ('ingress', 'egress'):
        invalid('Packet direction must be ingress or egress')
    protocol = PROTOCOLS.get(str(protocol))
    if protocol is None or protocol == '-1':
        invalid('Packet flow protocol must be TCP, UDP or ICMP')
    peer = ipaddress.ip_address(address(peer_ip))
    if not route_allows(policy, str(peer)):
        return False
    if protocol in ('tcp', 'udp') and (type(port) is not int or not 0 <= port <= 65535):
        invalid('Packet flow needs a valid destination port')
    if protocol == 'icmp' and (type(icmp_type) is not int or type(icmp_code) is not int or not 0 <= icmp_type <= 255 or not 0 <= icmp_code <= 255):
        invalid('Packet flow needs valid ICMP type and code')
    for rule in policy[direction]:
        if peer not in network(rule['cidr']) or rule['protocol'] not in ('-1', protocol):
            continue
        if rule['protocol'] == '-1':
            return True
        if protocol in ('tcp', 'udp') and rule['from_port'] <= port <= rule['to_port']:
            return True
        if protocol == 'icmp' and (rule['from_port'] == -1 or rule['from_port'] == icmp_type) and (rule['to_port'] == -1 or rule['to_port'] == icmp_code):
            return True
    return False
