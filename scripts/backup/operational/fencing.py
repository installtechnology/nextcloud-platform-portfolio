"""Administrative write-ahead fence coordinator; injected interfaces only.

No AWS launcher/client factory. A real exclusive-policy authority and complete
writer barrier must be implemented/approved before operational use. Neither
successful PutBucketPolicy nor two equal inventories is a quiescence proof.
Shared lock stays held throughout the window. Failure never auto-unfences.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import threading
from .fence_policy import FenceScope, build_policy, policy_document, digest, require
from ..capture.storage import CaptureFailure


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class PolicyAuthority:
    """No default approval. Requires an external administrative change freeze.

    Real implementation must exclude *all* other policy/config writers before
    the first read/write, including Terraform and break-glass. No S3 CAS exists.
    """
    @contextmanager
    def hold(self, scope, operation):
        raise CaptureFailure('POLICY_AUTHORITY_UNIMPLEMENTED')
        yield

    def assert_held(self, scope, operation):
        raise CaptureFailure('POLICY_AUTHORITY_UNIMPLEMENTED')


class WriterBarrier:
    """Real collector must stop/drain the complete set of identified writers.

    Return a digest of protected evidence, not user-provided ready flags.
    No default collector, self-signed config flag or inventory-only fallback.
    """
    def verify(self, operation, capture_id, phase):
        raise CaptureFailure('WRITER_BARRIER_UNIMPLEMENTED')

    def before_window(self, scope, operation):
        raise CaptureFailure('WRITER_BARRIER_UNIMPLEMENTED')


@dataclass(repr=False)
class HeldWindow:
    coordinator: object
    operation: str
    thread: int
    active: bool = True
    released: bool = False

    def assert_held(self):
        require(self.active and not self.released and self.thread == threading.get_ident(), 'FENCE_WINDOW_NOT_HELD')
        self.coordinator.authority.assert_held(self.coordinator.scope, self.operation)


class CaptureStoreView:
    """Reentrant view only inside an already-held shared PR3 lock.

    Avoids nested flock deadlock while Capture.start still calls store.lock().
    Not a synthetic/operational switch. Scope and thread are checked on every IO.
    """
    def __init__(self, store, window):
        require(type(window) is HeldWindow and window.coordinator.store is store, 'FENCE_WINDOW_REQUIRED')
        self._store, self._window = store, window

    @property
    def root(self):
        self._window.assert_held()
        return self._store.root

    @contextmanager
    def lock(self):
        self._window.assert_held()
        yield
        self._window.assert_held()

    def __getattr__(self, name):
        require(name in ('operation', 'read', 'write', 'reserve', 'release_reserve', 'artifact'), 'CAPTURE_STORE_METHOD_REJECTED')
        def invoke(*args, **kwargs):
            self._window.assert_held()
            return getattr(self._store, name)(*args, **kwargs)
        return invoke


def assert_fence_clear(store):
    path = store.root / 'fence-active.json'
    if path.exists() or path.is_symlink():
        try:
            active = store.read('fence-active.json')
            require(isinstance(active, dict) and set(active) == {'operation_id', 'terminal'}
                    and active['terminal'] is True, 'ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED')
            store.operation(active['operation_id'])
            journal = store.read(active['operation_id'] + '/fence-journal.json')
            require(journal.get('phase') == 'restored' and journal.get('operation_id') == active['operation_id']
                    and journal.get('restored_hash') == journal.get('before_hash'), 'ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED')
        except Exception:
            raise CaptureFailure('ADMINISTRATIVE_FENCE_RECOVERY_REQUIRED') from None


class Coordinator:
    def __init__(self, store, api, scope, authority, barrier):
        require(type(scope) is FenceScope and isinstance(authority, PolicyAuthority)
                and isinstance(barrier, WriterBarrier), 'FENCE_INTERFACES_REQUIRED')
        self.store, self.api, self.scope, self.authority, self.barrier = store, api, scope, authority, barrier

    def write(self, operation, record):
        try:
            self.store.write(operation + '/fence-journal.json', record)
        except Exception:
            raise CaptureFailure('FENCE_JOURNAL_WRITE_FAILED') from None

    def load(self, operation):
        """Validate every control input before recovery, never trust JSON flags."""
        try:
            record = self.store.read(operation + '/fence-journal.json')
            require(isinstance(record, dict) and record.get('kind') == 'administrative-s3-fence'
                    and type(record.get('version')) is int and record['version'] == 1
                    and record.get('operation_id') == operation and record.get('scope') == self.scope.__dict__
                    and record.get('phase') in ('install_intent', 'observed', 'quiescent', 'restore_intent', 'restored')
                    and record.get('backup_complete') is False and isinstance(record.get('observations'), list)
                    and len(record['observations']) <= 2, 'FENCE_JOURNAL_INVALID')
            original = policy_document(record['before_raw'])
            require(digest(original) == record['before_hash']
                    and hashlib.sha256(record['before_raw'].encode()).hexdigest() == record['before_bytes_sha256']
                    and build_policy(record['before_raw'], self.scope, operation) == record['fenced_policy']
                    and digest(record['fenced_policy']) == record['fenced_hash'], 'FENCE_JOURNAL_INVALID')
            owner = record['identity']
            require(isinstance(owner, dict) and set(owner) == {'account', 'role_arn', 'principal_arn', 'principal_id'}
                    and owner['account'] == self.scope.account and owner['role_arn'] == self.scope.administrative_role,
                    'FENCE_JOURNAL_INVALID')
            if record['capture_id'] is not None:
                self.store.operation(record['capture_id'])
            expected = ['before', 'after'][:len(record['observations'])]
            require([o.get('phase') for o in record['observations']] == expected, 'FENCE_JOURNAL_INVALID')
            import re
            for observation in record['observations']:
                require(set(observation) == {'phase', 'at', 'capture_id', 'writer_evidence_sha256', 'inventory_sha256'}
                        and observation['capture_id'] == record['capture_id']
                        and all(isinstance(observation[k], str) and re.fullmatch(r'[a-f0-9]{64}', observation[k])
                                for k in ('writer_evidence_sha256', 'inventory_sha256')), 'FENCE_JOURNAL_INVALID')
            return record
        except Exception:
            raise CaptureFailure('FENCE_JOURNAL_INVALID') from None

    def call(self, name, *args):
        # Narrow administrative interface; no data PUT/DELETE, retention or IAM.
        require(name in ('identity', 'read_policy', 'put_policy', 'read_configuration',
                         'pending_uploads', 'inventory_hash'), 'FENCE_API_REJECTED')
        try:
            return getattr(self.api, name)(self.scope, *args)
        except CaptureFailure:
            raise
        except BaseException:
            raise CaptureFailure('FENCE_API_OUTCOME_UNKNOWN') from None

    def identity(self):
        import re
        identity = self.call('identity')
        require(isinstance(identity, dict) and set(identity) == {'account', 'role_arn', 'principal_arn', 'principal_id'}
                and identity['account'] == self.scope.account and identity['role_arn'] == self.scope.administrative_role
                and isinstance(identity['principal_arn'], str) and re.fullmatch(
                    'arn:aws:sts::' + self.scope.account + ':assumed-role/'
                    + re.escape(self.scope.administrative_role.rsplit('/', 1)[1]) + '/[A-Za-z0-9+=,.@_-]{2,64}', identity['principal_arn'])
                and isinstance(identity['principal_id'], str) and re.fullmatch(r'[A-Z0-9]{8,128}:[A-Za-z0-9+=,.@_-]{2,64}', identity['principal_id']),
                'FENCE_ADMIN_IDENTITY_MISMATCH')
        return identity

    def read(self):
        raw = self.call('read_policy')
        return raw, policy_document(raw)

    def compare(self, expected):
        _, document = self.read()
        require(digest(document) == expected, 'FENCE_POLICY_CONCURRENT_CHANGE')

    def configuration(self):
        value = self.call('read_configuration')
        # Lifecycle does not honor a bucket-policy deny for automated expiration.
        # Other autonomous writers/retention configurations are not assumed safe.
        require(value == {'versioning': 'Enabled', 'lifecycle': [], 'replication': None, 'object_lock': None},
                'FENCE_AUTONOMOUS_WRITER_OR_CONFIGURATION')

    def checkpoint(self, window, capture_id, phase):
        import re
        require(type(window) is HeldWindow and window.coordinator is self, 'FENCE_WINDOW_REQUIRED')
        window.assert_held()
        self.store.operation(capture_id)
        record = self.load(window.operation)
        require(record['capture_id'] == capture_id and phase in ('before', 'after'), 'FENCE_CAPTURE_BINDING_MISMATCH')
        stages = [o['phase'] for o in record['observations']]
        require(stages == ([] if phase == 'before' else ['before']), 'FENCE_OBSERVATION_ORDER_INVALID')
        self.identity()
        self.compare(record['fenced_hash'])
        self.configuration()
        require(self.call('pending_uploads') == [], 'FENCE_MULTIPART_PENDING')
        evidence = self.barrier.verify(window.operation, capture_id, phase)
        require(isinstance(evidence, str) and re.fullmatch(r'[a-f0-9]{64}', evidence), 'FENCE_WRITER_EVIDENCE_REQUIRED')
        first = self.call('inventory_hash')
        self.compare(record['fenced_hash'])
        require(self.call('pending_uploads') == [], 'FENCE_MULTIPART_PENDING')
        second = self.call('inventory_hash')
        require(isinstance(first, str) and re.fullmatch(r'[a-f0-9]{64}', first)
                and second == first, 'FENCE_INVENTORY_CHANGED')
        require(phase == 'before' or record['observations'][0]['inventory_sha256'] == first,
                'FENCE_INVENTORY_CHANGED')
        second_evidence = self.barrier.verify(window.operation, capture_id, phase)
        require(isinstance(second_evidence, str) and re.fullmatch(r'[a-f0-9]{64}', second_evidence),
                'FENCE_WRITER_EVIDENCE_REQUIRED')
        window.assert_held()
        record['observations'].append({'phase': phase, 'at': now(), 'capture_id': capture_id,
                                       'writer_evidence_sha256': evidence, 'inventory_sha256': first})
        record['phase'] = 'quiescent'
        self.write(window.operation, record)
        return {'fence_id': window.operation, 'capture_id': capture_id,
                'policy_sha256': record['fenced_hash'], 'inventory_sha256': first,
                'writer_evidence_sha256': evidence, 'backup_complete': False}

    def bind(self, window, capture_id):
        require(type(window) is HeldWindow and window.coordinator is self, 'FENCE_WINDOW_REQUIRED')
        window.assert_held()
        self.store.operation(capture_id)
        record = self.load(window.operation)
        require(record['capture_id'] in (None, capture_id), 'FENCE_CAPTURE_BINDING_MISMATCH')
        require(record['phase'] in ('observed', 'quiescent'), 'FENCE_NOT_OBSERVED')
        record['capture_id'] = capture_id
        self.write(window.operation, record)

    @contextmanager
    def window(self, operation):
        self.store.operation(operation)
        from .lease import assert_deployment_clear
        with self.store.lock(), self.authority.hold(self.scope, operation):
            assert_deployment_clear(self.store)
            assert_fence_clear(self.store)
            require(self.barrier.before_window(self.scope, operation) is None, 'FENCE_CAPTURE_RECOVERY_REQUIRED')
            self.authority.assert_held(self.scope, operation)
            self.identity()
            self.configuration()
            raw, original = self.read()
            fenced = build_policy(raw, self.scope, operation)
            path = self.store.operation(operation)
            require(path.is_dir() and not path.is_symlink(), 'FENCE_PRIVATE_OPERATION_REQUIRED')
            journal_path = path / 'fence-journal.json'
            require(not journal_path.exists() and not journal_path.is_symlink(), 'FENCE_JOURNAL_EXISTS')
            record = {'kind': 'administrative-s3-fence', 'version': 1, 'operation_id': operation,
                'scope': self.scope.__dict__.copy(), 'identity': self.identity(), 'before_raw': raw,
                'before_hash': digest(original), 'before_bytes_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                'fenced_policy': fenced, 'fenced_hash': digest(fenced), 'phase': 'install_intent',
                'capture_id': None, 'observations': [], 'created_at': now(), 'backup_complete': False}
            self.write(operation, record)
            self.store.write('fence-active.json', {'operation_id': operation, 'terminal': False})
            window = HeldWindow(self, operation, threading.get_ident())
            try:
                self.compare(record['before_hash'])
                window.assert_held()
                self.call('put_policy', fenced)
                self.compare(record['fenced_hash'])
                record['phase'] = 'observed'
                record['applied_observed_at'] = now()
                self.write(operation, record)
                yield window
                # Normal context exit is deliberately NOT authorization to remove.
                final = self.load(operation)
                require(final['phase'] == 'restored', 'FENCE_EXPLICIT_RELEASE_REQUIRED')
            except BaseException as error:
                # Keep the last durable intent and installed denies; no rollback.
                code = error.code if isinstance(error, CaptureFailure) else 'FENCE_RECONCILIATION_REQUIRED'
                try:
                    failed = self.store.read(operation + '/fence-journal.json')
                    failed.update(last_error_code=code, interrupted_at=now())
                    self.write(operation, failed)
                except Exception:
                    pass
                raise CaptureFailure(code) from None
            finally:
                window.active = False

    def release(self, window, verify_capture_idle):
        require(type(window) is HeldWindow and window.coordinator is self, 'FENCE_WINDOW_REQUIRED')
        window.assert_held()
        record = self.load(window.operation)
        require(record.get('phase') in ('observed', 'quiescent'), 'FENCE_RECONCILIATION_REQUIRED')
        # Must verify PG sessions, execs, original maintenance and writer state.
        require(verify_capture_idle() is None, 'FENCE_CAPTURE_RECOVERY_REQUIRED')
        self._restore(window.operation, record, window)

    def _restore(self, operation, record, window, recovery=False):
        window.assert_held()
        self.identity()
        current_identity = self.identity()
        require(record['scope'] == self.scope.__dict__
                and record['identity']['account'] == current_identity['account']
                and record['identity']['role_arn'] == current_identity['role_arn']
                and (recovery or record['identity'] == current_identity), 'FENCE_ADMIN_IDENTITY_MISMATCH')
        if recovery:
            record['reconciliation_identity'] = current_identity
        self.compare(record['fenced_hash'])
        original = policy_document(record['before_raw'])
        require(digest(original) == record['before_hash']
                and hashlib.sha256(record['before_raw'].encode()).hexdigest() == record['before_bytes_sha256']
                and build_policy(record['before_raw'], self.scope, operation) == record['fenced_policy'], 'FENCE_JOURNAL_INVALID')
        record['phase'] = 'restore_intent'
        self.write(operation, record)
        self.compare(record['fenced_hash'])
        window.assert_held()
        self.call('put_policy', original)
        self.compare(record['before_hash'])
        record.update(phase='restored', restored_hash=record['before_hash'], restored_at=now())
        self.write(operation, record)
        self.store.write('fence-active.json', {'operation_id': operation, 'terminal': True})
        window.released = True

    def reconcile(self, operation, verify_capture_idle):
        """Explicit administrative abandonment; never resumes/publishes backup.

        Changed third-party policy is NOT merged or overwritten. Administrator
        must examine the delta and approve a separate emergency procedure.
        """
        self.store.operation(operation)
        with self.store.lock(), self.authority.hold(self.scope, operation):
            record = self.load(operation)
            require(record.get('kind') == 'administrative-s3-fence' and type(record.get('version')) is int and record['version'] == 1
                    and record.get('operation_id') == operation and record.get('scope') == self.scope.__dict__, 'FENCE_JOURNAL_INVALID')
            require(self.store.read('fence-active.json') == {'operation_id': operation, 'terminal': False}, 'FENCE_OWNERSHIP_CHANGED')
            self.identity()
            require(verify_capture_idle() is None, 'FENCE_CAPTURE_RECOVERY_REQUIRED')
            window = HeldWindow(self, operation, threading.get_ident())
            try:
                _, current = self.read()
                current_hash = digest(current)
                if current_hash == record['before_hash']:
                    # Lost apply or restore response; prove original bytes/identity
                    # before acknowledging. No additional AWS mutation occurs.
                    require(record.get('phase') in ('install_intent', 'restore_intent', 'restored'), 'FENCE_STATE_AMBIGUOUS')
                    if record['phase'] == 'restored':
                        # Restore was observed/journaled but updating the active
                        # pointer failed. Recovery still rereads the policy and
                        # requires the idle proof before fixing this pointer.
                        require(record.get('restored_hash') == record['before_hash']
                                and isinstance(record.get('restored_at'), str), 'FENCE_JOURNAL_INVALID')
                    original = policy_document(record['before_raw'])
                    require(digest(original) == current_hash
                            and hashlib.sha256(record['before_raw'].encode()).hexdigest() == record['before_bytes_sha256']
                            and build_policy(record['before_raw'], self.scope, operation) == record['fenced_policy']
                            and record['identity']['account'] == self.identity()['account']
                            and record['identity']['role_arn'] == self.identity()['role_arn'], 'FENCE_JOURNAL_INVALID')
                    record['reconciliation_identity'] = self.identity()
                    record.update(phase='restored', restored_hash=current_hash, restored_at=now())
                    self.write(operation, record)
                    self.store.write('fence-active.json', {'operation_id': operation, 'terminal': True})
                else:
                    self._restore(operation, record, window, recovery=True)
            finally:
                window.active = False
            return {'code': 'ADMINISTRATIVE_FENCE_RESTORED_NO_RESUME', 'backup_complete': False}
