#!/usr/bin/env python3
"""Inventory EC2 dispatch against public SDK contracts; never claim behavioral parity.

Run from any directory, after integrations:
  python3 scripts/ec2-coverage-audit.py --output docs/research/rea/ec2-release-inventory.json

The reference catalogue must contain the public Botocore operation names captured
from the pinned image. It does not establish which APIs that image implements.
This audit reads source/model files only; it never starts an engine or changes
resources, profiles, tokens or Docker state.
"""
import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def family(name):
    # Human navigation aid, not an inferred implementation architecture.
    for category, tokens in (
        ('launch-templates', ('LaunchTemplate',)),
        ('images', ('Image', 'Ami', 'ImageUsageReport')),
        ('storage', ('Volume', 'Snapshot', 'Ebs', 'FastSnapshot', 'LockedSnapshots')),
        ('networking', ('Vpc', 'Subnet', 'Network', 'Address', 'Ipam', 'Route', 'Gateway', 'Transit', 'Prefix', 'Cidr', 'SecurityGroup', 'Traffic', 'Vpn', 'FlowLog', 'Peering', 'Carrier', 'Dhcp')),
        ('capacity-and-placement', ('Capacity', 'Spot', 'Fleet', 'Reserved', 'Host', 'Placement', 'Availability', 'InstanceType', 'InstanceCredit')),
        ('identity-and-keys', ('Iam', 'KeyPair', 'VerifiedAccess')),
        ('tags', ('Tags',)),
        ('instances', ('Instance', 'Console', 'SerialConsole', 'Password')),
    ):
        if any(token in name for token in tokens):
            return category
    return 'other'


def literal_handler_mentions(paths):
    """Source pointers for string operation tests; pointers are not runtime proof."""
    result = {}
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        value = child.value
                        if value and value[0].isupper() and value.isalnum():
                            result.setdefault(value, []).append(f'{path.relative_to(ROOT)}:{node.lineno}')
    return {key: sorted(set(values)) for key, values in result.items()}


def audit(reference):
    from localcloud.ec2 import OPERATIONS
    from localcloud.ec2_infrastructure import PARAMETERS
    model = json.loads((ROOT / 'localcloud/ec2-model.json').read_text())
    modeled = set(model['operations'])
    dispatched = set(OPERATIONS)
    extensions = {'NameInstance'} & dispatched
    aws = dispatched - extensions
    names = reference.get('operations', reference.get('operation_names', []))
    if isinstance(names, dict):
        names = list(names)
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError('Reference catalogue needs a list of public SDK operation names')
    sdk = set(names)
    paths = [ROOT / 'localcloud/ec2.py', *sorted((ROOT / 'localcloud').glob('ec2_*.py'))]
    mentions = literal_handler_mentions(paths)
    omitted = sorted(sdk - aws)
    groups = {}
    for name in sorted(sdk | aws):
        groups.setdefault(family(name), {'dispatched': [], 'not_dispatched': []})['dispatched' if name in aws else 'not_dispatched'].append(name)
    return {
        'scope': 'Public SDK catalogue versus selected LocalCloud operation dispatch; not a parity certification',
        'reference': {key: value for key, value in reference.items() if key not in ('operations', 'operation_names')},
        'counts': {'reference_sdk_operations': len(sdk), 'localcloud_aws_dispatch_operations': len(aws), 'local_extensions': len(extensions), 'modeled_operations': len(modeled), 'sdk_operations_not_dispatched': len(omitted)},
        'consistency': {'modeled_not_dispatched': sorted(modeled - dispatched), 'aws_dispatch_not_modeled': sorted(aws - modeled), 'aws_dispatch_not_in_reference_sdk': sorted(aws - sdk)},
        'local_extensions': sorted(extensions),
        'operations': [{'operation': name, 'family': family(name), 'localcloud_dispatch': name in dispatched, 'localcloud_wire_model': name in modeled, 'parameter_allowlist': sorted(PARAMETERS[name]) if name in PARAMETERS else None, 'source_test_locations': mentions.get(name, []), 'reference_behavior': 'unprobed_by_this_inventory', 'parity': 'not_established_by_inventory'} for name in sorted(sdk | aws)],
        'families': groups,
        'limitations': ['A Botocore operation is an AWS contract, not proof of implementation in LocalStack.', 'Registered dispatch and parameter allowlists do not establish complete operation semantics.', 'Existing REA scenario reports establish only their declared cases, backends and pinned reference.', 'Family grouping uses name heuristics only.', 'This source inventory may change as parallel integration completes; rerun it after integration.'],
        'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [*paths, ROOT / 'localcloud/ec2-model.json']},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, default=ROOT / 'docs/research/rea/ec2-sdk-operation-catalogue.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(json.loads(args.reference.read_text()))
    encoded = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(encoded)
        print(json.dumps({'output': str(args.output), 'counts': result['counts'], 'consistency': result['consistency']}))
    else:
        print(encoded, end='')
    return 1 if any(result['consistency'].values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
