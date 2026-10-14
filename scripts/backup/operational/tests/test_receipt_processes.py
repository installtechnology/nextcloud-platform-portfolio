"""Synthetic journal ownership regressions; no operational backup evidence."""
import copy
from pathlib import Path
import tempfile
import unittest
from scripts.backup.operational.receipt import reconciled_processes
from scripts.backup.sealing.common import SealFailure
from scripts.backup.capture.storage import CaptureFailure


class ProcessReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'capture').mkdir()
        self.records={
            'fence/exec-journal.json':{'operation_id':'fence','execs':[{'phase':'finished','exit_code':0}]},
            'capture/exec-journal.json':{'operation_id':'capture','execs':[{'phase':'finished','exit_code':2}]},
            'fence/state-helper-journal.json':{'operation_id':'fence','helpers':[{'phase':'removed'}]},
        }
        outer=self
        class ReadOnlyStore:
            root=outer.root
            def read(self,path):
                if path not in outer.records:raise CaptureFailure('FILE_MISSING')
                return copy.deepcopy(outer.records[path])
        self.store=ReadOnlyStore()

    def check(self):reconciled_processes(self.store,'fence','capture')
    def reject(self,code):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):self.check()
    def extra(self,phase='removed',operation='capture'):
        (self.root/'capture/state-helper-journal.json').touch()
        self.records['capture/state-helper-journal.json']={'operation_id':operation,'helpers':[{'phase':phase}]}
    def test_shared_probe_registry_only(self):self.check()
    def test_missing_owner_registry_rejected(self):
        del self.records['fence/state-helper-journal.json']
        with self.assertRaisesRegex(CaptureFailure,'^FILE_MISSING$'):self.check()
    def test_mixed_helper_owner(self):
        self.records['fence/state-helper-journal.json']['operation_id']='other'
        self.reject('LIVE_HELPER_RECONCILIATION_REQUIRED')
    def test_pending_owner_helper(self):
        self.records['fence/state-helper-journal.json']['helpers'][0]['phase']='remove_intent'
        self.reject('LIVE_HELPER_RECONCILIATION_REQUIRED')
    def test_invalid_helpers(self):
        self.records['fence/state-helper-journal.json']['helpers']=None
        self.reject('LIVE_HELPER_RECONCILIATION_REQUIRED')
    def test_extra_removed_capture_helper(self):self.extra();self.check()
    def test_extra_pending_capture_helper(self):
        self.extra('start_intent');self.reject('LIVE_HELPER_RECONCILIATION_REQUIRED')
    def test_extra_mixed_capture_helper(self):
        self.extra(operation='other');self.reject('LIVE_HELPER_RECONCILIATION_REQUIRED')
    def test_both_exec_owners_required(self):
        for operation in ('fence','capture'):
            with self.subTest(operation=operation):
                self.records[operation+'/exec-journal.json']['operation_id']='other'
                self.reject('LIVE_EXEC_RECONCILIATION_REQUIRED')
                self.records[operation+'/exec-journal.json']['operation_id']=operation
    def test_pending_exec(self):
        self.records['capture/exec-journal.json']['execs'][0]['phase']='created'
        self.reject('LIVE_EXEC_RECONCILIATION_REQUIRED')
    def test_boolean_exit_code_rejected(self):
        self.records['capture/exec-journal.json']['execs'][0]['exit_code']=False
        self.reject('LIVE_EXEC_RECONCILIATION_REQUIRED')


if __name__=='__main__':unittest.main()
