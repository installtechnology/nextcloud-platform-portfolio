"""Write-ahead intents; interrupted captures are abandoned, never combined or resumed."""
from datetime import datetime, timezone
import uuid
import shutil
import errno
from pathlib import Path

from .storage import CaptureFailure, sync_directory
from .control import cleanup_signals


def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class Capture:
    def __init__(self, store, backend, inventory_hooks=None):
        self.store, self.backend = store, backend
        self.inventory_hooks = inventory_hooks

    def event(self, journal, phase, **updates):
        journal.update(phase=phase, updated_at=utc(), **updates)
        journal['events'].append({'phase': phase, 'at': journal['updated_at']})
        self.store.write(journal['operation_id'] + '/journal.json', journal)

    def start(self):
        with self.store.lock():
            if (self.store.root / 'active.json').exists():
                active = self.store.read('active.json')
                if active.get('terminal') is not True:
                    raise CaptureFailure('RECOVERY_REQUIRED')
            baseline = self.backend.preflight()
            for source in baseline.get('capture_sources', []):
                source = Path(source).resolve()
                if self.store.root.is_relative_to(source) or source.is_relative_to(self.store.root):
                    raise CaptureFailure('STAGING_SOURCE_OVERLAP')
            required = baseline.get('estimated_capture_bytes', 0) + 1024 * 1024
            if shutil.disk_usage(self.store.root).free < required:
                raise CaptureFailure('INSUFFICIENT_STAGING_DISK')
            operation_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + str(uuid.uuid4())
            directory = self.store.operation(operation_id)
            directory.mkdir(mode=0o700)
            sync_directory(self.store.root)
            self.store.reserve(directory)
            journal = {'internal_version': 1, 'kind': 'local-capture-journal', 'operation_id': operation_id,
                       'consistency_id': 'capture-' + str(uuid.uuid4()), 'original': baseline, 'events': [],
                       'stop_intents': [], 'maintenance_fingerprint': None, 'artifacts': {},
                       'status': 'incomplete', 'service_state_restored': False}
            journal['scope'] = {'local_only': True, 's3_consistency': 'not_checked',
                                'encryption': 'not_implemented', 'transfer': 'not_implemented',
                                'functional_restore': 'not_verified'}
            self.backend.context = {'operation_id': operation_id, 'consistency_id': journal['consistency_id']}
            self.event(journal, 'prepared')
            self.store.write('active.json', {'operation_id': operation_id, 'terminal': False})
            try:
                if (self.backend.maintenance() != baseline['maintenance']
                    or self.backend.fingerprint() != baseline['config_fingerprint']):
                    raise CaptureFailure('PREEXISTING_STATE_CHANGED')
                self.event(journal, 'maintenance_intent')
                if baseline['maintenance'] is False:
                    self.backend.set_maintenance(True)
                fingerprint = self.backend.fingerprint()
                self.event(journal, 'maintenance_owned', maintenance_fingerprint=fingerprint)
                for service, running in baseline['writers'].items():
                    if running:
                        intents = journal['stop_intents'] + [service]
                        self.event(journal, 'stop_intent', stop_intents=intents)
                        self.backend.stop_writer(service)
                self.backend.assert_quiescent(baseline)
                self.event(journal, 'quiesced', quiesced_at=utc())
                if self.inventory_hooks is not None:
                    self.event(journal, 'inventory_before_intent')
                    observation = self.inventory_hooks.before(journal)
                    self.backend.assert_quiescent(baseline)
                    self.event(journal, 'inventory_before_observed', inventory_observation=observation)
                self.event(journal, 'dump_intent')
                journal['artifacts']['database'] = self.store.artifact(directory, 'database.dump', self.backend.dump, self.backend.limit)
                self.backend.check_dump(directory / 'database.dump')
                self.event(journal, 'dump_catalog_checked')
                for role, name in [('configuration', 'configuration.tar'), ('local_files', 'local_files.tar'),
                                   ('recovery_parameters', 'recovery_parameters.json')]:
                    self.event(journal, role + '_intent')
                    journal['artifacts'][role] = self.store.artifact(directory, name,
                        lambda stream, role=role: self.backend.capture(role, stream), self.backend.limit)
                    self.event(journal, role + '_captured')
                self.backend.assert_quiescent(baseline)
                if self.inventory_hooks is not None:
                    self.event(journal, 'inventory_after_intent')
                    observation = self.inventory_hooks.after(journal)
                    # Confirm writer state again after the potentially long listing.
                    self.backend.assert_quiescent(baseline)
                    self.event(journal, 'inventory_after_observed', inventory_observation=observation)
                self.event(journal, 'local_capture_finished', capture_finished_at=utc())
                self.restore(journal)
                self.event(journal, 'candidate_ready', status='local_candidate_only')
                self.store.write('active.json', {'operation_id': operation_id, 'terminal': True})
                return {'code': 'LOCAL_CANDIDATE_ONLY', 'operation_id': operation_id,
                        'backup_complete': False, 'functional_restore': 'not_verified'}
            except BaseException as error:
                # Shield the entire failure journal/cleanup, not just backend restore,
                # against a second SIGTERM interrupting recovery before it starts.
                with cleanup_signals():
                    code = error.code if isinstance(error, CaptureFailure) else 'CAPTURE_INTERRUPTED'
                    if isinstance(error, OSError):
                        code = 'INSUFFICIENT_DISK' if error.errno == errno.ENOSPC else 'LOCAL_IO_ERROR'
                    self.store.release_reserve(directory)
                    self.event(journal, 'failed', status='incomplete', error_code=code, error_phase=journal['phase'])
                    try:
                        self.restore(journal)
                        self.store.write('active.json', {'operation_id': operation_id, 'terminal': True})
                    except BaseException:
                        self.event(journal, 'intervention_required', status='incomplete', error_code='RECOVERY_REQUIRED')
                        raise CaptureFailure('RECOVERY_REQUIRED') from None
                    raise CaptureFailure(code) from None

    def restore(self, journal):
        with cleanup_signals():
            self._restore(journal)

    def _restore(self, journal):
        original = journal['original']
        self.backend.assert_identity(original)
        current = self.backend.fingerprint()
        expected = journal['maintenance_fingerprint']
        if expected is None:
            # Crash in the maintenance intent window: cannot infer ownership from maintenance=true.
            if self.backend.maintenance() != original['maintenance'] or journal['stop_intents']:
                raise CaptureFailure('MAINTENANCE_OWNERSHIP_UNCERTAIN')
        elif current != expected:
            raise CaptureFailure('MAINTENANCE_OWNERSHIP_CHANGED')
        self.event(journal, 'restore_intent')
        for service in reversed(journal['stop_intents']):
            if original['writers'][service]:
                self.backend.start_writer(service)
        if self.backend.fingerprint() != current:
            raise CaptureFailure('CONFIG_CHANGED_DURING_RESTART')
        # Do not reopen writes while dependencies are unavailable. No DB/Redis restart here.
        self.backend.assert_original_services(original)
        # Only undo our recorded maintenance transition, never another operation's change.
        if expected is not None and original['maintenance'] is False and self.backend.maintenance() is True:
            self.event(journal, 'maintenance_restore_intent')
            self.backend.set_maintenance(False)
        if self.backend.maintenance() != original['maintenance']:
            raise CaptureFailure('ORIGINAL_MAINTENANCE_NOT_RESTORED')
        self.backend.assert_original_services(original)
        # Persist the post-restore fingerprint so a second recovery is idempotent
        # even if killed after maintenance was restored but before active terminal.
        journal['maintenance_fingerprint'] = self.backend.fingerprint()
        self.event(journal, 'service_state_restored', service_state_restored=True)

    def recover(self):
        with self.store.lock():
            active = self.store.read('active.json')
            if active.get('terminal') is True:
                return {'code': 'ALREADY_TERMINAL', 'backup_complete': False}
            operation_id = active['operation_id']
            self.store.operation(operation_id)
            journal = self.store.read(operation_id + '/journal.json')
            if not self.valid_journal(journal, operation_id):
                raise CaptureFailure('JOURNAL_MISMATCH')
            # Never restart pg_dump or reuse partial candidates during recovery.
            self.restore(journal)
            self.event(journal, 'abandoned_after_interruption', status='incomplete')
            self.store.write('active.json', {'operation_id': operation_id, 'terminal': True})
            return {'code': 'SERVICE_STATE_RECOVERED_CAPTURE_ABANDONED', 'backup_complete': False}

    @staticmethod
    def valid_journal(journal, operation_id):
        # Local journal is not a PR1 manifest. Validate all recovery control inputs
        # before invoking any backend operation, including unexpected service names.
        if not isinstance(journal, dict):
            return False
        original = journal.get('original')
        if not isinstance(original, dict):
            return False
        writers = original.get('writers')
        intents = journal.get('stop_intents')
        return (journal.get('operation_id') == operation_id
                and type(journal.get('internal_version')) is int and journal['internal_version'] == 1
                and journal.get('kind') == 'local-capture-journal'
                and journal.get('status') in ('incomplete', 'local_candidate_only')
                and isinstance(journal.get('events'), list)
                and type(original.get('maintenance')) is bool
                and isinstance(original.get('identity'), str) and bool(original['identity'])
                and isinstance(writers, dict) and set(writers) == {'nextcloud'}
                and all(type(value) is bool for value in writers.values())
                and isinstance(intents, list) and all(isinstance(value, str) for value in intents)
                and len(intents) == len(set(intents)) and set(intents) <= set(writers)
                and (journal.get('maintenance_fingerprint') is None
                     or isinstance(journal.get('maintenance_fingerprint'), str)
                     and bool(journal['maintenance_fingerprint'])))
