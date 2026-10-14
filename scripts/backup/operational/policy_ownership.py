"""Administrative policy ownership. No client factory or implicit activation.

All cooperating callers must use one broker and the same protected PR3 Store.
IAM restricts ordinary automation; hostile root/emergency is outside this mutex.
"""
import copy
import threading
import json
from contextlib import contextmanager
from .fence_policy import FenceScope, policy_document, digest, require
from .fencing import assert_fence_clear
from .lease import assert_deployment_clear
from ..capture.storage import CaptureFailure

SID = 'ExclusiveAdministrativeDataPolicyOwner'


def assert_policy_owner_clear(store):
    path = store.root/'policy-owner-active.json'
    require(path.exists() and not path.is_symlink(), 'POLICY_OWNER_NOT_REGISTERED')
    active = store.read('policy-owner-active.json')
    require(isinstance(active, dict) and set(active) == {'operation_id','terminal'}
            and active['terminal'] is True, 'POLICY_OWNER_RECONCILIATION_REQUIRED')
    store.operation(active['operation_id'])
    record = store.read(active['operation_id']+'/policy-ownership.json')
    require(record.get('kind') == 'data-policy-ownership' and record.get('operation_id') == active['operation_id']
            and record.get('phase') == 'adoption_observed'
            and digest(record.get('expected_policy')) == record.get('expected_hash'), 'POLICY_OWNER_JOURNAL_INVALID')
    return record


def guard_statement(scope):
    require(type(scope) is FenceScope, 'FENCE_SCOPE_REQUIRED')
    return {'Sid': SID, 'Effect': 'Deny', 'Principal': '*',
            'Action': ['s3:PutBucketPolicy', 's3:DeleteBucketPolicy'],
            'Resource': 'arn:aws:s3:::' + scope.bucket,
            'Condition': {'ArnNotEquals': {'aws:PrincipalArn': [scope.administrative_role, scope.emergency_role]}}}


def owned_policy(raw, scope):
    document = policy_document(raw)
    guards = [s for s in document['Statement'] if s.get('Sid') == SID]
    require(not guards or guards == [guard_statement(scope)], 'POLICY_OWNER_CONFLICT')
    require(not any(str(s.get('Sid', '')).startswith(('BackupObjectFence','BackupPolicyFence','BackupConfigurationFence'))
                    for s in document['Statement']), 'ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED')
    result = copy.deepcopy(document)
    if not guards: result['Statement'].append(guard_statement(scope))
    resource = 'arn:aws:s3:::' + scope.bucket
    require(any(s.get('Effect') == 'Deny' and s.get('Principal') == '*'
                and s.get('Action') in ('s3:*', ['s3:*'])
                and set(s.get('Resource', [])) >= {resource,resource+'/*'}
                and s.get('Condition') == {'Bool': {'aws:SecureTransport': 'false'}}
                for s in result['Statement']), 'DATA_POLICY_TLS_GUARD_REQUIRED')
    require(len(json.dumps(result).encode()) <= 20*1024, 'FENCE_POLICY_LIMIT')
    return result


def initial_policy(scope):
    resource = 'arn:aws:s3:::' + scope.bucket
    return {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'DenyInsecureTransport', 'Effect': 'Deny', 'Principal': '*', 'Action': ['s3:*'],
         'Resource': [resource, resource+'/*'], 'Condition': {'Bool': {'aws:SecureTransport': 'false'}}},
        guard_statement(scope)]}


class AdministrativeLease:
    def __init__(self, owner, operation):
        self.owner, self.operation, self.thread = owner, operation, threading.get_ident()
        self.active = True
    def check(self):
        require(self.active and self.thread == threading.get_ident(), 'ADMINISTRATIVE_LEASE_NOT_HELD')
        require(self.owner.api.identity(self.owner.scope) == self.owner.identity, 'FENCE_ADMIN_IDENTITY_MISMATCH')


