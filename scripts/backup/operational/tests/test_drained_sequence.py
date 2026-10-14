"""Sequencing/failure tests with a synthetic backend, never an operational proof."""
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts.backup.capture.storage import Store,CaptureFailure
from scripts.backup.operational.drained_capture import LiveCapture,DrainedCoordinator


class Backend:
    limit=1000
    def __init__(self,trace,initial=False,fail=None):
        self.trace,self.maint,self.fail=trace,initial,fail;self.running=True
        self.fp='original-on' if initial else 'original-off';self.context=None
    def preflight(self):return {'maintenance':self.maint,'config_fingerprint':self.fp,'identity':'synthetic',
        'writers':{'nextcloud':True},'capture_sources':[],'estimated_capture_bytes':1}
    def maintenance(self):return self.maint
    def fingerprint(self):return self.fp
    def set_maintenance(self,state):self.trace.append('maintenance-'+str(state));self.maint=state;self.fp='on' if state else 'off'
    def stop_writer(self,service):self.trace.extend(['drain-http','reject-new-pg']);self.running=False
    def assert_quiescent(self,original):
        if self.fail=='writer':raise CaptureFailure('WRITER_STILL_RUNNING')
        if self.running:raise CaptureFailure('WRITER_STILL_RUNNING')
    def dump(self,out):
        self.trace.append('dump')
        if self.fail=='dump':raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
        out.write(b'synthetic-dump')
    def check_dump(self,path):self.trace.append('catalog')
    def capture(self,role,out):
        if self.fail==role:raise CaptureFailure('CAPTURE_INTERRUPTED')
        self.trace.append(role);out.write(b'synthetic-'+role.encode())
    def assert_identity(self,original):pass
    def start_writer(self,service):self.trace.extend(['restore-pg','start-nextcloud-maintained']);self.running=True
    def assert_original_services(self,original):
        if not self.running:raise CaptureFailure('WRITER_STATE_NOT_RESTORED')


class Hooks:
    def __init__(self,trace,fail=None):self.trace,self.fail=trace,fail
    def before(self,journal):
        self.trace.append('inventory-before')
        if self.fail=='before':raise CaptureFailure('FENCE_INVENTORY_CHANGED')
        return {'synthetic':True}
    def after(self,journal):
        self.trace.append('inventory-after')
        if self.fail=='after':raise CaptureFailure('FENCE_INVENTORY_CHANGED')
        return {'synthetic':True}


class Sequence(unittest.TestCase):
    def invoke(self,fail=None,initial=False):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name),synthetic=True);self.trace=[]
        backend=Backend(self.trace,initial,fail);self.backend=backend
        coordinator=object.__new__(DrainedCoordinator)
        def activate(window,b,original):b.assert_quiescent(original);self.trace.append('install-s3-deny')
        def restore(window,b,original):b.assert_quiescent(original);self.trace.append('restore-s3-policy')
        coordinator.activate_after_drain=activate;coordinator.restore_before_reopening=restore
        live=LiveCapture(self.store,backend,Hooks(self.trace,fail),coordinator,Mock())
        return live.start()
    def test_first_capture_order(self):
        result=self.invoke();self.assertFalse(result['backup_complete'])
        expected=['maintenance-True','drain-http','reject-new-pg','install-s3-deny','inventory-before','dump','catalog',
            'configuration','local_files','recovery_parameters','inventory-after','restore-s3-policy','restore-pg',
            'start-nextcloud-maintained','maintenance-False']
        self.assertEqual(self.trace,expected)
        journal=self.store.read(result['operation_id']+'/journal.json')
        self.assertEqual(journal['status'],'local_candidate_only');self.assertTrue(journal['service_state_restored'])
    def test_prior_maintenance_preserved(self):
        self.invoke(initial=True);self.assertTrue(self.backend.maint)
        self.assertNotIn('maintenance-False',self.trace)
    def test_dump_interrupted_does_not_unfence(self):self.failure('dump')
    def test_before_inventory_failure_does_not_unfence(self):self.failure('before')
    def test_after_inventory_failure_does_not_unfence(self):self.failure('after')
    def test_configuration_failure_does_not_unfence(self):self.failure('configuration')
    def test_local_files_failure_does_not_unfence(self):self.failure('local_files')
    def test_parameters_failure_does_not_unfence(self):self.failure('recovery_parameters')
    def test_writer_failure_does_not_install_fence(self):
        self.failure('writer');self.assertNotIn('install-s3-deny',self.trace)
    def failure(self,where):
        with self.assertRaisesRegex(CaptureFailure,'^LIVE_CAPTURE_RECONCILIATION_REQUIRED$'):self.invoke(where)
        self.assertNotIn('restore-s3-policy',self.trace);self.assertNotIn('restore-pg',self.trace)
        self.assertNotIn('maintenance-False',self.trace)
        active=self.store.read('active.json');self.assertFalse(active['terminal'])
        journal=self.store.read(active['operation_id']+'/journal.json')
        self.assertEqual(journal['status'],'incomplete');self.assertFalse(journal['service_state_restored'])


if __name__=='__main__':unittest.main()
