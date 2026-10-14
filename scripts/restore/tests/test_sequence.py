"""Durable synthetic journals and downloads; mocks cannot prove live restore."""
import copy
import io
from pathlib import Path
import tempfile
import unittest

from scripts.restore.sequence import execute_candidate,STEPS
from scripts.restore.downloads import download_selected
from scripts.backup.capture.storage import Store
from scripts.backup.sealing.common import SealFailure
from scripts.restore.tests import test_selection as fixtures

OP='20261011T120000Z-22222222-2222-4333-8444-555555555555'


class Sequence(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.Selection();fixture.setUp()
        self.selected=fixture.selector.select('specific',fixtures.BID);self.reader=fixture.reader
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name,synthetic=True)
        self.target=MockDestination(self.selected.bundle['manifest']['source'])
    def run_sequence(self):
        return execute_candidate(self.store,OP,self.selected,self.target,'i-source','fixture-state')
    def journal(self):return self.store.read(OP+'/restore-journal.json')
    def test_complete_mock_never_proves_restore(self):
        r=self.run_sequence();self.assertEqual(self.target.trace,list(STEPS))
        self.assertEqual(self.journal()['phase'],'candidate_ready')
        self.assertFalse(r['restore_proven']);self.assertFalse(r['destroy_authorized'])
    def test_second_execution_does_not_reinstall(self):
        self.run_sequence();before=self.target.trace.copy();r=self.run_sequence()
        self.assertEqual(self.target.trace,before);self.assertEqual(r['status'],'existing_candidate_revalidated')
    def test_failure_at_each_step_is_not_promoted(self):
        for step in STEPS:
            with self.subTest(step=step),tempfile.TemporaryDirectory() as temp:
                self.store=Store(temp,synthetic=True);self.target=MockDestination(self.selected.bundle['manifest']['source']);self.target.fail=step
                with self.assertRaisesRegex(SealFailure,'^RESTORE_STEP_FAILED$'):self.run_sequence()
                j=self.journal();self.assertEqual(j['phase'],'intervention_required');self.assertFalse(j['restore_proven'])
                self.assertEqual(j['completed_steps'],list(STEPS[:STEPS.index(step)]))
                self.assertNotIn('synthetic-private',str(j))
    def test_interrupted_sequence_cannot_automatically_retry(self):
        self.target.fail=STEPS[2]
        with self.assertRaises(SealFailure):self.run_sequence()
        self.target.fail=None
        with self.assertRaisesRegex(SealFailure,'^RESTORE_INTERRUPTED_REQUIRES_RECONCILIATION$'):self.run_sequence()
    def test_origin_target_rejected_before_journal(self):
        self.target.identity['instance']='i-source'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_IDENTITY_REJECTED$'):self.run_sequence()
        self.assertFalse((self.store.root/OP).exists())
    def test_state_bucket_rejected(self):
        self.target.identity['data_bucket']='fixture-state'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_IDENTITY_REJECTED$'):self.run_sequence()
    def test_nonempty_target_prevents_first_write(self):
        self.target.empty=False
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_NOT_EMPTY$'):self.run_sequence()
        self.assertFalse((self.store.root/OP).exists())
    def test_lease_loss_preserves_intervention(self):
        self.target.control=False
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_CONTROL_LOST$'):self.run_sequence()
        self.assertEqual(self.journal()['phase'],'intervention_required');self.assertEqual(self.target.trace,[])
    def test_existing_journal_different_backup_is_rejected(self):
        self.run_sequence();self.selected=copy.deepcopy(self.selected)
        object.__setattr__(self.selected,'manifest_sha256','f'*64)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_JOURNAL_BINDING_MISMATCH$'):self.run_sequence()
    def test_download_exact_versions_to_protected_synthetic_stage(self):
        directory=self.store.operation(OP);directory.mkdir()
        result=download_selected(self.reader,self.selected,self.store,OP)
        self.assertEqual(set(result),set(self.selected.bundle['manifest']['artifacts']))
        for role,a in self.selected.bundle['manifest']['artifacts'].items():
            name=a['key'].rsplit('/',1)[1];p=directory/name
            self.assertEqual(p.stat().st_size,a['size_bytes']);self.assertEqual(result[role]['plaintext_sha256'],a['sha256'])
    def test_corrupt_download_preserves_partial_without_success(self):
        directory=self.store.operation(OP);directory.mkdir()
        a=self.selected.bundle['manifest']['artifacts']['database'];self.reader.bodies[(a['key'],a['version_id'])]=b'x'*a['size_bytes']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HASH_MISMATCH$'):
            download_selected(self.reader,self.selected,self.store,OP)
        self.assertFalse((directory/'database.dump').exists())
        self.assertTrue((directory/'database.dump.partial').exists())


class MockDestination:
    def __init__(self,source):
        self.identity={'account':source['account_id'],'region':source['region'],'instance':'i-target','data_bucket':'fixture-restore'}
        self.trace=[];self.fail=None;self.empty=True;self.control=True
    def inspect_target(self):return self.identity.copy()
    def assert_empty_and_exclusive(self):
        if not self.empty:raise SealFailure('RESTORE_TARGET_NOT_EMPTY')
    def assert_identity_and_control(self,identity):
        if identity!=self.identity or not self.control:raise SealFailure('RESTORE_TARGET_CONTROL_LOST')
    def __getattr__(self,name):
        if name not in STEPS:raise AttributeError(name)
        def step(**kwargs):
            if kwargs.get('read_only'):return
            if name==self.fail:raise RuntimeError('synthetic-private')
            self.trace.append(name)
        return step


if __name__=='__main__':unittest.main()
