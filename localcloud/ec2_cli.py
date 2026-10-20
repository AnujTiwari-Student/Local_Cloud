"""AWS-style CLI arguments for the implemented infrastructure operations."""
import argparse
import base64
import re
from pathlib import Path
from .ec2_infrastructure import PARAMETERS
from .ec2_protocol import MODEL


def flag(name):
    return re.sub(r'(?<!^)(?=[A-Z])', '-', name).lower()


def register(group, operation):
    for name, fields in PARAMETERS.items():
        command = operation(group, flag(name))
        command.set_defaults(ec2_infrastructure_operation=name)
        shape = MODEL['shapes'][MODEL['operations'][name]['input']]
        for field in sorted(fields):
            link = shape['members'].get(field)
            if not link:
                continue
            field_shape = MODEL['shapes'][link['shape']]
            options = {'dest': 'ec2_' + field, 'required': field in shape['required'], 'default': None}
            kind = field_shape['type']
            if kind in ('integer', 'long'):
                options['type'] = int
            elif kind == 'boolean':
                options['action'] = argparse.BooleanOptionalAction
            elif kind == 'list' and MODEL['shapes'][field_shape['member']['shape']]['type'] == 'string':
                options['nargs'] = '+'
            elif kind in ('structure', 'list', 'map'):
                options['metavar'] = 'JSON_OR_FILE'
            command.add_argument('--' + flag(field), **options)


def payload(args, json_value):
    operation = getattr(args, 'ec2_infrastructure_operation', None)
    if not operation:
        return None
    shape = MODEL['shapes'][MODEL['operations'][operation]['input']]
    result = {'Action': operation}
    for field in PARAMETERS[operation]:
        value = getattr(args, 'ec2_' + field, None)
        if value is None:
            continue
        link = shape['members'][field]
        kind = MODEL['shapes'][link['shape']]['type']
        if kind in ('structure', 'map') or kind == 'list' and isinstance(value, str):
            value = json_value(value)
        elif kind == 'blob':
            if value.startswith('fileb://') or value.startswith('file://'):
                path = value.split('://', 1)[1]
                with Path(path).expanduser().open('rb') as source:
                    raw = source.read(16385)
            else:
                raw = value.encode()
            if len(raw) > 16384:
                raise ValueError('Public key material exceeds 16 KiB')
            value = base64.b64encode(raw).decode()
        result[field] = value
    return result