class PolicyOwner:
    def __init__(self, store, api, scope, inspect_idle):
        require(type(scope) is FenceScope and callable(inspect_idle), 'POLICY_OWNER_INTERFACES_REQUIRED')
        self.store, self.api, self.scope, self.inspect_idle = store, api, scope, inspect_idle
        self.identity = None

    @contextmanager
    def hold(self, operation):
        self.store.operation(operation)
        with self.store.lock():
            assert_fence_clear(self.store)
            assert_deployment_clear(self.store)
            pointer = self.store.root/'policy-owner-active.json'
            if pointer.exists():
                active = self.store.read('policy-owner-active.json')
                require(active.get('terminal') is True, 'POLICY_OWNER_RECONCILIATION_REQUIRED')
            require(self.inspect_idle() is None, 'POLICY_OWNER_CAPTURE_RECOVERY_REQUIRED')
            identity = self.api.identity(self.scope)
            require(identity.get('account') == self.scope.account
                    and identity.get('role_arn') == self.scope.administrative_role, 'FENCE_ADMIN_IDENTITY_MISMATCH')
            self.identity = copy.deepcopy(identity)
            lease = AdministrativeLease(self, operation)
            try: yield lease
            finally:
                lease.active = False
                self.identity = None

    def adopt(self, lease):
        """Existing policy only. No deletion, no replacing third-party statements."""
        return self._adopt(lease, new=False)

    def initialize(self, lease):
        """New empty policy only after an exact NoSuchBucketPolicy response."""
        return self._adopt(lease, new=True)

    def _adopt(self, lease, new):
        require(type(lease) is AdministrativeLease and lease.owner is self, 'ADMINISTRATIVE_LEASE_REQUIRED')
        lease.check()
        path = lease.operation + '/policy-ownership.json'
        require(not (self.store.root/path).exists(), 'POLICY_OWNER_RECONCILIATION_REQUIRED')
        if new:
            try: self.api.read_policy(self.scope)
            except CaptureFailure as error:
                require(error.code == 'DATA_POLICY_ABSENT', 'POLICY_ABSENCE_NOT_PROVEN')
            else: raise CaptureFailure('POLICY_ALREADY_EXISTS')
            raw, before, wanted = None, None, initial_policy(self.scope)
        else:
            raw = self.api.read_policy(self.scope)
            before = policy_document(raw)
            wanted = owned_policy(raw, self.scope)
        record = {'kind': 'data-policy-ownership', 'operation_id': lease.operation,
                  'identity': self.identity, 'scope': self.scope.__dict__, 'before_raw': raw,
                  'before_hash': digest(before) if before is not None else None,
                  'expected_policy': wanted, 'expected_hash': digest(wanted), 'phase': 'adoption_intent'}
        self.store.write(path, record)
        self.store.write('policy-owner-active.json', {'operation_id': lease.operation, 'terminal': False})
        lease.check()
        if new:
            try: self.api.read_policy(self.scope)
            except CaptureFailure as error: require(error.code == 'DATA_POLICY_ABSENT', 'POLICY_ABSENCE_NOT_PROVEN')
            else: raise CaptureFailure('FENCE_POLICY_CONCURRENT_CHANGE')
        else:
            require(digest(policy_document(self.api.read_policy(self.scope))) == record['before_hash'], 'FENCE_POLICY_CONCURRENT_CHANGE')
        if before != wanted: self.api.put_policy(self.scope, wanted)
        lease.check()
        require(digest(policy_document(self.api.read_policy(self.scope))) == record['expected_hash'], 'FENCE_POLICY_CONCURRENT_CHANGE')
        record['phase'] = 'adoption_observed'
        self.store.write(path, record)
        self.store.write('policy-owner-active.json', {'operation_id': lease.operation, 'terminal': True})
        return {'code': 'POLICY_OWNER_ADOPTED', 'backup_complete': False}

    def reconcile(self, operation):
        """Read-only remote recovery of a lost acknowledgement; no policy write."""
        self.store.operation(operation)
        with self.store.lock():
            require(self.inspect_idle() is None, 'POLICY_OWNER_CAPTURE_RECOVERY_REQUIRED')
            assert_fence_clear(self.store)
            assert_deployment_clear(self.store)
            active = self.store.read('policy-owner-active.json')
            require(active == {'operation_id': operation, 'terminal': False}, 'POLICY_OWNER_RECONCILIATION_REQUIRED')
            record = self.store.read(operation+'/policy-ownership.json')
            identity = self.api.identity(self.scope)
            require(record.get('kind') == 'data-policy-ownership' and record.get('operation_id') == operation
                    and record.get('scope') == self.scope.__dict__ and record.get('phase') in ('adoption_intent','adoption_observed')
                    and identity['role_arn'] == self.scope.administrative_role and identity['account'] == self.scope.account
                    and record['identity']['role_arn'] == identity['role_arn'], 'POLICY_OWNER_JOURNAL_INVALID')
            expected = initial_policy(self.scope) if record['before_raw'] is None else owned_policy(record['before_raw'], self.scope)
            require(expected == record['expected_policy'] and digest(expected) == record['expected_hash'], 'POLICY_OWNER_JOURNAL_INVALID')
            require(digest(policy_document(self.api.read_policy(self.scope))) == record['expected_hash'], 'FENCE_POLICY_CONCURRENT_CHANGE')
            record.update(phase='adoption_observed', reconciliation_identity=identity)
            self.store.write(operation+'/policy-ownership.json', record)
            self.store.write('policy-owner-active.json', {'operation_id': operation, 'terminal': True})
            return {'code': 'OWNERSHIP_OBSERVED_NO_REMOTE_RETRY', 'backup_complete': False}


