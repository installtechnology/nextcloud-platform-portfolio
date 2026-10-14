import copy
import json
from contextlib import contextmanager
import unittest
from scripts.backup.operational.fence_policy import FenceScope, build_policy, policy_document, digest, OBJECT_MUTATIONS, CONFIGURATION_MUTATIONS
from scripts.backup.operational.fencing import Coordinator, PolicyAuthority, WriterBarrier, CaptureStoreView, assert_fence_clear
from scripts.backup.operational.interlock import deployment_guard
from scripts.backup.capture.storage import CaptureFailure

OP = '20260101T000000Z-01234567-89ab-4cde-8fab-0123456789ab'
CAPTURE = '20260101T000001Z-01234567-89ab-4cde-8fab-0123456789ab'
SCOPE = FenceScope('123456789012', 'synthetic-data', 'arn:aws:iam::123456789012:role/fence-admin', 'arn:aws:iam::123456789012:role/fence-emergency')
ORIGINAL = {'Version': '2012-10-17', 'Id': 'preserve-this-id', 'Statement': [
    {'Sid': 'TLS', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
     'Resource': ['arn:aws:s3:::synthetic-data', 'arn:aws:s3:::synthetic-data/*'],
     'Condition': {'Bool': {'aws:SecureTransport': 'false'}}},
    {'Sid': 'ExistingGrant', 'Effect': 'Allow', 'Principal': {'AWS': 'arn:aws:iam::123456789012:role/synthetic-reader'},
     'Action': ['s3:GetObject'], 'Resource': 'arn:aws:s3:::synthetic-data/*'},
]}


class Path:
    def __init__(self, store, name=''): self.store, self.name = store, name
    def __truediv__(self, name): return Path(self.store, (self.name + '/' + name).strip('/'))
    def exists(self): return self.name in self.store.data
    def is_symlink(self): return False
    def is_dir(self): return self.name in (OP, CAPTURE)


class Store:
    def __init__(self): self.data = {}; self.locked = False; self.root = Path(self); self.fail_path = None
    def operation(self, operation):
        if operation not in (OP, CAPTURE): raise CaptureFailure('INVALID_OPERATION_ID')
        return self.root / operation
    @contextmanager
    def lock(self):
        if self.locked: raise CaptureFailure('CAPTURE_BUSY')
        self.locked = True
        try: yield
        finally: self.locked = False
    def read(self, path): return copy.deepcopy(self.data[path])
    def write(self, path, document):
        if self.fail_path == path: raise OSError('synthetic-sensitive ENOSPC')
        self.data[path] = copy.deepcopy(document)


class Authority(PolicyAuthority):
    def __init__(self): self.held = False
    @contextmanager
    def hold(self, scope, operation):
        if self.held: raise CaptureFailure('POLICY_AUTHORITY_BUSY')
        self.held = True
        try: yield
        finally: self.held = False
    def assert_held(self, scope, operation):
        if not self.held: raise CaptureFailure('POLICY_AUTHORITY_LOST')


class Barrier(WriterBarrier):
    def __init__(self): self.calls = []; self.fail = None
    def before_window(self, scope, operation):
        if self.fail == 'entry': raise CaptureFailure('ORPHANED_CAPTURE_PRESENT')
    def verify(self, operation, capture, phase):
        self.calls.append((operation, capture, phase))
        if self.fail: raise CaptureFailure('EXTERNAL_WRITER_NOT_DRAINED')
        return 'b' * 64


class API:
    def __init__(self, store):
        self.raw = json.dumps(ORIGINAL, indent=2); self.store = store; self.writes = []
        self.mode = None; self.pending = []; self.hash = 'a'*64
        self.config = {'versioning': 'Enabled', 'lifecycle': [], 'replication': None, 'object_lock': None}
        self.admin = SCOPE.administrative_role
        self.session = 'synthetic-run'
    def identity(self, scope): return {'account': scope.account, 'role_arn': self.admin,
        'principal_arn': 'arn:aws:sts::123456789012:assumed-role/fence-admin/'+self.session,
        'principal_id': 'AROASYNTHETIC:'+self.session}
    def read_policy(self, scope): return self.raw
    def put_policy(self, scope, document):
        assert self.store.locked
        state = self.store.data[OP + '/fence-journal.json']
        assert state['phase'] in ('install_intent', 'restore_intent')
        self.writes.append(copy.deepcopy(document))
        if self.mode == 'before': raise TimeoutError('synthetic-sensitive')
        self.raw = json.dumps(document)
        if self.mode == 'after': raise TimeoutError('synthetic-sensitive')
        if self.mode == 'kill': raise KeyboardInterrupt()
    def read_configuration(self, scope): return self.config
    def pending_uploads(self, scope): return self.pending
    def inventory_hash(self, scope): return self.hash


