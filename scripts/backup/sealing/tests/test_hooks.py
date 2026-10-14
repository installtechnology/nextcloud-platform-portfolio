import unittest
from unittest.mock import patch

from scripts.backup.capture.engine import Capture
from scripts.backup.sealing.hooks import CONTROLS, InventoryHooks
from scripts.backup.sealing.simulator import MemoryS3
from scripts.backup.sealing.tests.support import Assertions, SOURCE, candidate, Attestor


class HookTests(Assertions, unittest.TestCase):
    def test_default_capture_unchanged(self):
        _, store, _, bid, _, _ = candidate(self, hooks=False)
        journal = self.journal(store, bid)
        self.assertNotIn('inventory_observation', journal)
        self.assertEqual(journal['status'], 'local_candidate_only')

    def test_both_observations_before_writer_restart(self):
        _, store, backend, bid, _, attestor = candidate(self)
        self.assertEqual(attestor.calls, ['before', 'before_complete', 'after', 'after_complete'])
        journal = self.journal(store, bid)
        phases = [e['phase'] for e in journal['events']]
        self.assertLess(phases.index('inventory_after_observed'), phases.index('restore_intent'))
        self.assertTrue(backend.running)
        self.assertFalse(backend.enabled)
        self.assertEqual(journal['status'], 'local_candidate_only')

    def test_preexisting_maintenance_preserved(self):
        _, _, backend, _, _, _ = candidate(self, maintenance=True)
        self.assertTrue(backend.enabled)
        self.assertNotIn('maintenance_off', backend.calls)

    def test_each_unproven_control_rejected(self):
        for field in CONTROLS:
            with self.subTest(control=field):
                self.code('WRITER_CONTROLS_UNPROVEN', lambda: candidate(self, attestor_failure=field))

    def test_failing_before_hook_restores_original_state(self):
        _, store, backend, _, _, _ = candidate(self, hooks=False)
        observer = InventoryHooks(MemoryS3(), SOURCE, Attestor(backend))
        with patch.object(observer, 'before', side_effect=RuntimeError('secret-not-to-print')):
            self.code('CAPTURE_INTERRUPTED', lambda: Capture(store, backend, observer).start())
        self.assertFalse(backend.enabled)
        self.assertTrue(backend.running)
        active = store.read('active.json')
        self.assertEqual(self.journal(store, active['operation_id'])['status'], 'incomplete')

    def test_inventory_mutation_after_dump_rejected(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        original = backend.dump
        def mutating(out):
            original(out)
            api.seed(SOURCE['data_bucket'], 'concurrent', b'write')
        backend.dump = mutating
        observer = InventoryHooks(api, SOURCE, Attestor(backend))
        self.code('INVENTORY_CHANGED', lambda: Capture(store, backend, observer).start())
        self.assertFalse(backend.enabled)
        self.assertTrue(backend.running)

    def test_equal_inventories_with_lost_fence_rejected(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        attestor = Attestor(backend)
        original = attestor.check
        def lost(bid, cid, stage):
            value = original(bid, cid, stage)
            if stage == 'after':
                value['fence_continuously_held'] = False
            return value
        attestor.check = lost
        self.code('WRITER_CONTROLS_UNPROVEN', lambda: Capture(store, backend, InventoryHooks(api, SOURCE, attestor)).start())
        self.assertTrue(backend.running)

    def test_fence_identity_changed_rejected(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        attestor = Attestor(backend)
        original = attestor.check
        def replaced(bid, cid, stage):
            value = original(bid, cid, stage)
            if stage == 'after':
                value['fence_id'] = 'different'
            return value
        attestor.check = replaced
        self.code('WRITER_FENCE_CHANGED', lambda: Capture(store, backend, InventoryHooks(api, SOURCE, attestor)).start())

    def test_hook_interruption_does_not_publish(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        observer = InventoryHooks(api, SOURCE, Attestor(backend))
        with patch.object(observer, 'after', side_effect=KeyboardInterrupt):
            self.code('CAPTURE_INTERRUPTED', lambda: Capture(store, backend, observer).start())
        self.assertTrue(backend.running)
        self.assertFalse(list(store.root.rglob('completion.json')))

    def test_failed_restore_preserves_maintenance(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        observer = InventoryHooks(api, SOURCE, Attestor(backend))
        backend.failure = 'start'
        with patch.object(observer, 'after', side_effect=RuntimeError):
            self.code('RECOVERY_REQUIRED', lambda: Capture(store, backend, observer).start())
        self.assertTrue(backend.enabled)
        self.assertFalse(store.read('active.json')['terminal'])

    def test_before_hook_cannot_reopen_writer_before_dump(self):
        api, store, backend, _, _, _ = candidate(self, hooks=False)
        observer = InventoryHooks(api, SOURCE, Attestor(backend))
        original = observer.before
        def reopen(journal):
            receipt = original(journal)
            backend.running = True
            return receipt
        with patch.object(observer, 'before', side_effect=reopen), patch.object(backend, 'dump') as dump:
            self.code('WRITER_STILL_RUNNING', lambda: Capture(store, backend, observer).start())
            dump.assert_not_called()
        self.assertTrue(backend.running)
        self.assertFalse(backend.enabled)

    def test_after_hook_rejects_different_operation(self):
        api, store, backend, bid, _, _ = candidate(self)
        observer = InventoryHooks(api, SOURCE, Attestor(backend))
        journal = self.journal(store, bid)
        backend.running = False
        observer.before(journal)
        journal['consistency_id'] = 'capture-another'
        self.code('INVENTORY_HOOK_REUSED', lambda: observer.after(journal))

    def test_observation_times_recorded_inside_quiescence(self):
        from scripts.backup.sealing.common import instant
        _, store, _, bid, _, _ = candidate(self)
        journal = self.journal(store, bid)
        first, last = journal['inventory_observation']['before'], journal['inventory_observation']['after']
        times = [journal['quiesced_at'], first['started_at'], first['finished_at'],
                 last['started_at'], last['finished_at'], journal['capture_finished_at']]
        self.assertEqual([instant(t) for t in times], sorted(instant(t) for t in times))
