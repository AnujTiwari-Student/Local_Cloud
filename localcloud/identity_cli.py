"""CLI wiring for the implemented IAM and STS subset."""
def configure(iam, group, operation):
    for name in ('create-role', 'get-role', 'delete-role', 'update-assume-role-policy', 'put-role-policy', 'get-role-policy', 'delete-role-policy', 'list-role-policies', 'list-instance-profiles-for-role'):
        item = operation(iam, name)
        item.add_argument('--role-name', required=True)
        if name in ('create-role', 'update-assume-role-policy'):
            item.add_argument('--assume-role-policy-document' if name == 'create-role' else '--policy-document', required=True)
        if name == 'create-role':
            item.add_argument('--path')
            item.add_argument('--description')
            item.add_argument('--max-session-duration', type=int)
        if name in ('put-role-policy', 'get-role-policy', 'delete-role-policy'):
            item.add_argument('--policy-name', required=True)
        if name == 'put-role-policy': item.add_argument('--policy-document', required=True)
    operation(iam, 'list-roles')
    for name in ('create-instance-profile', 'get-instance-profile', 'delete-instance-profile', 'add-role-to-instance-profile', 'remove-role-from-instance-profile'):
        item = operation(iam, name)
        item.add_argument('--instance-profile-name', required=True)
        if name == 'create-instance-profile': item.add_argument('--path')
        if name in ('add-role-to-instance-profile', 'remove-role-from-instance-profile'): item.add_argument('--role-name', required=True)
    operation(iam, 'list-instance-profiles')
    sts = group('sts', 'Local temporary role credentials')
    operation(sts, 'get-caller-identity')
    assume = operation(sts, 'assume-role')
    assume.add_argument('--role-arn', required=True)
    assume.add_argument('--role-session-name', required=True)
    assume.add_argument('--duration-seconds', type=int)


def payload(args, json_value):
    fields = {'role_name': 'RoleName', 'assume_role_policy_document': 'AssumeRolePolicyDocument',
              'policy_document': 'PolicyDocument', 'policy_name': 'PolicyName', 'path': 'Path',
              'description': 'Description', 'max_session_duration': 'MaxSessionDuration',
              'instance_profile_name': 'InstanceProfileName', 'role_arn': 'RoleArn',
              'role_session_name': 'RoleSessionName', 'duration_seconds': 'DurationSeconds'}
    result = {'Action': ''.join(word.title() for word in args.operation.split('-'))}
    for attribute, key in fields.items():
        value = getattr(args, attribute, None)
        if value is not None: result[key] = json_value(value) if attribute.endswith('_document') else value
    return result
