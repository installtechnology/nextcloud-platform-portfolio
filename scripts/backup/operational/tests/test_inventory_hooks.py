"""Synthetic interface/order tests; not evidence of real quiescence."""
import copy
import unittest
from unittest.mock import patch
from scripts.backup.operational.tests import test_fencing as f
from scripts.backup.operational.tests.test_s3 import Client
from scripts.backup.operational.s3 import DataInventory
from scripts.backup.operational.inventory_hooks import OperationalInventoryHooks
from scripts.backup.operational.inventory_projection import InventoryProjection
from scripts.backup.capture.storage import CaptureFailure


SOURCE = {'account_id': f.SCOPE.account, 'project': 'synthetic', 'environment': 'test',
          'installation_identity_sha256': 'e'*64, 'data_bucket': f.SCOPE.bucket, 'region': 'us-east-1'}


class HookTests(unittest.TestCase):
    def setUp(self):
        self.store = f.Store(); self.api = f.API(self.store)
        self.coordinator = f.Coordinator(self.store, self.api, f.SCOPE, f.Authority(), f.Barrier())
        self.data = DataInventory(Client(), f.SCOPE.bucket, f.SCOPE.account, 'us-east-1')
        self.journal = {'operation_id': f.CAPTURE, 'consistency_id': 'capture-synthetic',
                        'original': {'identity': SOURCE['installation_identity_sha256']}}
    def reject(self, code, fn):
        with self.assertRaises(CaptureFailure) as error: fn()
        self.assertEqual(error.exception.code, code)
    def fail_window(self, body):
        with self.coordinator.window(f.OP) as window: body(window)
    def test_before_after_capture_and_policy_binding(self):
        with patch('scripts.backup.operational.inventory_hooks.inventory', return_value={'projection_sha256': self.api.hash}) as listing:
            with self.coordinator.window(f.OP) as window:
                hooks = OperationalInventoryHooks(self.data, SOURCE, window)
                for phase in ('before', 'after'):
                    observation = getattr(hooks, phase)(self.journal)
                    self.journal['inventory_observation'] = observation
                    self.assertFalse(observation['backup_complete'])
                    self.assertEqual(observation['capture_id'], f.CAPTURE)
                    self.assertEqual(observation['fence_id'], f.OP)
                self.assertEqual(observation['before']['capture_id'], observation['after']['capture_id'])
                self.assertEqual(listing.call_args.args[2:], (f.CAPTURE, 'capture-synthetic'))
                self.coordinator.release(window, lambda: None)
    def test_installation_mismatch_before_listing(self):
        self.journal['original']['identity'] = 'c'*64
        self.reject('INVENTORY_INSTALLATION_MISMATCH', lambda: self.fail_window(
            lambda w: OperationalInventoryHooks(self.data, SOURCE, w).before(self.journal)))
    def test_unstable_snapshot_rejected(self):
        with patch('scripts.backup.operational.inventory_hooks.inventory', return_value={'projection_sha256': 'c'*64}):
            self.reject('FENCE_INVENTORY_CHANGED', lambda: self.fail_window(
                lambda w: OperationalInventoryHooks(self.data, SOURCE, w).before(self.journal)))
    def test_policy_changed_during_listing_rejected(self):
        def listing(*args):
            import json
            policy = f.policy_document(self.api.raw); policy['Id'] = 'third-party'
            self.api.raw = json.dumps(policy)
            return {'projection_sha256': self.api.hash}
        with patch('scripts.backup.operational.inventory_hooks.inventory', side_effect=listing):
            self.reject('FENCE_POLICY_CONCURRENT_CHANGE', lambda: self.fail_window(
                lambda w: OperationalInventoryHooks(self.data, SOURCE, w).before(self.journal)))
    def test_source_missing_field_rejected(self):
        source = copy.deepcopy(SOURCE); del source['region']
        self.reject('INVENTORY_SOURCE_INVALID', lambda: InventoryProjection(self.data, source, f.OP))
    def test_account_mismatch_rejected(self):
        source = copy.deepcopy(SOURCE); source['account_id'] = '999999999999'
        self.reject('INVENTORY_SCOPE_MISMATCH', lambda: InventoryProjection(self.data, source, f.OP))
    def test_projection_scoped_to_bucket(self):
        projection = InventoryProjection(self.data, SOURCE, f.OP)
        self.reject('INVENTORY_SCOPE_MISMATCH', lambda: projection.projection_hash('other-bucket'))
    def test_projection_deep_copies_source(self):
        source = copy.deepcopy(SOURCE); projection = InventoryProjection(self.data, source, f.OP)
        source['data_bucket'] = 'other-bucket'
        self.assertEqual(projection.source['data_bucket'], f.SCOPE.bucket)
    def test_after_without_before_rejected(self):
        self.reject('INVENTORY_BEFORE_MISSING', lambda: self.fail_window(
            lambda w: OperationalInventoryHooks(self.data, SOURCE, w).after(self.journal)))
    def test_changed_durable_before_observation_rejected(self):
        def body(window):
            hooks = OperationalInventoryHooks(self.data, SOURCE, window)
            self.journal['inventory_observation'] = hooks.before(self.journal)
            self.journal['inventory_observation']['before']['receipt']['inventory_sha256'] = 'd'*64
            hooks.after(self.journal)
        with patch('scripts.backup.operational.inventory_hooks.inventory', return_value={'projection_sha256': self.api.hash}):
            self.reject('INVENTORY_RECEIPT_CHANGED', lambda: self.fail_window(body))
    def test_changed_consistency_id_rejected(self):
        def body(window):
            hooks = OperationalInventoryHooks(self.data, SOURCE, window)
            self.journal['inventory_observation'] = hooks.before(self.journal)
            self.journal['consistency_id'] = 'other-capture'
            hooks.after(self.journal)
        with patch('scripts.backup.operational.inventory_hooks.inventory', return_value={'projection_sha256': self.api.hash}):
            self.reject('INVENTORY_HOOK_REUSED', lambda: self.fail_window(body))
    def test_returned_before_mutation_does_not_change_internal_copy(self):
        def body(window):
            hooks = OperationalInventoryHooks(self.data, SOURCE, window)
            observed = hooks.before(self.journal)
            observed['before']['receipt']['inventory_sha256'] = 'd'*64
            self.assertEqual(hooks.first['receipt']['inventory_sha256'], self.api.hash)
            self.coordinator.release(window, lambda: None)
        with patch('scripts.backup.operational.inventory_hooks.inventory', return_value={'projection_sha256': self.api.hash}):
            self.fail_window(body)
