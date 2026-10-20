"""EC2 contract scenario, run with Boto3 against an isolated local emulator."""
import json
import os
import boto3
from botocore.config import Config


def scenario(endpoint, image):
    client = boto3.client('ec2', endpoint_url=endpoint, region_name='us-east-1',
                          aws_access_key_id='test', aws_secret_access_key='test',
                          aws_session_token=os.environ.get('EC2_PROBE_TOKEN'),
                          config=Config(retries={'max_attempts': 0}, read_timeout=30))
    records, ids = [], []
    def call(label, operation, **parameters):
        try:
            result = getattr(client, operation)(**parameters)
            status = result.pop('ResponseMetadata')['HTTPStatusCode']
            records.append({'case': label, 'operation': operation, 'status': status, 'result': result})
            if operation == 'run_instances':
                ids.extend(i['InstanceId'] for i in result.get('Instances', []) if i['InstanceId'] not in ids)
            return result
        except Exception as error:
            response = getattr(error, 'response', None)
            records.append({'case': label, 'operation': operation,
                            'status': response.get('ResponseMetadata', {}).get('HTTPStatusCode') if response else None,
                            'error': response.get('Error') if response else {'type': type(error).__name__, 'message': str(error)}})
            return {}
    try:
        call('images', 'describe_images', ImageIds=[image])
        call('missing-image', 'describe_images', ImageIds=['ami-00000000000000000'])
        launch = dict(ImageId=image, InstanceType='t3.micro', MinCount=1, MaxCount=1,
                      UserData='printf rea-user-data-ok >/tmp/rea-sdk-user-data',
                      ClientToken='rea-idempotent-launch', TagSpecifications=[{
                          'ResourceType': 'instance', 'Tags': [{'Key': 'Name', 'Value': 'rea-web'}, {'Key': 'Env', 'Value': 'test'}]}])
        first = call('launch', 'run_instances', **launch)
        call('replay', 'run_instances', **launch)
        call('mismatched-replay', 'run_instances', **dict(launch, InstanceType='t3.small'))
        if not first.get('Instances'):
            return records
        identifier = first['Instances'][0]['InstanceId']
        call('describe', 'describe_instances', InstanceIds=[identifier])
        call('filter-tag', 'describe_instances', Filters=[{'Name': 'tag:Env', 'Values': ['test']}])
        call('filter-empty', 'describe_instances', Filters=[{'Name': 'tag:Env', 'Values': ['other']}])
        call('filter-invalid', 'describe_instances', Filters=[{'Name': 'not-real', 'Values': ['x']}])
        call('tag-update', 'create_tags', Resources=[identifier], Tags=[{'Key': 'Env', 'Value': 'dev'}, {'Key': 'Owner', 'Value': 'team'}])
        call('tags', 'describe_tags', Filters=[{'Name': 'resource-id', 'Values': [identifier]}])
        call('tag-delete-wrong-value', 'delete_tags', Resources=[identifier], Tags=[{'Key': 'Env', 'Value': 'other'}])
        call('tag-delete', 'delete_tags', Resources=[identifier], Tags=[{'Key': 'Owner'}])
        call('tags-after-delete', 'describe_tags', Filters=[{'Name': 'resource-id', 'Values': [identifier]}])
        call('attribute', 'describe_instance_attribute', InstanceId=identifier, Attribute='instanceType')
        call('user-data-attribute', 'describe_instance_attribute', InstanceId=identifier, Attribute='userData')
        call('stop', 'stop_instances', InstanceIds=[identifier])
        call('stop-again', 'stop_instances', InstanceIds=[identifier])
        call('modify', 'modify_instance_attribute', InstanceId=identifier, InstanceType={'Value': 't3.small'})
        call('attribute-after-modify', 'describe_instance_attribute', InstanceId=identifier, Attribute='instanceType')
        call('start', 'start_instances', InstanceIds=[identifier])
        call('start-again', 'start_instances', InstanceIds=[identifier])
        call('terminate', 'terminate_instances', InstanceIds=[identifier])
        call('terminate-again', 'terminate_instances', InstanceIds=[identifier])
        call('start-terminated', 'start_instances', InstanceIds=[identifier])
        call('missing-instance', 'describe_instances', InstanceIds=['i-00000000000000000'])
    finally:
        # A timed-out launch can still create a resource. Discover the scenario's
        # token before cleanup instead of trusting only returned launch IDs.
        discovered = client.describe_instances(Filters=[{'Name': 'client-token', 'Values': ['rea-idempotent-launch']}])
        for reservation in discovered.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                if instance['InstanceId'] not in ids:
                    ids.append(instance['InstanceId'])
        if ids:
            call('cleanup', 'terminate_instances', InstanceIds=ids)
    return records


if __name__ == '__main__':
    print(json.dumps(scenario(os.environ.get('EC2_PROBE_ENDPOINT', 'http://127.0.0.1:4566'),
                              os.environ.get('EC2_PROBE_IMAGE', 'ami-03cf127a')), default=str))
