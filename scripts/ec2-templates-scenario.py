"""Launch template contract probe for an isolated reference or candidate backend."""
import json
import os
import uuid
import boto3
from botocore.config import Config


def scenario():
    client = boto3.client('ec2', endpoint_url=os.environ.get('EC2_PROBE_ENDPOINT', 'http://127.0.0.1:4566'), region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test', aws_session_token=os.environ.get('EC2_PROBE_TOKEN'), config=Config(retries={'max_attempts': 0}, read_timeout=20))
    rows = []
    name = 'rea-template-' + uuid.uuid4().hex[:10]
    template = instance = None
    def call(label, operation, **params):
        try:
            result = getattr(client, operation)(**params)
            status = result.pop('ResponseMetadata')['HTTPStatusCode']
            rows.append({'case': label, 'operation': operation, 'parameters': params, 'status': status, 'result': result})
            return result
        except Exception as error:
            response = getattr(error, 'response', {})
            rows.append({'case': label, 'operation': operation, 'parameters': params, 'status': response.get('ResponseMetadata', {}).get('HTTPStatusCode'), 'error': response.get('Error', {'type': type(error).__name__, 'message': str(error)})})
            return {}
    try:
        image = os.environ.get('EC2_PROBE_IMAGE', 'ami-03cf127a')
        result = call('create', 'create_launch_template', LaunchTemplateName=name, ClientToken='rea-create-001', VersionDescription='initial', LaunchTemplateData={'ImageId': image, 'InstanceType': 't3.micro', 'UserData': 'ZWNobyB0ZW1wbGF0ZQ=='}, TagSpecifications=[{'ResourceType': 'launch-template', 'Tags': [{'Key': 'purpose', 'Value': 'rea'}]}])
        template = result['LaunchTemplate']['LaunchTemplateId']
        call('create-idempotent', 'create_launch_template', LaunchTemplateName=name, ClientToken='rea-create-001', VersionDescription='initial', LaunchTemplateData={'ImageId': image, 'InstanceType': 't3.micro', 'UserData': 'ZWNobyB0ZW1wbGF0ZQ=='}, TagSpecifications=[{'ResourceType': 'launch-template', 'Tags': [{'Key': 'purpose', 'Value': 'rea'}]}])
        call('duplicate-name', 'create_launch_template', LaunchTemplateName=name, LaunchTemplateData={'ImageId': image})
        call('describe', 'describe_launch_templates', LaunchTemplateNames=[name])
        call('versions', 'describe_launch_template_versions', LaunchTemplateId=template)
        call('version-create', 'create_launch_template_version', LaunchTemplateId=template, SourceVersion='$Latest', VersionDescription='larger', LaunchTemplateData={'InstanceType': 't3.small'})
        call('versions-latest', 'describe_launch_template_versions', LaunchTemplateName=name, Versions=['$Latest', '$Default'])
        call('default-change', 'modify_launch_template', LaunchTemplateId=template, DefaultVersion='2')
        call('version-after-default', 'describe_launch_template_versions', LaunchTemplateId=template, Versions=['$Default'])
        result = call('run-template', 'run_instances', LaunchTemplate={'LaunchTemplateId': template}, MinCount=1, MaxCount=1)
        if result.get('Instances'):
            instance = result['Instances'][0]['InstanceId']
            call('instance-template-data', 'get_launch_template_data', InstanceId=instance)
        call('version-missing', 'describe_launch_template_versions', LaunchTemplateId=template, Versions=['999'])
        call('delete-default-version', 'delete_launch_template_versions', LaunchTemplateId=template, Versions=['2'])
        call('delete-old-version', 'delete_launch_template_versions', LaunchTemplateId=template, Versions=['1'])
        call('template-missing', 'describe_launch_templates', LaunchTemplateIds=['lt-00000000000000000'])
    except Exception as error:
        rows.append({'case': 'probe-interrupted', 'error': {'type': type(error).__name__, 'message': str(error)}})
    finally:
        if instance:
            call('cleanup-instance', 'terminate_instances', InstanceIds=[instance])
        if template:
            call('cleanup-template', 'delete_launch_template', LaunchTemplateId=template)
    return rows


print(json.dumps(scenario(), default=str))
