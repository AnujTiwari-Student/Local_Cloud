#!/usr/bin/env python3
"""Bounded EC2 Query probe. Creates one instance and terminates it in finally.

Run only against an explicitly selected local test emulator. Raw XML is retained;
the comparison projection is intentionally smaller than full AWS compatibility.
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def probe(endpoint, image, token=None):
    records = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(action, **parameters):
        request = urllib.request.Request(endpoint, urllib.parse.urlencode({
            'Action': action, 'Version': '2016-11-15', **parameters,
        }).encode(), {'Content-Type': 'application/x-www-form-urlencoded'})
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        try:
            response = opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read().decode()
            root = ET.fromstring(body)
            # Remove namespace only in the projection; preserve the entire wire body.
            for node in root.iter():
                node.tag = node.tag.rsplit('}', 1)[-1]
            projection = {'status': response.status, 'root': root.tag}
            for key, path in {
                'error': './Errors/Error/Code', 'state': './/instanceState/name',
                'current': './/currentState/name', 'previous': './/previousState/name',
                'type': './/instanceType', 'return': './return',
            }.items():
                value = root.findtext(path)
                if value is not None:
                    projection[key] = value
            records.append({'operation': action, 'parameters': parameters,
                            'projection': projection, 'xml': body})
            return root, response.status

    identifier = None
    try:
        call('DescribeInstances')
        root, status = call('RunInstances', ImageId=image, InstanceType='t3.micro', MinCount=1, MaxCount=1)
        if status != 200:
            return {'completed': False, 'records': records, 'reason': 'Launch rejected'}
        identifier = root.findtext('.//instanceId')
        if not identifier:
            raise RuntimeError('Launch succeeded without an instance ID')
        call('DescribeInstances', **{'InstanceId.1': identifier})
        call('StopInstances', **{'InstanceId.1': identifier})
        call('ModifyInstanceAttribute', InstanceId=identifier, **{'InstanceType.Value': 't3.small'})
        call('DescribeInstances', **{'InstanceId.1': identifier})
        call('StartInstances', **{'InstanceId.1': identifier})
        call('DescribeInstances', **{'InstanceId.1': 'i-00000000000000000'})
    finally:
        if identifier:
            _, status = call('TerminateInstances', **{'InstanceId.1': identifier})
            if status != 200:
                raise RuntimeError('Probe instance termination failed: ' + identifier)
    return {'completed': True, 'records': records}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--token-env', help='Optional management token environment variable')
    args = parser.parse_args()
    print(json.dumps(probe(args.endpoint, args.image_id,
                           os.environ[args.token_env] if args.token_env else None)))
