import copy
import json
import unittest
from scripts.backup.operational.tests import test_fencing as f
from scripts.backup.operational.policy_ownership import PolicyOwner, initial_policy, owned_policy, guard_statement, validate_persistent_plan, assert_policy_owner_clear, validate_adoption_forget_plan
from scripts.backup.operational.registered_authority import RegisteredAuthority
from scripts.backup.capture.storage import CaptureFailure


class API(f.API):
    def read_policy(self, scope):
        if self.raw is None: raise CaptureFailure('DATA_POLICY_ABSENT')
        return self.raw
    def put_policy(self, scope, document):
        self.writes.append(copy.deepcopy(document))
        self.raw = json.dumps(document)
        if self.mode == 'after': raise CaptureFailure('FENCE_API_OUTCOME_UNKNOWN')


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.store=f.Store();self.api=API(self.store)
        self.owner=PolicyOwner(self.store,self.api,f.SCOPE,lambda:None)
    def reject(self, code, fn):
        with self.assertRaises(CaptureFailure) as e: fn()
        self.assertEqual(e.exception.code,code)
    def adopt(self):
        with self.owner.hold(f.OP) as lease: return self.owner.adopt(lease)
    def test_existing_policy_preserved(self):
        self.adopt()
        self.assertEqual(json.loads(self.api.raw)['Statement'][:-1], f.ORIGINAL['Statement'])
        self.assertFalse(assert_policy_owner_clear(self.store).get('backup_complete',False))
    def test_initial_policy_only_on_proven_absence(self):
        self.api.raw=None
        with self.owner.hold(f.OP) as lease: self.owner.initialize(lease)
        self.assertEqual(json.loads(self.api.raw),initial_policy(f.SCOPE))
    def test_initializer_never_overwrites_existing(self):
        def run():
            with self.owner.hold(f.OP) as lease:self.owner.initialize(lease)
        self.reject('POLICY_ALREADY_EXISTS',run);self.assertFalse(self.api.writes)
    def test_foreign_owner_not_replaced(self):
        policy=owned_policy(self.api.raw,f.SCOPE)
        policy['Statement'][-1]['Condition']={}
        self.api.raw=json.dumps(policy)
        self.reject('POLICY_OWNER_CONFLICT',self.adopt)
    def test_active_fence_blocks_adoption(self):
        self.store.write('fence-active.json',{'operation_id':f.OP,'terminal':False})
        self.reject('ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED',self.adopt)
    def test_pending_deploy_blocks_adoption(self):
        self.store.write('deploy-lease.json',{'phase':'held'})
        self.reject('DEPLOY_LEASE_RECOVERY_REQUIRED',self.adopt)
    def test_lost_ack_blocks_new_operation_until_reconciled(self):
        self.api.mode='after';self.reject('FENCE_API_OUTCOME_UNKNOWN',self.adopt)
        self.reject('POLICY_OWNER_RECONCILIATION_REQUIRED',self.adopt)
        self.api.mode=None;self.owner.reconcile(f.OP)
        self.assertEqual(len(self.api.writes),1)
    def test_third_party_change_preserved(self):
        self.api.mode='after';self.reject('FENCE_API_OUTCOME_UNKNOWN',self.adopt)
        policy=json.loads(self.api.raw);policy['Id']='legitimate-third-party'
        self.api.raw=json.dumps(policy)
        self.reject('FENCE_POLICY_CONCURRENT_CHANGE',lambda:self.owner.reconcile(f.OP))
        self.assertEqual(json.loads(self.api.raw),policy)
    def test_unregistered_authority_blocks(self):
        authority=RegisteredAuthority(self.store,self.api)
        self.reject('POLICY_OWNER_NOT_REGISTERED',lambda:authority.verify(f.SCOPE))
    def test_registered_authority_serializes(self):
        self.adopt();authority=RegisteredAuthority(self.store,self.api)
        with authority.hold(f.SCOPE,f.CAPTURE):
            authority.assert_held(f.SCOPE,f.CAPTURE)
            self.reject('POLICY_AUTHORITY_BUSY',lambda:authority.hold(f.SCOPE,f.OP).__enter__())
        self.reject('POLICY_AUTHORITY_LOST',lambda:authority.assert_held(f.SCOPE,f.CAPTURE))
    def test_guard_removed_blocks_authority(self):
        self.adopt();self.api.raw=json.dumps(f.ORIGINAL)
        self.reject('POLICY_OWNER_GUARD_MISSING',lambda:RegisteredAuthority(self.store,self.api).verify(f.SCOPE))
    def test_unexpected_identity_blocks(self):
        self.api.admin='arn:aws:iam::123456789012:role/backup'
        self.reject('FENCE_ADMIN_IDENTITY_MISMATCH',self.adopt)
    def test_missing_tls_cannot_be_adopted(self):
        self.api.raw=json.dumps({'Version':'2012-10-17','Statement':[{'Sid':'Other','Effect':'Allow','Action':'s3:GetBucketLocation','Principal':'*','Resource':'*'}]})
        self.reject('DATA_POLICY_TLS_GUARD_REQUIRED',self.adopt)
        self.assertFalse(self.api.writes)
    def test_lost_ack_journal_tampering_blocks_recovery(self):
        self.api.mode='after';self.reject('FENCE_API_OUTCOME_UNKNOWN',self.adopt)
        record=self.store.read(f.OP+'/policy-ownership.json')
        record['expected_hash']='0'*64
        self.store.write(f.OP+'/policy-ownership.json',record)
        self.api.mode=None
        self.reject('POLICY_OWNER_JOURNAL_INVALID',lambda:self.owner.reconcile(f.OP))
        self.assertEqual(len(self.api.writes),1)
    def test_registered_authority_has_no_implicit_release(self):
        self.adopt();authority=RegisteredAuthority(self.store,self.api)
        before=self.api.raw
        with self.assertRaises(RuntimeError):
            with authority.hold(f.SCOPE,f.CAPTURE):raise RuntimeError('synthetic interruption')
        self.assertEqual(self.api.raw,before)
        self.assertEqual(len(self.api.writes),1)


