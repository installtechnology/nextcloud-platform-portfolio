"""Synthetic resume journals/adapters only; no operational recovery authorization."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from scripts.restore.resume import ResumeApproval,validate,CHECKPOINT
from scripts.restore.sequence import execute_candidate,STEPS
from scripts.restore.tests.test_sequence import MockDestination,OP
from scripts.restore.tests import test_selection as fixtures
from scripts.backup.capture.storage import Store
from scripts.backup.sealing.common import SealFailure
import scripts.restore.resume as module

BOOT='11111111-2222-4333-8444-555555555555'

class Resume(unittest.TestCase):
    def setUp(self):
        f=fixtures.Selection();f.setUp();self.selected=f.selector.select('specific',fixtures.BID)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        (Path(self.temp.name)/'stage').mkdir(mode=0o700);(Path(self.temp.name)/'shared').mkdir(mode=0o700)
        self.store=Store(Path(self.temp.name)/'stage',synthetic=True)
        self.shared=Store(Path(self.temp.name)/'shared',synthetic=True)
        self.target=MockDestination(self.selected.bundle['manifest']['source']);self.target.identity['instance']='i-'+'2'*17
        self.target.fail=STEPS[2]
        with self.assertRaises(SealFailure):self.execute()
        self.target.fail=None;self.target.revalidate_pre_database=Mock()
        self.store.write(OP+'/exec-journal.json',{'version':1,'operation_id':OP,'boot_id':BOOT,
            'execs':[{'phase':'finished','exit_code':0} for _ in range(3)]})
        self.store.write(OP+'/host-command.json',{'phase':'finished'})
        self.shared.write('deploy-lease.json',{'kind':'deploy-lease','version':1,'owner':'100:1','phase':'intervention_required'})
        j=self.store.read(OP+'/restore-journal.json')
        self.doc={'version':1,'kind':'restore-pre-database-resume','binding':j['binding'],'commit':'a'*40,
            'run_owner':'200:1','previous_owner':'100:1','boot_id':BOOT,
            'journal_sha256':self.digest(self.store.root/OP/'restore-journal.json'),
            'exec_journal_sha256':self.digest(self.store.root/OP/'exec-journal.json'),
            'host_command_sha256':self.digest(self.store.root/OP/'host-command.json'),
            'lease_sha256':self.digest(self.shared.root/'deploy-lease.json'),
            'tree_sha256':{name:'a'*64 for name in ('configuration','html','runtime')}}
        self.approval=ResumeApproval(validate(copy.deepcopy(self.doc)),'b'*64)
        self.addCleanup(patch.stopall)
        patch.object(ResumeApproval,'assert_unchanged',return_value=None).start()
        patch.object(module,'current_boot',return_value=BOOT).start()
    def digest(self,p):return hashlib.sha256(p.read_bytes()).hexdigest()
    def execute(self,resume=None):return execute_candidate(self.store,OP,self.selected,self.target,'i-source','fixture-state',resume)
    def checkpoint(self):self.approval.checkpoint(self.store,OP,self.store.read(OP+'/restore-journal.json'))
    def claim(self):self.approval.claim(self.store,self.shared,'200:1')
    def test_default_still_rejects_interruption(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_INTERRUPTED_REQUIRES_RECONCILIATION$'):self.execute()
    def test_boolean_is_not_authorization(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_REQUIRED$'):self.execute(True)
    def test_valid_checkpoint_does_not_mutate_journal(self):
        before=self.digest(self.store.root/OP/'restore-journal.json');self.checkpoint()
        self.assertEqual(before,self.digest(self.store.root/OP/'restore-journal.json'))
    def test_claim_is_durable_preserves_previous_journal_and_lease(self):
        before=(self.store.root/OP/'restore-journal.json').read_bytes();lease=(self.shared.root/'deploy-lease.json').read_bytes()
        self.claim();self.approval.assert_claimed(self.store,OP)
        self.assertEqual((self.store.root/OP/'resume-journal-before.json').read_bytes(),before)
        self.assertEqual((self.store.root/OP/'resume-lease-before.json').read_bytes(),lease)
        self.assertEqual(self.shared.read('deploy-lease.json')['owner'],'200:1')
        self.assertEqual(self.shared.read('deploy-lease.json')['phase'],'held')
    def test_duplicate_claim_rejected_before_lease_change(self):
        self.claim();before=self.shared.read('deploy-lease.json')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_ALREADY_CLAIMED$'):self.claim()
        self.assertEqual(self.shared.read('deploy-lease.json'),before)
    def test_wrong_run_owner(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_OWNER_REJECTED$'):
            self.approval.claim(self.store,self.shared,'201:1')
    def test_previous_lease_owner_mismatch(self):
        old=self.shared.read('deploy-lease.json');old['owner']='999:1';self.shared.write('deploy-lease.json',old)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_LEASE_REJECTED$'):self.claim()
        self.assertFalse((self.store.root/OP/'resume-claim.json').exists())
    def test_lease_bytes_change_even_if_same_semantics(self):
        p=self.shared.root/'deploy-lease.json';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_LEASE_REJECTED$'):self.claim()
    def test_new_boot_blocks_claim(self):
        with patch.object(module,'current_boot',return_value='other'),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_BOOT_REJECTED$'):self.claim()
    def test_journal_evidence_change(self):
        p=self.store.root/OP/'restore-journal.json';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_EVIDENCE_CHANGED$'):self.checkpoint()
    def test_any_existing_database_fence_rejected(self):
        (self.store.root/OP/'postgres-fence.json').write_text('{}')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_CHECKPOINT_REJECTED$'):self.checkpoint()
    def test_claim_required_before_sequence(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_CLAIM_REQUIRED$'):self.execute(self.approval)
        self.target.revalidate_pre_database.assert_not_called()
    def test_valid_continuation_skips_completed_writes(self):
        self.claim();before=self.target.trace.copy();result=self.execute(self.approval)
        self.target.revalidate_pre_database.assert_called_once_with(self.approval)
        self.assertEqual(self.target.trace,before+list(STEPS[2:]))
        self.assertEqual(self.store.read(OP+'/restore-journal.json')['completed_steps'],list(STEPS))
        self.assertFalse(result['restore_proven']);self.assertFalse(result['destroy_authorized'])
    def test_failed_revalidation_never_starts_database_restore(self):
        self.claim();self.target.revalidate_pre_database.side_effect=SealFailure('RESTORE_RESUME_DATABASE_NOT_EMPTY')
        before=self.target.trace.copy()
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_DATABASE_NOT_EMPTY$'):self.execute(self.approval)
        self.assertEqual(self.target.trace,before)
        self.assertEqual(self.store.read(OP+'/restore-journal.json')['phase'],'intervention_required')
    def test_different_commit_or_run_rejected(self):
        request={'owner':'200:1','commit':'a'*40};c=self.doc['binding']['destination']
        self.approval.assert_request(request,self.selected,c)
        for key,value in [('owner','200:2'),('commit','c'*40)]:
            with self.subTest(key=key),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_REQUEST_REJECTED$'):
                self.approval.assert_request({**request,key:value},self.selected,c)
    def test_unknown_document_fields_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_INVALID$'):validate({**self.doc,'force':True})
    def test_missing_document_fields_rejected(self):
        d=copy.deepcopy(self.doc);del d['tree_sha256']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_INVALID$'):validate(d)
    def test_same_owner_cannot_approve_replay(self):
        d=copy.deepcopy(self.doc);d['run_owner']=d['previous_owner']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_OWNER_REJECTED$'):validate(d)
    def test_tree_revalidation_checks_each_protected_tree(self):
        with patch.object(module,'tree_digest',return_value='a'*64) as digest:self.approval.assert_trees(self.store.root/OP)
        self.assertEqual(digest.call_count,3)
    def test_tree_change_rejected(self):
        with patch.object(module,'tree_digest',return_value='c'*64),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_FILES_CHANGED$'):
            self.approval.assert_trees(self.store.root/OP)
    def test_restore_after_fencing_or_later_stage_is_not_resumable(self):
        j=self.store.read(OP+'/restore-journal.json');j['completed_steps'].append(STEPS[2]);self.store.write(OP+'/restore-journal.json',j)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_CHECKPOINT_REJECTED$'):self.checkpoint()
    def test_lease_write_failure_preserves_consumed_claim_and_old_lease(self):
        before=self.shared.read('deploy-lease.json')
        with patch.object(self.shared,'write',side_effect=OSError('synthetic-error')),self.assertRaises(OSError):self.claim()
        self.assertTrue((self.store.root/OP/'resume-claim.json').exists())
        self.assertEqual(self.shared.read('deploy-lease.json'),before)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_ALREADY_CLAIMED$'):self.claim()
    def control(self):
        from scripts.restore.control import RestoreControl
        c={**self.doc['binding']['destination'],'project':'fixture','backups_bucket':'fixture-backups','state_bucket':'fixture-state'}
        control=object.__new__(RestoreControl);control.owner=None;control.resume_authorization=None
        control.host=SimpleNamespace(config=c,identity=Mock());control.shared=self.shared;control.staging=self.store
        control.inspect=Mock(return_value=[])
        return control,c
    def test_resume_lease_released_only_after_successful_context(self):
        from scripts.restore import control as module_control
        control,c=self.control()
        with patch.object(module_control,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(module_control.runtime_config,'load',return_value=c):
            with control.hold('200:1',self.approval):
                self.assertEqual(self.shared.read('deploy-lease.json')['phase'],'held')
                control.assert_held()
        self.assertEqual(self.shared.read('deploy-lease.json')['phase'],'released')
        self.assertIsNone(control.resume_authorization)
    def test_failure_during_resume_preserves_intervention_with_new_owner(self):
        from scripts.restore import control as module_control
        control,c=self.control()
        with patch.object(module_control,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(module_control.runtime_config,'load',return_value=c):
            with self.assertRaisesRegex(SealFailure,'^RESTORE_SYNTHETIC_FAILURE$'):
                with control.hold('200:1',self.approval):raise SealFailure('RESTORE_SYNTHETIC_FAILURE')
        lease=self.shared.read('deploy-lease.json')
        self.assertEqual((lease['owner'],lease['phase']),('200:1','intervention_required'))
    def test_orphan_blocks_before_resume_claim_or_lease_transition(self):
        from scripts.restore import control as module_control
        from scripts.backup.capture.storage import CaptureFailure
        control,c=self.control();control.inspect.return_value=['ORPHAN']
        before=self.shared.read('deploy-lease.json')
        with patch.object(module_control,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(module_control.runtime_config,'load',return_value=c):
            with self.assertRaisesRegex(CaptureFailure,'^ORPHANED_CAPTURE_PRESENT$'):
                with control.hold('200:1',self.approval):self.fail('must not enter')
        self.assertEqual(self.shared.read('deploy-lease.json'),before)
        self.assertFalse((self.store.root/OP/'resume-claim.json').exists())
    def test_root_loader_rejects_nonroot_before_reading_file(self):
        with patch.object(module,'os',SimpleNamespace(name='posix',geteuid=lambda:1000)),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_ROOT_REQUIRED$'):
            module.load()

class DatabaseResume(unittest.TestCase):
    """Adapter boundary tests; synthetic observations do not prove a live DB."""
    def setUp(self):
        from scripts.restore.host_runtime import HostRuntime
        self.r=object.__new__(HostRuntime);self.r.guard=Mock();self.r.verify_resume_files=Mock()
        self.r.supervisor=Mock();self.r.operation=OP;self.r.api=Mock();self.r.host=Mock();self.r.resumed_database=None
        self.nodes={'postgres':{'Id':'a'*64,'State':{'Health':{'Status':'healthy'}}},
                    'redis':{'Id':'b'*64,'State':{'Health':{'Status':'healthy'}}}}
        self.r.containers=Mock(return_value=self.nodes)
        self.p=SimpleNamespace(database={'database':'nextcloud','owner':'nextcloud','extensions':[]})
        self.pg=SimpleNamespace(assert_roles=Mock(),inspect_database=Mock());self.pg.inspect_database.return_value={'server_major':17,'database':'nextcloud',
            'owner':'nextcloud','tables':[],'sessions':[],'extensions':[]}
    def check(self):
        with patch('scripts.restore.postgres_adapter.PostgreSQLDestination',return_value=self.pg):
            self.r.revalidate_pre_database(self.p,{})
    def test_database_empty_roles_and_identity_revalidated_before_reuse(self):
        self.check();self.pg.assert_roles.assert_called_once_with(self.p)
        self.assertEqual(self.r.resumed_database,'a'*64)
        self.r.verify_resume_files.assert_called_once_with(self.p,{})
    def test_tables_or_other_sessions_block_without_writes(self):
        for key,value in [('tables',['existing']),('sessions',[42]),('server_major',16),('owner','other')]:
            before=copy.deepcopy(self.pg.inspect_database.return_value)
            self.pg.inspect_database.return_value[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_DATABASE_NOT_EMPTY$'):self.check()
            self.pg.assert_roles.assert_not_called();self.assertIsNone(self.r.resumed_database)
            self.pg.inspect_database.return_value=before
    def test_unhealthy_container_blocks_before_sql(self):
        self.nodes['postgres']['State']['Health']['Status']='unhealthy'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_CONTAINERS_REJECTED$'):self.check()
        self.pg.inspect_database.assert_not_called()
    def test_file_changes_block_before_sql(self):
        self.r.verify_resume_files.side_effect=SealFailure('RESTORE_RESUME_RUNTIME_CHANGED')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_RUNTIME_CHANGED$'):self.check()
        self.pg.inspect_database.assert_not_called()
    def test_invalid_roles_do_not_authorize_database_reuse(self):
        self.pg.assert_roles.side_effect=SealFailure('RESTORE_ROLES_REVIEW_REQUIRED')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_REVIEW_REQUIRED$'):self.check()
        self.assertIsNone(self.r.resumed_database)
    def test_existing_database_not_recreated_or_restarted(self):
        self.check();self.r.compose=Mock();self.r.wait_healthy=Mock(return_value=self.nodes)
        self.assertEqual(self.r.start_database(),'a'*64);self.r.compose.assert_not_called()
    def test_replaced_container_rejected_before_restore(self):
        self.check();self.r.compose=Mock();nodes=copy.deepcopy(self.nodes);nodes['postgres']['Id']='c'*64
        self.r.wait_healthy=Mock(return_value=nodes)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_DATABASE_CHANGED$'):self.r.start_database()
        self.r.compose.assert_not_called()

class ApprovalWait(unittest.TestCase):
    """Fake clock plus protected synthetic files; never real sleeping/dispatch."""
    def setUp(self):
        self.f=Resume();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.clock=0;self.loads=0
        self.request={'owner':'200:1','commit':'a'*40}
        self.config=self.f.doc['binding']['destination']
        self.addCleanup(patch.stopall)
        patch.object(module.time,'monotonic',side_effect=lambda:self.clock).start()
        patch.object(module.time,'sleep',side_effect=self.sleep).start()
        patch.object(module,'_APPROVAL_WAIT_SECONDS',3).start()
    def sleep(self,seconds):self.clock+=seconds
    def run_wait(self,loader):
        with patch.object(module,'load',side_effect=loader):
            return module.await_approval(self.f.store,self.f.selected,self.config,self.request)
    def test_arriving_exact_approval_is_accepted_without_lease_or_journal_write(self):
        before=self.f.digest(self.f.store.root/OP/'restore-journal.json');lease=self.f.shared.read('deploy-lease.json')
        result=self.run_wait(lambda:self.f.approval if self.clock>=1 else None)
        self.assertIs(result,self.f.approval);self.assertEqual(self.clock,1)
        self.assertEqual(before,self.f.digest(self.f.store.root/OP/'restore-journal.json'))
        self.assertEqual(self.f.shared.read('deploy-lease.json'),lease)
        self.assertFalse((self.f.store.root/OP/'resume-claim.json').exists())
    def test_timeout_leaves_old_lease_and_journal_intact(self):
        before=self.f.shared.read('deploy-lease.json')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_TIMEOUT$'):self.run_wait(lambda:None)
        self.assertEqual(self.clock,3);self.assertEqual(self.f.shared.read('deploy-lease.json'),before)
        self.assertEqual(self.f.store.read(OP+'/restore-journal.json')['phase'],'intervention_required')
    def test_approval_for_other_run_rejected_without_waiting(self):
        self.request['owner']='999:1'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_REQUEST_REJECTED$'):self.run_wait(lambda:self.f.approval)
        self.assertEqual(self.clock,0)
    def test_journal_changed_during_wait_rejected(self):
        def sleep(seconds):
            self.clock+=seconds;p=self.f.store.root/OP/'restore-journal.json';p.write_bytes(p.read_bytes()+b'\n')
        with patch.object(module.time,'sleep',side_effect=sleep),self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_EVIDENCE_CHANGED$'):
            self.run_wait(lambda:None)
    def test_malformed_or_unsafe_approval_never_polled_until_accepted(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_UNSAFE$'):
            self.run_wait(lambda:(_ for _ in ()).throw(SealFailure('RESTORE_RESUME_APPROVAL_UNSAFE')))
        self.assertEqual(self.clock,0)
    def test_already_consumed_authorization_does_not_wait(self):
        self.f.claim()
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_ALREADY_CLAIMED$'):self.run_wait(lambda:None)
        self.assertEqual(self.clock,0)
    def test_partial_sql_or_later_checkpoint_is_not_waitable(self):
        j=self.f.store.read(OP+'/restore-journal.json');j['completed_steps'].append(STEPS[2]);self.f.store.write(OP+'/restore-journal.json',j)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_CHECKPOINT_REJECTED$'):self.run_wait(lambda:None)
    def test_fresh_target_does_not_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh=Store(tmp,synthetic=True)
            with patch.object(module,'load',return_value=None):
                self.assertIsNone(module.await_approval(fresh,self.f.selected,self.config,self.request))
        self.assertEqual(self.clock,0)
    def test_completed_candidate_does_not_wait(self):
        j=self.f.store.read(OP+'/restore-journal.json');j['phase']='candidate_ready';j['completed_steps']=list(STEPS)
        self.f.store.write(OP+'/restore-journal.json',j)
        self.assertIsNone(self.run_wait(lambda:None));self.assertEqual(self.clock,0)

@unittest.skipUnless(__import__('os').name=='posix' and getattr(__import__('os'),'geteuid',lambda:1)()==0,
                     'actual POSIX root permissions require isolated Linux qualification')
class RootLoader(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.root.chmod(0o700);self.path=self.root/'resume-approval.json'
        self.doc={'version':1,'kind':'restore-pre-database-resume','binding':{'operation_id':OP,'backup_id':fixtures.BID,
            'manifest_sha256':'a'*64,'completion_version_id':'fixture-version',
            'destination':{'account':'000000000000','region':'us-east-1','instance':'i-'+'2'*17,'data_bucket':'fixture-destination'}},
            'commit':'a'*40,'run_owner':'200:1','previous_owner':'100:1','boot_id':BOOT,
            **{k:'a'*64 for k in ('journal_sha256','exec_journal_sha256','host_command_sha256','lease_sha256')},
            'tree_sha256':{k:'a'*64 for k in ('configuration','html','runtime')}}
        self.path.write_text(json.dumps(self.doc));self.path.chmod(0o600)
    def load(self):
        with patch.object(module,'APPROVAL_ROOT',self.root):return module.load()
    def test_real_root_private_file_is_accepted(self):
        approval=self.load();self.assertEqual(approval.document,self.doc)
        self.assertEqual(approval.approval_sha256,hashlib.sha256(self.path.read_bytes()).hexdigest())
    def test_real_world_readable_approval_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_UNSAFE$'):self.load()
    def test_real_symlink_approval_rejected(self):
        target=self.root/'private-source.json';self.path.rename(target);self.path.symlink_to(target)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RESUME_APPROVAL_UNSAFE$'):self.load()

if __name__=='__main__':unittest.main()
