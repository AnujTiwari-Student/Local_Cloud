"""Bounded AMI contract probe; use only an isolated test endpoint."""
import json
import os


def scenario():
    import boto3
    from botocore.config import Config
    ec2 = boto3.client('ec2', endpoint_url=os.environ.get('EC2_PROBE_ENDPOINT', 'http://127.0.0.1:4566'),
                       region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                       aws_session_token=os.environ.get('EC2_PROBE_TOKEN'), config=Config(retries={'max_attempts': 0}))
    rows, images, instance = [], [], None
    def call(case, operation, **args):
        try:
            result = getattr(ec2, operation)(**args)
            status = result.pop('ResponseMetadata')['HTTPStatusCode']
            rows.append(dict(case=case, operation=operation, parameters=args, status=status, result=result))
            return result
        except Exception as error:
            response = getattr(error, 'response', {})
            rows.append(dict(case=case, operation=operation, parameters=args,
                             status=response.get('ResponseMetadata', {}).get('HTTPStatusCode'),
                             error=response.get('Error', {'message': str(error), 'type': type(error).__name__})))
            return {}
    try:
        image = call('register', 'register_image', Name='rea-images-one', Description='reference probe', Architecture='x86_64', RootDeviceName='/dev/sda1').get('ImageId')
        if image:
            images.append(image)
            call('describe-registered', 'describe_images', ImageIds=[image])
            duplicate = call('register-duplicate', 'register_image', Name='rea-images-one').get('ImageId')
            if duplicate:
                images.append(duplicate)
            call('attribute-description', 'describe_image_attribute', ImageId=image, Attribute='description')
            call('modify-description', 'modify_image_attribute', ImageId=image, Description={'Value': 'updated'})
            call('attribute-updated', 'describe_image_attribute', ImageId=image, Attribute='description')
            call('modify-public', 'modify_image_attribute', ImageId=image, LaunchPermission={'Add': [{'Group': 'all'}]})
            call('attribute-permissions', 'describe_image_attribute', ImageId=image, Attribute='launchPermission')
            call('describe-public', 'describe_images', ImageIds=[image])
            call('reset-permissions', 'reset_image_attribute', ImageId=image, Attribute='launchPermission')
            call('permissions-reset', 'describe_image_attribute', ImageId=image, Attribute='launchPermission')
        instance = call('run', 'run_instances', ImageId=os.environ.get('EC2_PROBE_IMAGE', 'ami-03cf127a'), MinCount=1, MaxCount=1).get('Instances', [{}])[0].get('InstanceId')
        if instance:
            created = call('create-image', 'create_image', InstanceId=instance, Name='rea-created-image', Description='created', NoReboot=True).get('ImageId')
            if created:
                images.append(created)
                call('describe-created', 'describe_images', ImageIds=[created])
        if image:
            call('deregister', 'deregister_image', ImageId=image)
            call('describe-deregistered', 'describe_images', ImageIds=[image])
            call('deregister-twice', 'deregister_image', ImageId=image)
            images.remove(image)
        call('missing-image', 'describe_image_attribute', ImageId='ami-00000000000000000', Attribute='description')
    finally:
        for image in images:
            call('cleanup-image', 'deregister_image', ImageId=image)
        if instance:
            call('cleanup-instance', 'terminate_instances', InstanceIds=[instance])
    return rows


def capture_reference():
    import pathlib
    import subprocess
    import time
    import uuid
    root = pathlib.Path(__file__).resolve().parents[1]
    image = 'localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
    name = 'localcloud-rea-images-' + uuid.uuid4().hex[:10]
    env = dict(os.environ, LOCALSTACK_AUTH_TOKEN=(pathlib.Path.home() / '.config/localstack/rea-auth-token').read_text().strip())
    report, created = {}, False
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--label', 'localcloud.audit=' + name,
                        '-e', 'LOCALSTACK_AUTH_TOKEN', '-e', 'EC2_VM_MANAGER=mock', image], env=env, capture_output=True, check=True)
        created = True
        # Probe within the container; host loopback publishing varies by Docker host.
        probe = 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:4566/_localstack/health",timeout=1)'
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', name, '/opt/code/localstack/.venv/bin/python', '-c', probe], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Reference readiness deadline exceeded')
        result = subprocess.run(['docker', 'exec', '-i', name, '/opt/code/localstack/.venv/bin/python', '-'],
                                input=pathlib.Path(__file__).read_text(), capture_output=True, text=True, timeout=140)
        report = {'exit': result.returncode, 'reference_image': image, 'records': json.loads(result.stdout) if result.returncode == 0 else [], 'stderr': result.stderr[-2000:]}
    finally:
        if created:
            info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
            assert info['Config']['Labels']['localcloud.audit'] == name
            subprocess.run(['docker', 'rm', '-f', '-v', name], check=True, capture_output=True)
            report['cleanup_completed'] = True
    (root / 'docs/research/rea/ec2-images-reference.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'exit': report.get('exit'), 'statuses': {r['case']: r['status'] for r in report.get('records', [])}, 'cleanup_completed': report.get('cleanup_completed')}))


def capture_local():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    controller = root / 'scripts/ec2-network-candidate.py'
    source = controller.read_text().replace('scripts/ec2-network-scenario.py', 'scripts/ec2-images-scenario.py').replace('ec2-network-localcloud.json', 'ec2-images-localcloud.json')
    exec(compile(source, str(controller), 'exec'), {'__file__': str(controller), '__name__': '__main__'})


if __name__ == '__main__':
    import sys
    if '--reference' in sys.argv:
        capture_reference()
    elif '--localcloud' in sys.argv:
        capture_local()
    else:
        print(json.dumps(scenario(), default=str))
