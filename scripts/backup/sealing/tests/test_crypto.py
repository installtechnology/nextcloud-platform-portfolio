import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup.sealing.common import Bounds, Budget
from scripts.backup.sealing.crypto import Age
from scripts.backup.sealing.tests.support import Assertions, age_tools, decrypt


class CryptoTests(Assertions, unittest.TestCase):
    def setUp(self):
        self.age, self.recipient, self.identity = age_tools(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        os.chmod(self.root, 0o700)
        (self.root / 'plain').write_bytes(b'fictitious data only')

    def encrypt(self, **kwargs):
        return self.age.encrypt(self.root, 'plain', 'cipher.age', self.recipient, **kwargs)

    def test_real_authenticated_round_trip(self):
        record = self.encrypt()
        data = (self.root / 'cipher.age').read_bytes()
        self.assertEqual(decrypt(self.age, self.identity, data), b'fictitious data only')
        self.assertEqual(record['ciphertext_sha256'], hashlib.sha256(data).hexdigest())
        self.assertNotIn(b'fictitious data only', data)

    def test_real_corruption_rejected(self):
        self.encrypt()
        body = bytearray((self.root / 'cipher.age').read_bytes())
        body[-1] ^= 1
        self.code('TEST_DECRYPTION_FAILED', lambda: decrypt(self.age, self.identity, bytes(body)))

    def test_real_truncation_rejected(self):
        self.encrypt()
        body = (self.root / 'cipher.age').read_bytes()
        self.code('TEST_DECRYPTION_FAILED', lambda: decrypt(self.age, self.identity, body[:-20]))

    def test_randomized_ciphertext(self):
        first = self.encrypt()
        second = self.age.encrypt(self.root, 'plain', 'second.age', self.recipient)
        self.assertNotEqual(first['ciphertext_sha256'], second['ciphertext_sha256'])

    def test_wrong_recipient_key_cannot_decrypt(self):
        self.encrypt()
        _, _, other_identity = age_tools(self)
        self.code('TEST_DECRYPTION_FAILED', lambda: decrypt(self.age, other_identity, (self.root / 'cipher.age').read_bytes()))

    def test_private_plugin_ssh_recipient_forbidden(self):
        for recipient in ('AGE-SECRET-KEY-FAKE', 'age-plugin-anything', 'ssh-ed25519 fixture', '-i private'):
            with self.subTest(recipient=recipient):
                self.code('INVALID_PUBLIC_RECIPIENT', lambda: self.age.encrypt(self.root, 'plain', 'cipher.age', recipient))

    def test_binary_provenance_mismatch(self):
        self.code('AGE_PROVENANCE_MISMATCH', lambda: Age(self.age.path, '0' * 64))

    def test_existing_target_not_overwritten(self):
        self.encrypt()
        old = (self.root / 'cipher.age').read_bytes()
        self.code('CIPHERTEXT_ALREADY_EXISTS', self.encrypt)
        self.assertEqual((self.root / 'cipher.age').read_bytes(), old)

    def test_source_hash_mismatch(self):
        self.code('PLAINTEXT_CHANGED', lambda: self.encrypt(plaintext_sha256='0' * 64))
        self.assertFalse((self.root / 'cipher.age').exists())

    def test_target_traversal(self):
        self.code('UNSAFE_CIPHERTEXT_PATH', lambda: self.age.encrypt(self.root, 'plain', 'other/cipher', self.recipient))

    def test_cancellation(self):
        self.code('TRANSFER_CANCELLED', lambda: self.encrypt(cancelled=lambda: True))

    def test_plaintext_limit(self):
        self.age.bounds = Bounds(artifact_bytes=1)
        self.code('PLAINTEXT_LIMIT', self.encrypt)

    def test_process_errors_do_not_expose_stderr(self):
        with patch('scripts.backup.sealing.crypto.subprocess.Popen', side_effect=OSError('fictitious-secret')):
            self.code('ENCRYPTION_FAILED', self.encrypt)

    def test_environment_strips_identity_aws_plugin_and_agent(self):
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'synthetic', 'AGE_PASSPHRASE': 'synthetic',
                                     'SSH_AUTH_SOCK': 'synthetic', 'PATH': 'plugin-synthetic'}):
            env = Age.environment()
            self.assertNotIn('PATH', env)
            self.assertNotIn('AGE_PASSPHRASE', env)
            self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
            self.assertNotIn('SSH_AUTH_SOCK', env)

    def test_budget_timeout(self):
        budget = Budget(Bounds())
        budget.end = 0
        self.code('TRANSFER_TIMEOUT', budget.check)

    def test_real_multichunk_round_trip(self):
        data = b'fictitious multi-chunk payload\n' * 200000
        (self.root / 'plain').write_bytes(data)
        self.encrypt()
        body = (self.root / 'cipher.age').read_bytes()
        self.assertGreater(len(body), 1024 ** 2)
        self.assertEqual(decrypt(self.age, self.identity, body), data)

    def test_source_mutation_signature_rejected(self):
        from scripts.backup.safe_files import signature
        count = [0]
        def changed(stream):
            info = signature(stream)
            # Do not mutate the executable's signature: this case targets plaintext only.
            if info[2] != (self.root / 'plain').stat().st_size:
                return info
            count[0] += 1
            return info if count[0] == 1 else (*info[:3], info[3] + 1, info[4])
        with patch('scripts.backup.sealing.crypto.signature', side_effect=changed):
            self.code('PLAINTEXT_CHANGED', self.encrypt)

    def test_empty_plaintext_rejected(self):
        (self.root / 'plain').write_bytes(b'')
        self.code('PLAINTEXT_LIMIT', self.encrypt)

    def test_process_nonzero_is_sanitized(self):
        from unittest.mock import Mock
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with patch('scripts.backup.sealing.crypto.subprocess.Popen', return_value=process):
            self.code('ENCRYPTION_FAILED', self.encrypt)

    def test_success_status_cannot_accept_header_only_ciphertext(self):
        from unittest.mock import Mock
        def invalid(*args, **kwargs):
            kwargs['stdout'].write(b'age-encryption.org/v1\n')
            process = Mock(returncode=0)
            process.poll.return_value = 0
            return process
        with patch('scripts.backup.sealing.crypto.subprocess.Popen', side_effect=invalid):
            self.code('INVALID_AGE_ENVELOPE', self.encrypt)

    def test_decryption_rejects_invalid_ciphertext(self):
        self.code('TEST_DECRYPTION_FAILED', lambda: decrypt(self.age, self.identity, b'fictitious invalid envelope'))