def validate_persistent_plan(plan, bucket_scope=None):
    """Inspect Terraform JSON offline. No AWS request or state mutation."""
    require(isinstance(plan, dict) and plan.get('errored', False) is False, 'PERSISTENT_PLAN_INVALID')
    require(plan.get('format_version') == '1.2' and isinstance(plan.get('resource_changes', []), list),
            'PERSISTENT_PLAN_INVALID')
    if bucket_scope is not None:
        require(isinstance(bucket_scope, tuple) and len(bucket_scope) == 2
                and all(isinstance(name, str) and name for name in bucket_scope)
                and bucket_scope[0] != bucket_scope[1], 'PERSISTENT_BUCKET_SCOPE_INVALID')
    for change in plan.get('resource_changes', []):
        require(isinstance(change, dict) and isinstance(change.get('change'), dict)
                and isinstance(change['change'].get('actions'), list), 'PERSISTENT_PLAN_INVALID')
        actions = change['change']['actions']
        if change.get('mode') == 'data':
            require(actions in (['read'], ['no-op']), 'PERSISTENT_PLAN_INVALID')
            continue
        if change.get('address') == 'aws_s3_bucket_policy.tls':
            require(actions == ['forget'], 'DATA_POLICY_TERRAFORM_MUTATION_REJECTED')
            if bucket_scope is not None:
                require(isinstance(change['change'].get('before'), dict)
                        and change['change']['before'].get('bucket') == bucket_scope[0],
                        'DATA_POLICY_STATE_SCOPE_MISMATCH')
            continue
        require(change.get('type') != 'aws_s3_bucket_policy'
                or change.get('address') == 'aws_s3_bucket_policy.backups_tls', 'DATA_POLICY_TERRAFORM_MUTATION_REJECTED')
        if change.get('type') == 'aws_s3_bucket_policy' and bucket_scope is not None:
            require(all(isinstance(value, dict) and value.get('bucket') == bucket_scope[1]
                        for value in (change['change'].get('before'), change['change'].get('after'))
                        if value is not None)
                    and isinstance(change['change'].get('after'), dict), 'BACKUP_POLICY_BUCKET_SCOPE_MISMATCH')
        require(actions in (['no-op'], ['create'], ['update']), 'PERSISTENT_DESTRUCTIVE_PLAN_REJECTED')