class PolicyTests(unittest.TestCase):
    def test_original_preserved_structurally_and_in_order(self):
        raw = json.dumps(ORIGINAL)
        result = build_policy(raw, SCOPE, OP)
        self.assertEqual(result['Statement'][:2], ORIGINAL['Statement'])
        self.assertEqual(result['Id'], ORIGINAL['Id']); self.assertEqual(ORIGINAL['Statement'][0]['Sid'], 'TLS')
    def test_object_denies_have_no_exceptions_or_expiry(self):
        statement = build_policy(json.dumps(ORIGINAL), SCOPE, OP)['Statement'][-2]
        self.assertEqual(statement['Principal'], '*'); self.assertNotIn('Condition', statement)
        self.assertEqual(set(statement['Action']), set(OBJECT_MUTATIONS))
        self.assertEqual(statement['Resource'], 'arn:aws:s3:::synthetic-data/*')
    def test_multipart_is_covered_by_put_not_nonexistent_iam_actions(self):
        actions = OBJECT_MUTATIONS
        self.assertIn('s3:PutObject', actions); self.assertIn('s3:AbortMultipartUpload', actions)
        self.assertNotIn('s3:CompleteMultipartUpload', actions); self.assertNotIn('s3:CreateMultipartUpload', actions)
    def test_reads_not_denied(self):
        for action in ('s3:GetObject', 's3:GetObjectVersion', 's3:ListBucketVersions', 's3:ListBucket', 's3:ListBucketMultipartUploads'):
            self.assertNotIn(action, OBJECT_MUTATIONS)
    def test_autonomous_configuration_changes_denied_for_everyone(self):
        statement = build_policy(json.dumps(ORIGINAL), SCOPE, OP)['Statement'][-3]
        self.assertEqual(statement['Principal'], '*'); self.assertNotIn('Condition', statement)
        self.assertIn('s3:PutLifecycleConfiguration', statement['Action'])
        self.assertEqual(set(statement['Action']), set(CONFIGURATION_MUTATIONS))
    def test_policy_admin_only_exempt_from_policy_deny(self):
        result = build_policy(json.dumps(ORIGINAL), SCOPE, OP)
        guard = result['Statement'][-1]
        self.assertEqual(guard['Condition']['ArnNotEquals']['aws:PrincipalArn'], [SCOPE.administrative_role, SCOPE.emergency_role])
        self.assertEqual(guard['Action'], ['s3:PutBucketPolicy', 's3:DeleteBucketPolicy'])
    def test_duplicate_json_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_POLICY_INVALID$'):
            policy_document('{"Version":"2012-10-17","Version":"2008-10-17","Statement":[{}]}')
    def test_missing_policy_never_replaces_original(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_POLICY_REQUIRED$'): policy_document(None)
    def test_second_fence_rejected(self):
        result = build_policy(json.dumps(ORIGINAL), SCOPE, OP)
        with self.assertRaisesRegex(CaptureFailure, '^EXISTING_FENCE_REQUIRES_RECONCILIATION$'):
            build_policy(json.dumps(result), SCOPE, CAPTURE)
    def test_oversized_policy(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_POLICY_INVALID$'): policy_document(' ' * (20*1024+1))
    def test_cross_account_admin(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_ROLE_INVALID$'):
            FenceScope(SCOPE.account, SCOPE.bucket, 'arn:aws:iam::999999999999:role/fence-admin', SCOPE.emergency_role)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.api = API(self.store); self.authority = Authority(); self.barrier = Barrier()
        self.coordinator = Coordinator(self.store, self.api, SCOPE, self.authority, self.barrier)
    def code(self, code, action):
        with self.assertRaises(CaptureFailure) as result: action()
        self.assertEqual(result.exception.code, code)
        self.assertNotIn('synthetic-sensitive', str(result.exception))
    def enter_fail(self, body=lambda w: None):
        with self.coordinator.window(OP) as window: body(window)
    def release(self, window): self.coordinator.release(window, lambda: None)
    def test_explicit_apply_capture_and_release_preserves_policy(self):
        with self.coordinator.window(OP) as window:
            self.assertTrue(self.store.locked)
            self.coordinator.bind(window, CAPTURE)
            self.assertFalse(self.coordinator.checkpoint(window, CAPTURE, 'before')['backup_complete'])
            self.coordinator.checkpoint(window, CAPTURE, 'after')
            self.release(window)
        self.assertEqual(policy_document(self.api.raw), ORIGINAL)
        self.assertTrue(self.store.data['fence-active.json']['terminal'])
    def test_normal_exit_does_not_auto_unfence(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        self.assertFalse(self.store.data['fence-active.json']['terminal'])
        self.assertEqual(len(self.api.writes), 1)
    def test_no_authority_no_put(self):
        self.coordinator.authority = PolicyAuthority()
        self.code('POLICY_AUTHORITY_UNIMPLEMENTED', self.enter_fail)
        self.assertFalse(self.api.writes)
    def test_backup_or_application_role_not_admin(self):
        self.api.admin = 'arn:aws:iam::123456789012:role/application'
        self.code('FENCE_ADMIN_IDENTITY_MISMATCH', self.enter_fail)
        self.assertFalse(self.api.writes)
    def test_existing_lifecycle_rejected_without_changing_it(self):
        self.api.config['lifecycle'] = [{'ID': 'existing-expiration'}]
        self.code('FENCE_AUTONOMOUS_WRITER_OR_CONFIGURATION', self.enter_fail)
        self.assertFalse(self.api.writes)
    def test_pending_multipart_rejected_not_aborted(self):
        self.api.pending = ['synthetic-upload-id']
        def capture(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, CAPTURE, 'before')
        self.code('FENCE_MULTIPART_PENDING', lambda: self.enter_fail(capture))
        self.assertEqual(self.api.pending, ['synthetic-upload-id'])
    def test_external_writer_not_drained_even_if_inventories_equal(self):
        self.barrier.fail = 'external'
        def capture(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, CAPTURE, 'before')
        self.code('EXTERNAL_WRITER_NOT_DRAINED', lambda: self.enter_fail(capture))
    def test_after_before_order_enforced(self):
        def capture(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, CAPTURE, 'after')
        self.code('FENCE_OBSERVATION_ORDER_INVALID', lambda: self.enter_fail(capture))
    def test_mixed_capture_rejected(self):
        def capture(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, OP, 'before')
        self.code('FENCE_CAPTURE_BINDING_MISMATCH', lambda: self.enter_fail(capture))
    def test_inventory_change_between_capture_and_after(self):
        def capture(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, CAPTURE, 'before')
            self.api.hash = 'c'*64
            self.coordinator.checkpoint(window, CAPTURE, 'after')
        self.code('FENCE_INVENTORY_CHANGED', lambda: self.enter_fail(capture))
    def test_timeout_after_apply_keeps_fence_and_can_reconcile(self):
        self.api.mode = 'after'; self.code('FENCE_API_OUTCOME_UNKNOWN', self.enter_fail)
        self.assertFalse(self.store.data['fence-active.json']['terminal'])
        self.api.mode = None
        self.assertFalse(self.coordinator.reconcile(OP, lambda: None)['backup_complete'])
        self.assertEqual(policy_document(self.api.raw), ORIGINAL)
    def test_timeout_before_apply_reconciles_without_another_put(self):
        self.api.mode = 'before'; self.code('FENCE_API_OUTCOME_UNKNOWN', self.enter_fail)
        self.api.mode = None; self.coordinator.reconcile(OP, lambda: None)
        self.assertEqual(len(self.api.writes), 1)
    def test_controller_death_after_write_has_no_auto_release(self):
        self.api.mode = 'kill'; self.code('FENCE_API_OUTCOME_UNKNOWN', self.enter_fail)
        self.assertFalse(self.store.data['fence-active.json']['terminal'])
        self.assertNotEqual(policy_document(self.api.raw), ORIGINAL)
    def test_third_party_policy_not_overwritten_on_recovery(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        changed = policy_document(self.api.raw); changed['Id'] = 'legitimate-third-party-change'
        self.api.raw = json.dumps(changed)
        self.code('FENCE_POLICY_CONCURRENT_CHANGE', lambda: self.coordinator.reconcile(OP, lambda: None))
        self.assertEqual(policy_document(self.api.raw), changed); self.assertEqual(len(self.api.writes), 1)
    def test_failed_restore_response_reconciles_no_repeated_write(self):
        def body(window):
            self.api.mode = 'after'; self.release(window)
        self.code('FENCE_API_OUTCOME_UNKNOWN', lambda: self.enter_fail(body))
        self.api.mode = None; self.coordinator.reconcile(OP, lambda: None)
        self.assertEqual(len(self.api.writes), 2)
    def test_journal_disk_failure_prevents_put(self):
        self.store.fail_path = OP + '/fence-journal.json'
        self.code('FENCE_JOURNAL_WRITE_FAILED', self.enter_fail)
        self.assertFalse(self.api.writes)
    def test_other_deploy_cannot_acquire_shared_lock(self):
        with self.coordinator.window(OP) as window:
            with self.assertRaisesRegex(CaptureFailure, '^CAPTURE_BUSY$'):
                with deployment_guard(self.store, lambda: []): self.fail('Concurrent deployment')
            self.release(window)
    def test_deploy_blocked_after_controller_death(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        def deploy():
            with deployment_guard(self.store, lambda: []): self.fail('Unresolved fence ignored')
        self.code('ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED', deploy)
    def test_authority_loss_blocks_unfence(self):
        def body(window):
            self.authority.held = False; self.release(window)
        self.code('POLICY_AUTHORITY_LOST', lambda: self.enter_fail(body))
        self.assertEqual(len(self.api.writes), 1)
    def test_restore_waits_for_capture_recovery(self):
        def body(window): self.coordinator.release(window, lambda: ['orphan'])
        self.code('FENCE_CAPTURE_RECOVERY_REQUIRED', lambda: self.enter_fail(body))
        self.assertEqual(len(self.api.writes), 1)
    def test_store_view_valid_only_inside_window_same_thread(self):
        with self.coordinator.window(OP) as window:
            view = CaptureStoreView(self.store, window)
            with view.lock(): self.assertTrue(self.store.locked)
            self.release(window)
        self.code('FENCE_WINDOW_NOT_HELD', lambda: view.read('fence-active.json'))
    def test_preexisting_capture_blocks_before_policy_write(self):
        self.barrier.fail = 'entry'; self.code('ORPHANED_CAPTURE_PRESENT', self.enter_fail)
        self.assertFalse(self.api.writes)
    def test_original_hash_tamper_rejected(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        self.store.data[OP+'/fence-journal.json']['before_hash'] = 'd'*64
        self.code('FENCE_JOURNAL_INVALID', lambda: self.coordinator.reconcile(OP, lambda: None))
    def test_new_admin_session_requires_explicit_reconciliation(self):
        def body(window):
            self.api.session = 'replacement-session'; self.release(window)
        self.code('FENCE_ADMIN_IDENTITY_MISMATCH', lambda: self.enter_fail(body))
        self.coordinator.reconcile(OP, lambda: None)
        journal = self.store.data[OP+'/fence-journal.json']
        self.assertEqual(journal['identity']['principal_id'], 'AROASYNTHETIC:synthetic-run')
        self.assertEqual(journal['reconciliation_identity']['principal_id'], 'AROASYNTHETIC:replacement-session')
    def test_boolean_journal_version_rejected(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        self.store.data[OP+'/fence-journal.json']['version'] = True
        self.code('FENCE_JOURNAL_INVALID', lambda: self.coordinator.reconcile(OP, lambda: None))
        self.assertEqual(len(self.api.writes), 1)
    def test_missing_original_policy_rejected(self):
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', self.enter_fail)
        del self.store.data[OP+'/fence-journal.json']['before_raw']
        self.code('FENCE_JOURNAL_INVALID', lambda: self.coordinator.reconcile(OP, lambda: None))
    def test_mixed_observation_capture_rejected(self):
        def body(window):
            self.coordinator.bind(window, CAPTURE)
            self.coordinator.checkpoint(window, CAPTURE, 'before')
        self.code('FENCE_EXPLICIT_RELEASE_REQUIRED', lambda: self.enter_fail(body))
        self.store.data[OP+'/fence-journal.json']['observations'][0]['capture_id'] = OP
        self.code('FENCE_JOURNAL_INVALID', lambda: self.coordinator.reconcile(OP, lambda: None))
    def test_store_view_invalid_immediately_after_release(self):
        with self.coordinator.window(OP) as window:
            view = CaptureStoreView(self.store, window)
            self.release(window)
            self.code('FENCE_WINDOW_NOT_HELD', lambda: view.read('fence-active.json'))
    def test_restored_journal_with_failed_active_update_reconciles(self):
        def body(window):
            self.store.fail_path = 'fence-active.json'
            self.release(window)
        self.code('FENCE_RECONCILIATION_REQUIRED', lambda: self.enter_fail(body))
        self.assertEqual(self.store.data[OP+'/fence-journal.json']['phase'], 'restored')
        self.assertFalse(self.store.data['fence-active.json']['terminal'])
        self.store.fail_path = None
        self.coordinator.reconcile(OP, lambda: None)
        self.assertTrue(self.store.data['fence-active.json']['terminal'])
        self.assertEqual(len(self.api.writes), 2)
