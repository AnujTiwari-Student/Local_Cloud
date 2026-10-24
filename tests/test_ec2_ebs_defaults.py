"""Observed default-encryption flag must persist without claiming encryption."""
import tempfile
import unittest
from pathlib import Path

from localcloud.engine import Engine, Error


class EbsDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'state.sqlite3'
        self.engine = Engine(str(self.path))

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return self.engine.ec2.call(operation, args, 'test')

    def test_initial_enable_disable_flag_and_no_implicit_encryption_claim(self):
        self.assertEqual(self.call('GetEbsEncryptionByDefault'), {'EbsEncryptionByDefault':False})
        self.assertEqual(self.call('EnableEbsEncryptionByDefault'), {'EbsEncryptionByDefault':True})
        self.assertEqual(self.call('GetEbsEncryptionByDefault'), {'EbsEncryptionByDefault':True})
        # Reference behavior: the flag does not turn this volume into encrypted storage.
        volume=self.call('CreateVolume',AvailabilityZone='us-east-1a',Size=1)
        self.assertFalse(volume['Encrypted'])
        self.assertEqual(self.call('DisableEbsEncryptionByDefault'), {'EbsEncryptionByDefault':False})

    def test_flag_survives_backend_restart_and_portable_state(self):
        self.call('EnableEbsEncryptionByDefault')
        bundle=self.engine.tools.export_state()
        self.engine.close()
        self.engine=Engine(str(self.path))
        self.assertTrue(self.call('GetEbsEncryptionByDefault')['EbsEncryptionByDefault'])
        self.call('DisableEbsEncryptionByDefault')
        self.engine.tools.import_state(bundle)
        self.assertTrue(self.call('GetEbsEncryptionByDefault')['EbsEncryptionByDefault'])

    def test_enforcement_denies_mutation_without_ec2_permission(self):
        self.engine.iam_mode='enforce'
        with self.assertRaises(Error) as caught:self.call('EnableEbsEncryptionByDefault')
        self.assertEqual(caught.exception.code,'AccessDenied')
        self.engine.iam_mode='off'
        self.assertFalse(self.call('GetEbsEncryptionByDefault')['EbsEncryptionByDefault'])


if __name__=='__main__':unittest.main()
