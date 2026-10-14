"""Fictitious fixtures only. Keys stay in OS temporary directories outside the repository."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts.backup.capture.engine import Capture
from scripts.backup.capture.storage import Store
from scripts.backup.capture.tests.test_capture import FakeBackend
from scripts.backup.sealing.common import SealFailure
from scripts.backup.sealing.crypto import Age
from scripts.backup.sealing.hooks import CONTROLS, InventoryHooks
from scripts.backup.sealing.simulator import MemoryS3

ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = json.loads((ROOT / 'contracts/recovery/fixtures/complete-unproven.json').read_text())
SOURCE = TEMPLATE['source']
OFFICIAL_WINDOWS_AGE_SHA256 = '2821a4ed191da07372acd302e5f6feae7a7985e285e1417765ebe74025af45f0'
OFFICIAL_WINDOWS_GENERATOR_SHA256 = '1549c7049be32695594bedd09bbd352a94b6013a9d5c43364f3c6cd7a09ab61c'


class Attestor:
    def __init__(self, backend, failure=None):
        self.backend, self.failure = backend, failure
        self.calls = []

    def check(self, bid, consistency, stage):
        assert self.backend.running is False  # Proves mock ordering, not real fencing.
        self.calls.append(stage)
        evidence = {'operation_id': bid, 'consistency_id': consistency, 'simulation': True,
                    'fence_id': 'synthetic-fence', **{k: True for k in CONTROLS}}
        if self.failure in CONTROLS:
            evidence[self.failure] = False
        return evidence


class SealingBackend(FakeBackend):
    """PR3-shaped synthetic database identifiers; no database connection."""
    def preflight(self):
        original = super().preflight()
        original.update(database=TEMPLATE['compatibility']['database_name'],
                        owner=TEMPLATE['compatibility']['database_owner'],
                        table_prefix=TEMPLATE['compatibility']['table_prefix'])
        return original


def candidate(test, api=None, hooks=True, maintenance=False, attestor_failure=None):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    os.chmod(root, 0o700)
    store, backend = Store(root, synthetic=True), SealingBackend(maintenance=maintenance)
    backend.identity = SOURCE['installation_identity_sha256']
    api = api or MemoryS3(page_size=1)
    attestor = Attestor(backend, attestor_failure)
    observer = InventoryHooks(api, SOURCE, attestor) if hooks else None
    capture = Capture(store, backend, observer)
    result = capture.start()
    return api, store, backend, result['operation_id'], capture, attestor


def age_tools(test):
    path = os.environ.get('PR5_TEST_AGE')
    keygen = os.environ.get('PR5_TEST_AGE_KEYGEN')
    if not path or not keygen:
        test.skipTest('Approved portable age unavailable; real cryptography remains pending')
    # Extracted hashes are bound to the official ZIP verified against GitHub's release digest.
    # Never execute an arbitrary replacement merely because it reports the expected version.
    if os.name == 'nt':
        expected = {path: OFFICIAL_WINDOWS_AGE_SHA256, keygen: OFFICIAL_WINDOWS_GENERATOR_SHA256}
        for binary, digest in expected.items():
            if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != digest:
                raise AssertionError('Approved official test tool digest mismatch')
    # Test-only path: no product API accepts private keys or performs decryption.
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    identity = Path(temp.name) / 'synthetic-identity.txt'
    result = subprocess.run([keygen], capture_output=True, timeout=10, env=Age.environment())
    if result.returncode:
        raise AssertionError('Synthetic key generation failed')
    fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(result.stdout)
    public = subprocess.run([keygen, '-y', str(identity)], capture_output=True, timeout=10, env=Age.environment())
    if public.returncode:
        raise AssertionError('Synthetic public-key derivation failed')
    binary_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return Age(path, binary_hash), public.stdout.decode().strip(), identity


def decrypt(age, identity, content):
    result = subprocess.run([str(age.path), '--decrypt', '--identity', str(identity)], input=content,
                            capture_output=True, timeout=10, env=Age.environment())
    if result.returncode:
        raise SealFailure('TEST_DECRYPTION_FAILED')
    return result.stdout


class Assertions:
    def code(self, expected, operation):
        with self.assertRaises(Exception) as caught:
            operation()
        self.assertEqual(getattr(caught.exception, 'code', None), expected)

    def journal(self, store, bid):
        return store.read(bid + '/journal.json')