def validate_adoption_forget_plan(plan, scope, backups_bucket, journal, previous_policy, current_policy):
    """Only the proved adoption drift plus one forget. Never a generic drift exemption.

    Caller must hold the administrative lease, verify the terminal pointer and
    read the live policy independently. Previous policy comes from the protected
    pre-adoption snapshot, not from the plan itself. This function performs no IO.
    """
    require(type(scope) is FenceScope, 'FENCE_SCOPE_REQUIRED')
    previous, current = policy_document(previous_policy), policy_document(current_policy)
    require(isinstance(journal, dict) and journal.get('kind') == 'data-policy-ownership'
            and journal.get('phase') == 'adoption_observed'
            and journal.get('scope') == scope.__dict__
            and isinstance(journal.get('identity'), dict)
            and journal['identity'].get('account') == scope.account
            and journal['identity'].get('role_arn') == scope.administrative_role,
            'ADOPTION_JOURNAL_PROOF_REQUIRED')
    require(journal.get('before_hash') == digest(previous)
            and policy_document(journal.get('before_raw')) == previous
            and journal.get('expected_hash') == digest(current)
            and journal.get('expected_policy') == current,
            'ADOPTION_POLICY_PROOF_MISMATCH')
    require(not any(s.get('Sid') == SID for s in previous['Statement'])
            and owned_policy(previous_policy, scope) == current,
            'ADOPTION_TRANSITION_MISMATCH')
    validate_persistent_plan(plan, (scope.bucket, backups_bucket))
    require(plan.get('applyable') is True and not plan.get('deferred_changes'), 'PERSISTENT_PLAN_INVALID')
    addresses, forgets = set(), 0
    for resource in plan.get('resource_changes', []):
        address, change = resource.get('address'), resource['change']
        require(isinstance(address, str) and address not in addresses, 'PERSISTENT_PLAN_INVALID')
        addresses.add(address)
        require(change.get('after_unknown') in (None, False, {})
                and not change.get('replace_paths') and not resource.get('importing'), 'PERSISTENT_PLAN_INVALID')
        if address == 'aws_s3_bucket_policy.tls':
            require(resource.get('mode') == 'managed' and resource.get('type') == 'aws_s3_bucket_policy'
                    and change['actions'] == ['forget'] and change.get('after') is None,
                    'DATA_POLICY_TERRAFORM_MUTATION_REJECTED')
            forgets += 1
        else:
            require(change['actions'] == ['no-op'] and change.get('before') == change.get('after'),
                    'ADOPTION_FORGET_OTHER_CHANGE_REJECTED')
    require(forgets == 1, 'ADOPTION_FORGET_REQUIRED')
    outputs = plan.get('output_changes', {})
    require(isinstance(outputs, dict) and all(isinstance(c, dict) and c.get('actions') == ['no-op']
            and c.get('before') == c.get('after') and c.get('after_unknown') in (None, False, {})
            for c in outputs.values()), 'ADOPTION_OUTPUT_CHANGE_REJECTED')
    drift = plan.get('resource_drift', [])
    require(isinstance(drift, list) and len(drift) == 1 and isinstance(drift[0], dict),
            'ADOPTION_DRIFT_NOT_EXCLUSIVE')
    resource = drift[0]
    change = resource.get('change', {})
    require(resource.get('address') == 'aws_s3_bucket.data' and resource.get('mode') == 'managed'
            and resource.get('type') == 'aws_s3_bucket' and change.get('actions') == ['update']
            and isinstance(change.get('before'), dict) and isinstance(change.get('after'), dict),
            'ADOPTION_DRIFT_SCOPE_MISMATCH')
    before, after = change['before'], change['after']
    require(before.get('bucket') == scope.bucket and before.get('id') == scope.bucket
            and after.get('bucket') == scope.bucket and after.get('id') == scope.bucket,
            'ADOPTION_DRIFT_SCOPE_MISMATCH')
    require({k: v for k, v in before.items() if k != 'policy'}
            == {k: v for k, v in after.items() if k != 'policy'}
            and change.get('after_unknown') in (None, False, {})
            and not change.get('replace_paths'), 'ADOPTION_DRIFT_OTHER_ATTRIBUTE_REJECTED')
    require(policy_document(before.get('policy')) == previous
            and policy_document(after.get('policy')) == current, 'ADOPTION_DRIFT_POLICY_MISMATCH')
    return {'forget': 1, 'add': 0, 'change': 0, 'destroy': 0,
            'replacement': 0, 'destroy_false': True, 'proved_adoption_drift': 1}