class PlanTests(unittest.TestCase):
    def plan(self, address, actions, resource_type='aws_s3_bucket_policy'):
        return {'format_version':'1.2','resource_changes':[{'address':address,'mode':'managed','type':resource_type,'change':{'actions':actions}}]}
    def test_forget_only_migration(self):validate_persistent_plan(self.plan('aws_s3_bucket_policy.tls',['forget']))
    def test_empty_new_plan(self):validate_persistent_plan({'format_version':'1.2','resource_changes':[]})
    def test_backup_policy_creation(self):validate_persistent_plan(self.plan('aws_s3_bucket_policy.backups_tls',['create']))
    def test_data_policy_mutations_all_rejected(self):
        for action in ('delete','create','update','no-op'):
            with self.subTest(action=action),self.assertRaisesRegex(CaptureFailure,'^DATA_POLICY_TERRAFORM_MUTATION_REJECTED$'):
                validate_persistent_plan(self.plan('aws_s3_bucket_policy.tls',[action]))
    def test_alias_policy_cannot_bypass(self):
        with self.assertRaisesRegex(CaptureFailure,'^DATA_POLICY_TERRAFORM_MUTATION_REJECTED$'):
            validate_persistent_plan(self.plan('module.alias.aws_s3_bucket_policy.data',['create']))
    def test_bucket_replacement_rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^PERSISTENT_DESTRUCTIVE_PLAN_REJECTED$'):
            validate_persistent_plan(self.plan('aws_s3_bucket.data',['delete','create'],'aws_s3_bucket'))
    def test_forget_is_bound_to_original_data_bucket(self):
        plan=self.plan('aws_s3_bucket_policy.tls',['forget'])
        plan['resource_changes'][0]['change']['before']={'bucket':'wrong-bucket'}
        with self.assertRaisesRegex(CaptureFailure,'^DATA_POLICY_STATE_SCOPE_MISMATCH$'):
            validate_persistent_plan(plan,('data-bucket','backups-bucket'))
        plan['resource_changes'][0]['change']['before']['bucket']='data-bucket'
        validate_persistent_plan(plan,('data-bucket','backups-bucket'))
    def test_backup_address_cannot_target_data_bucket(self):
        plan=self.plan('aws_s3_bucket_policy.backups_tls',['update'])
        plan['resource_changes'][0]['change'].update(before={'bucket':'data-bucket'},after={'bucket':'data-bucket'})
        with self.assertRaisesRegex(CaptureFailure,'^BACKUP_POLICY_BUCKET_SCOPE_MISMATCH$'):
            validate_persistent_plan(plan,('data-bucket','backups-bucket'))
    def test_backup_policy_requires_known_bucket(self):
        plan=self.plan('aws_s3_bucket_policy.backups_tls',['create'])
        plan['resource_changes'][0]['change']['after']={'bucket':None}
        with self.assertRaisesRegex(CaptureFailure,'^BACKUP_POLICY_BUCKET_SCOPE_MISMATCH$'):
            validate_persistent_plan(plan,('data-bucket','backups-bucket'))


