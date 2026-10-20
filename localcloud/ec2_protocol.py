"""EC2 Query codecs using selected public Botocore service model shapes.

The model is data extracted from the pinned reference's Botocore installation;
it does not contain LocalStack provider code. See docs/EC2_INFRASTRUCTURE.md.
"""
import base64
import json
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from .engine import Error

MODEL = json.loads(Path(__file__).with_name('ec2-model.json').read_text())


def decode(operation, fields):
    """Translate parsed/indexed Query fields to SDK member names/types."""
    if operation not in MODEL['operations']:
        return fields
    def read(value, name):
        shape = MODEL['shapes'][name]
        kind = shape['type']
        if kind == 'structure':
            if not isinstance(value, dict):
                raise Error('InvalidParameterValue', 'Expected structured parameter')
            members = shape['members']
            lookup = {link['serialization'].get('name', key): (key, link['shape']) for key, link in members.items()}
            # EC2 Query capitalizes serialized input names, including model
            # names such as internetGatewayId and vpcId.
            lookup.update({key[0].upper() + key[1:]: target for key, target in list(lookup.items())})
            result = {}
            for key, item in value.items():
                target = lookup.get(key) or ((key, members[key]['shape']) if key in members else None)
                if target:
                    if target[0] in result:
                        raise Error('InvalidParameterValue', 'Conflicting query parameters')
                    result[target[0]] = read(item, target[1])
                else:
                    result[key] = item  # Provider rejects unsupported fields explicitly.
            return result
        if kind == 'list':
            if not isinstance(value, list):
                raise Error('InvalidParameterValue', 'Expected indexed list parameter')
            return [read(v, shape['member']['shape']) for v in value]
        if kind in ('integer', 'long'):
            if isinstance(value, bool):
                raise Error('InvalidParameterValue', 'Expected integer parameter')
            try:
                return int(value)
            except (TypeError, ValueError):
                raise Error('InvalidParameterValue', 'Expected integer parameter')
        if kind == 'boolean':
            if value not in ('true', 'false', True, False):
                raise Error('InvalidParameterValue', 'Expected true or false')
            return value is True or value == 'true'
        if kind in ('float', 'double'):
            try:
                return float(value)
            except (TypeError, ValueError):
                raise Error('InvalidParameterValue', 'Expected numeric parameter')
        return value
    return read(fields, MODEL['operations'][operation]['input'])


def encode(operation, result, request_id):
    if operation not in MODEL['operations']:
        return None
    root = Element(operation + 'Response', xmlns='http://ec2.amazonaws.com/doc/2016-11-15/')
    SubElement(root, 'requestId').text = request_id
    def write(node, value, shape_name):
        shape = MODEL['shapes'][shape_name]
        if shape['type'] == 'structure':
            for key, link in shape['members'].items():
                if key in value and value[key] is not None:
                    child = SubElement(node, link['serialization'].get('name', key[0].lower() + key[1:]))
                    write(child, value[key], link['shape'])
        elif shape['type'] == 'list':
            for item in value:
                child = SubElement(node, shape['member']['serialization'].get('name', 'item'))
                write(child, item, shape['member']['shape'])
        elif shape['type'] == 'blob':
            node.text = base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode()
        else:
            node.text = str(value).lower() if isinstance(value, bool) else str(value)
    output = MODEL['operations'][operation]['output']
    if output:
        write(root, result, output)
    return tostring(root, encoding='unicode')
