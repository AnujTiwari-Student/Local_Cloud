"""Observed EBS encryption-default control-plane flag, not data encryption.

The normal LocalStack reference toggles this flag without changing implicit
CreateVolume encryption metadata. Keep it separate from Docker file storage.
"""
PARAMETERS = {
    'GetEbsEncryptionByDefault': set(),
    'EnableEbsEncryptionByDefault': set(),
    'DisableEbsEncryptionByDefault': set(),
}


class Defaults:
    def __init__(self, infrastructure):
        self.infra = infrastructure

    def perform(self, data, resources, operation, args):
        if operation == 'EnableEbsEncryptionByDefault':
            resources['_ebs_encryption_default'] = True
        elif operation == 'DisableEbsEncryptionByDefault':
            resources['_ebs_encryption_default'] = False
        return {'EbsEncryptionByDefault': resources.get('_ebs_encryption_default', False)}
