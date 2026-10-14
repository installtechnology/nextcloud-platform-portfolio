import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from scripts.backup.operational.interlock import deployment_guard
from scripts.backup.capture.storage import CaptureFailure


class Store:
    root = Path('synthetic-not-created')
    locked = False
    @contextmanager
    def lock(self):
        self.locked=True
        try: yield
        finally: self.locked=False
    def read(self, path): return self.active if path=='active.json' else self.journal
    def operation(self, operation):
        if operation != 'synthetic-operation': raise CaptureFailure('INVALID_OPERATION_ID')


class GateTests(unittest.TestCase):
    def setUp(self): self.store=Store()
    def code(self, code, collector):
        with self.assertRaises(CaptureFailure) as error:
            with deployment_guard(self.store,collector): self.fail('Gate incorrectly opened')
        self.assertEqual(error.exception.code,code)
        self.assertFalse(self.store.locked)
    def test_lock_held_through_deploy(self):
        with deployment_guard(self.store,lambda:[]): self.assertTrue(self.store.locked)
        self.assertFalse(self.store.locked)
    def test_orphan_blocks_without_cleanup(self): self.code('ORPHANED_CAPTURE_PRESENT',lambda:['synthetic'])
    def test_unknown_process_state_blocks(self): self.code('PROCESS_INSPECTION_REQUIRED',lambda:None)
    def test_collector_error_sanitized(self):
        def failed(): raise RuntimeError('synthetic-secret')
        self.code('PROCESS_INSPECTION_REQUIRED',failed)
    def test_nonterminal_journal_blocks(self):
        self.store.active={'operation_id':'synthetic-operation','terminal':False}
        with patch.object(Path,'exists',return_value=True): self.code('RECOVERY_REQUIRED',lambda:[])
    def test_terminal_without_restoration_blocks(self):
        self.store.active={'operation_id':'synthetic-operation','terminal':True};self.store.journal={'service_state_restored':False}
        with patch.object(Path,'exists',return_value=True),patch('scripts.backup.operational.interlock.Capture.valid_journal',return_value=True):
            self.code('RECOVERY_REQUIRED',lambda:[])
    def test_terminal_invalid_journal_blocks(self):
        self.store.active={'operation_id':'synthetic-operation','terminal':True};self.store.journal={}
        with patch.object(Path,'exists',return_value=True): self.code('RECOVERY_REQUIRED',lambda:[])
    def test_terminal_still_checks_orphans(self):
        self.store.active={'operation_id':'synthetic-operation','terminal':True};self.store.journal={'service_state_restored':True}
        with patch.object(Path,'exists',return_value=True),patch('scripts.backup.operational.interlock.Capture.valid_journal',return_value=True):
            self.code('ORPHANED_CAPTURE_PRESENT',lambda:['synthetic'])
