from contextlib import contextmanager
from pathlib import Path
import copy
from unittest.mock import patch
import unittest
from scripts.backup.operational.lease import deploy_lease, assert_deployment_clear
from scripts.backup.capture.storage import CaptureFailure


class Store:
    root = Path('not-created-lease-test')
    def __init__(self): self.data = {}; self.locked = False
    @contextmanager
    def lock(self):
        if self.locked: raise CaptureFailure('CAPTURE_BUSY')
        self.locked = True
        try: yield
        finally: self.locked = False
    def write(self, path, value): self.data[path] = copy.deepcopy(value)
    def read(self, path): return self.data[path]


class LeaseTests(unittest.TestCase):
    def setUp(self): self.store = Store()
    def test_held_for_entire_mutation(self):
        with deploy_lease(self.store, '123:1', lambda: []):
            self.assertTrue(self.store.locked)
            self.assertEqual(self.store.data['deploy-lease.json']['phase'], 'held')
        self.assertEqual(self.store.data['deploy-lease.json']['phase'], 'released')
    def test_channel_loss_does_not_auto_release(self):
        with self.assertRaisesRegex(CaptureFailure, '^DEPLOY_INTERRUPTED$'):
            with deploy_lease(self.store, '123:1', lambda: []): raise ConnectionError('synthetic-sensitive')
        self.assertFalse(self.store.locked)
        with patch.object(Path, 'exists', return_value=True):
            with self.assertRaisesRegex(CaptureFailure, '^DEPLOY_LEASE_RECOVERY_REQUIRED$'):
                assert_deployment_clear(self.store)
    def test_nonterminal_never_expires(self):
        self.store.data['deploy-lease.json'] = {'kind': 'deploy-lease', 'version': 1, 'owner': '123:1', 'phase': 'held'}
        with patch.object(Path, 'exists', return_value=True):
            with self.assertRaisesRegex(CaptureFailure, '^DEPLOY_LEASE_RECOVERY_REQUIRED$'): assert_deployment_clear(self.store)
    def test_released_validated_not_arbitrary_true(self):
        self.store.data['deploy-lease.json'] = {'kind': 'deploy-lease', 'version': True, 'owner': '123:1', 'phase': 'released'}
        with patch.object(Path, 'exists', return_value=True):
            with self.assertRaisesRegex(CaptureFailure, '^DEPLOY_LEASE_RECOVERY_REQUIRED$'): assert_deployment_clear(self.store)
    def test_owner_injection(self):
        with self.assertRaisesRegex(CaptureFailure, '^DEPLOY_OWNER_INVALID$'):
            with deploy_lease(self.store, '123;evil', lambda: []): pass
    def test_concurrency_mutually_exclusive(self):
        with deploy_lease(self.store, '123:1', lambda: []):
            with self.assertRaisesRegex(CaptureFailure, '^CAPTURE_BUSY$'):
                with deploy_lease(self.store, '124:1', lambda: []): pass