class AdoptionDriftTests(unittest.TestCase):
    def setUp(self):
        store = f.Store(); api = API(store)
        owner = PolicyOwner(store, api, f.SCOPE, lambda: None)
        with owner.hold(f.OP) as lease: owner.adopt(lease)
        self.journal = assert_policy_owner_clear(store)
        self.previous, self.current = json.dumps(f.ORIGINAL), api.raw
        before = {'id': f.SCOPE.bucket, 'bucket': f.SCOPE.bucket,
                  'policy': self.previous, 'tags': {'Project': 'synthetic'}}
        after = {**before, 'policy': self.current}
        self.plan = {'format_version': '1.2', 'applyable': True,
            'resource_changes': [
                {'address': 'aws_s3_bucket_policy.tls', 'type': 'aws_s3_bucket_policy', 'mode': 'managed',
                 'change': {'actions': ['forget'], 'before': {'bucket': f.SCOPE.bucket}, 'after': None}},
                {'address': 'aws_s3_bucket.data', 'type': 'aws_s3_bucket', 'mode': 'managed',
                 'change': {'actions': ['no-op'], 'before': after, 'after': after}}],
            'resource_drift': [{'address': 'aws_s3_bucket.data', 'type': 'aws_s3_bucket', 'mode': 'managed',
                'change': {'actions': ['update'], 'before': before, 'after': after}}]}
    def validate(self):
        return validate_adoption_forget_plan(self.plan, f.SCOPE, 'synthetic-backups',
            self.journal, self.previous, self.current)
    def reject(self, code):
        with self.assertRaisesRegex(CaptureFailure, '^' + code + '$'): self.validate()
    def test_proved_computed_policy_drift_only(self):
        self.assertEqual(self.validate()['proved_adoption_drift'], 1)
    def test_pending_journal_rejected(self):
        self.journal['phase'] = 'adoption_intent'; self.reject('ADOPTION_JOURNAL_PROOF_REQUIRED')
    def test_foreign_journal_scope_rejected(self):
        self.journal['scope'] = {}; self.reject('ADOPTION_JOURNAL_PROOF_REQUIRED')
    def test_foreign_administrator_rejected(self):
        self.journal['identity']['role_arn'] = f.SCOPE.emergency_role
        self.reject('ADOPTION_JOURNAL_PROOF_REQUIRED')
    def test_before_hash_mismatch_rejected(self):
        self.journal['before_hash'] = '0'*64; self.reject('ADOPTION_POLICY_PROOF_MISMATCH')
    def test_expected_hash_mismatch_rejected(self):
        self.journal['expected_hash'] = '0'*64; self.reject('ADOPTION_POLICY_PROOF_MISMATCH')
    def test_live_policy_change_rejected(self):
        p = json.loads(self.current); p['Id'] = 'foreign'; self.current = json.dumps(p)
        self.reject('ADOPTION_POLICY_PROOF_MISMATCH')
    def test_plan_previous_policy_mismatch_rejected(self):
        self.plan['resource_drift'][0]['change']['before']['policy'] = self.current
        self.reject('ADOPTION_DRIFT_POLICY_MISMATCH')
    def test_plan_current_policy_mismatch_rejected(self):
        self.plan['resource_drift'][0]['change']['after']['policy'] = self.previous
        self.reject('ADOPTION_DRIFT_POLICY_MISMATCH')
    def test_second_drift_rejected(self):
        self.plan['resource_drift'] *= 2; self.reject('ADOPTION_DRIFT_NOT_EXCLUSIVE')
    def test_no_proved_drift_rejected(self):
        self.plan['resource_drift'] = []; self.reject('ADOPTION_DRIFT_NOT_EXCLUSIVE')
    def test_wrong_drift_resource_rejected(self):
        self.plan['resource_drift'][0]['address'] = 'aws_s3_bucket.backups'
        self.reject('ADOPTION_DRIFT_SCOPE_MISMATCH')
    def test_wrong_bucket_rejected(self):
        self.plan['resource_drift'][0]['change']['before']['bucket'] = 'foreign'
        self.reject('ADOPTION_DRIFT_SCOPE_MISMATCH')
    def test_tag_drift_rejected(self):
        self.plan['resource_drift'][0]['change']['before']['tags'] = {'Project': 'foreign'}
        self.reject('ADOPTION_DRIFT_OTHER_ATTRIBUTE_REJECTED')
    def test_added_attribute_rejected(self):
        self.plan['resource_drift'][0]['change']['before']['force_destroy'] = True
        self.reject('ADOPTION_DRIFT_OTHER_ATTRIBUTE_REJECTED')
    def test_unknown_drift_rejected(self):
        self.plan['resource_drift'][0]['change']['after_unknown'] = {'policy': True}
        self.reject('ADOPTION_DRIFT_OTHER_ATTRIBUTE_REJECTED')
    def test_other_resource_update_rejected(self):
        self.plan['resource_changes'][1]['change']['actions'] = ['update']
        self.reject('ADOPTION_FORGET_OTHER_CHANGE_REJECTED')
    def test_duplicate_resource_rejected(self):
        self.plan['resource_changes'].append(copy.deepcopy(self.plan['resource_changes'][1]))
        self.reject('PERSISTENT_PLAN_INVALID')
    def test_no_forget_rejected(self):
        self.plan['resource_changes'].pop(0); self.reject('ADOPTION_FORGET_REQUIRED')
    def test_wrong_forget_bucket_rejected(self):
        self.plan['resource_changes'][0]['change']['before']['bucket'] = 'foreign'
        self.reject('DATA_POLICY_STATE_SCOPE_MISMATCH')
    def test_output_mutation_rejected(self):
        self.plan['output_changes'] = {'bucket': {'actions': ['update'], 'before': 'a', 'after': 'b'}}
        self.reject('ADOPTION_OUTPUT_CHANGE_REJECTED')
    def test_non_applyable_rejected(self):
        self.plan['applyable'] = False; self.reject('PERSISTENT_PLAN_INVALID')
