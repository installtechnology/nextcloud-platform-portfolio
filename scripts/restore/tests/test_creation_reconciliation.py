"""Synthetic administrative recovery; no AWS/SSH/workflow or unlock IO."""
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from scripts.restore.tests.test_reprovision import config,S3,preserved_state,preserved_plan,Operations
from scripts.restore.reprovision import execute,retained_state
from scripts.restore.creation_lease import CreationLease,encoded,digest
from scripts.restore.reprovision_grant import issue,reconcile_before_apply
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import SealFailure


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.c=config();self.owner='123:1';self.commit='a'*40;self.s3=S3(self.c)
        self.lease=self.make(self.owner,self.commit)
        self.s3.add(self.lease.grant_key,{'version':1,'kind':'recovery-host-creation','owner':self.owner,
            'commit':self.commit,'configuration_sha256':digest(self.c),'configuration':self.c})
        self.lease.acquire()
        self.lease.put({**self.lease.record,'phase':'intervention_required'})
        self.old=copy.deepcopy(self.lease.record);self.history=copy.deepcopy(self.s3.data[self.lease.key])
        self.sts=Mock();self.sts.get_caller_identity.return_value={'Account':self.c['account'],
            'Arn':f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-issuer/approved"}
        self.raw=encoded(preserved_state(self.c));self.state_version='state-v1'
        self.state_key=f"states/{self.c['account']}/{self.c['project']}/restore/restore/terraform.tfstate"
        original=self.s3.get_object
        def get(**kw):
            if kw['Key']==self.state_key:
                return {'Body':io.BytesIO(self.raw),'VersionId':self.state_version,
                        'ContentLength':len(self.raw),'ServerSideEncryption':'AES256'}
            return original(**kw)
        self.s3.get_object=get
        self.observation={'account':self.c['account'],
            'workflow':{'repository':self.c['repository'],'run_id':123,'attempt':1,'commit':self.commit,
                        'status':'completed','conclusion':'failure'},
            'runtime':{k:[] for k in ('instances','vpcs','subnets','internet_gateways','route_tables',
                        'security_groups','key_pairs','network_interfaces','volumes','host_role','host_profile')},
            'state':{'version_id':self.state_version,'bytes':self.raw}}
        self.observer=Mock();self.observer.collect.return_value=self.observation
        self.expected={'lease_version_id':self.lease.version,'lease_sha256':digest(self.old),
                       'state_version_id':self.state_version,'state_sha256':hashlib.sha256(self.raw).hexdigest()}
        self.key=self.lease.prefix+'grants/reconciliations/123-1.json';self.calls=len(self.s3.calls)
    def make(self,owner,commit):
        return CreationLease(self.s3,self.c['account'],self.c['state_bucket'],
            f"control/reprovision/{self.c['account']}/{self.c['project']}/lab/",owner,commit,self.c)
    def reconcile(self):
        return reconcile_before_apply(self.s3,self.sts,json.dumps(self.c),self.owner,self.commit,self.expected,self.observer)
    def new(self):
        l=self.make('124:1','b'*40)
        issue(self.s3,self.sts,json.dumps(self.c),l.owner,l.commit)
        return l
    def code(self,code,fn):
        with self.assertRaises((CaptureFailure,SealFailure)) as caught:fn()
        self.assertEqual(caught.exception.code,code)
    def test_receipt_preserves_failed_journal_without_claiming_handoff(self):
        r=self.reconcile();self.assertEqual(r['code'],'CREATION_ABORT_BEFORE_APPLY_RECONCILED')
        self.assertFalse(r['handoff_verified']);self.assertFalse(r['backup_complete']);self.assertFalse(r['restore_proven'])
        self.assertEqual(self.s3.data[self.lease.key],self.history)
        self.assertEqual(self.s3.calls[-1]['Key'],self.key);self.assertEqual(self.s3.calls[-1]['IfNoneMatch'],'*')
    def test_new_run_can_acquire_only_after_receipt_and_new_grant(self):
        self.reconcile();l=self.new();l.acquire();self.assertEqual(l.record['owner'],'124:1')
        self.assertEqual(l.record['phase'],'held');self.assertIsNone(l.record['host']);self.assertFalse(l.record['handoff_verified'])
        self.assertEqual(self.s3.data[self.lease.key][:-1],self.history)
    def test_missing_receipt_blocks_new_grant(self):
        self.code('CREATION_RECONCILIATION_REQUIRED',self.new)
    def test_wrong_issuer_cannot_reconcile(self):
        self.sts.get_caller_identity.return_value['Arn']=f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-lab-reprovision/executor"
        self.code('CREATION_ADMINISTRATIVE_ISSUER_REQUIRED',self.reconcile)
        self.assertEqual(len(self.s3.calls),self.calls)
    def test_unfinished_or_successful_workflow_blocks(self):
        for field,value in [('status','queued'),('status','in_progress'),('conclusion','success'),('commit','b'*40),('run_id',124)]:
            with self.subTest(field=field):
                saved=copy.deepcopy(self.observation['workflow']);self.observation['workflow'][field]=value
                self.code('CREATION_RECONCILIATION_WORKFLOW_UNVERIFIED',self.reconcile)
                self.observation['workflow']=saved
    def test_any_residual_runtime_blocks(self):
        for key in self.observation['runtime']:
            with self.subTest(key=key):
                self.observation['runtime'][key]=['residual']
                self.code('CREATION_RECONCILIATION_RUNTIME_PRESENT',self.reconcile)
                self.observation['runtime'][key]=[]
    def test_boolean_absence_flags_are_not_evidence(self):
        self.observation['runtime']['instances']=True
        self.code('CREATION_RECONCILIATION_RUNTIME_PRESENT',self.reconcile)
    def test_wrong_state_version_blocks_receipt(self):
        self.observation['state']['version_id']='different'
        self.code('CREATION_RECONCILIATION_STATE_CHANGED',self.reconcile)
    def test_wrong_state_bytes_block_receipt(self):
        self.observation['state']['bytes']=self.raw+b' '
        self.code('CREATION_RECONCILIATION_STATE_CHANGED',self.reconcile)
    def test_wrong_lease_binding_blocks_receipt(self):
        self.expected['lease_sha256']='f'*64
        self.code('CREATION_RECONCILIATION_BINDING_REJECTED',self.reconcile)
    def test_bound_host_never_uses_before_apply_recovery(self):
        self.lease.put({**self.old,'host':{'account':self.c['account'],'region':self.c['region'],'instance':'i-'+'3'*17}})
        self.expected.update(lease_version_id=self.lease.version,lease_sha256=digest(self.lease.record))
        self.code('CREATION_RECONCILIATION_BINDING_REJECTED',self.reconcile)
    def test_concurrent_journal_change_blocks_receipt(self):
        def collect(*args):self.s3.add(self.lease.key,self.old);return self.observation
        self.observer.collect.side_effect=collect
        self.code('CREATION_AUTHORITY_LOST',self.reconcile)
        self.assertNotIn(self.key,self.s3.data)
    def test_duplicate_receipt_is_never_overwritten(self):
        self.reconcile();history=copy.deepcopy(self.s3.data[self.key])
        self.code('CREATION_RECONCILIATION_ALREADY_EXISTS',self.reconcile)
        self.assertEqual(self.s3.data[self.key],history)
    def test_timeout_before_receipt_preserves_blocked_journal(self):
        self.s3.fail='before';self.code('CREATION_S3_OUTCOME_UNKNOWN',self.reconcile)
        self.assertEqual(self.s3.data[self.lease.key],self.history);self.assertNotIn(self.key,self.s3.data)
    def test_timeout_after_receipt_does_not_release_journal_or_retry(self):
        self.s3.fail='after';self.code('CREATION_S3_OUTCOME_UNKNOWN',self.reconcile)
        self.assertEqual(self.s3.data[self.lease.key],self.history);self.assertEqual(len(self.s3.data[self.key]),1)
    def test_changed_receipt_binding_blocks_new_grant(self):
        self.reconcile();value=json.loads(self.s3.data[self.key][0][1]);value['binding']['lease_sha256']='f'*64
        vid=self.s3.data[self.key][0][0];self.s3.data[self.key][0]=(vid,encoded(value))
        self.code('CREATION_RECONCILIATION_INVALID',self.new)
    def test_two_receipt_versions_block_new_grant(self):
        self.reconcile();self.s3.add(self.key,json.loads(self.s3.data[self.key][0][1]))
        self.code('CREATION_RECONCILIATION_REQUIRED',self.new)
    def test_state_changed_before_acquire_blocks_without_lease_write(self):
        self.reconcile();l=self.new();self.raw+=b' '
        self.code('CREATION_RECONCILIATION_STATE_CHANGED',l.acquire)
        self.assertEqual(self.s3.data[self.lease.key],self.history)
    def test_failed_run_grant_is_not_reused(self):
        self.reconcile();self.code('CREATION_GRANT_ALREADY_USED',self.make(self.owner,self.commit).acquire)
    def test_untrusted_receipt_fields_are_rejected(self):
        self.reconcile();vid,raw=self.s3.data[self.key][0];value=json.loads(raw);value['force_unlock']=True
        self.s3.data[self.key][0]=(vid,encoded(value))
        self.code('CREATION_RECONCILIATION_INVALID',self.new)
    def test_observation_failure_is_sanitized_and_writes_nothing(self):
        self.observer.collect.side_effect=RuntimeError('synthetic-sensitive-value')
        self.code('CREATION_RECONCILIATION_OBSERVATION_FAILED',self.reconcile)
        self.assertEqual(len(self.s3.calls),self.calls)
    def test_state_changes_before_apply_stop_execution(self):
        self.reconcile();l=self.new();ops=Operations(self.c,l.owner)
        state=json.loads(self.raw);ops.retained=retained_state(state,self.c)
        def plan(c,lease):ops.stage('plan');return preserved_plan(c,state)
        ops.plan=plan
        def absence(c):
            ops.stage('absence')
            if ops.calls.count('absence')==2:self.raw+=b' '
        ops.assert_absent=absence
        self.code('CREATION_RECONCILIATION_STATE_CHANGED',lambda:execute(l,ops))
        self.assertNotIn('apply',ops.calls);self.assertEqual(l.record['phase'],'intervention_required')


if __name__=='__main__':unittest.main()
