"""Durable Docker exec ownership. No automatic kills or maintenance recovery.

Intent precedes exec creation; registry is fsynced by PR3 Store.write before
start. Unknown create/start outcomes remain blockers after controller death.
"""
import hashlib
import re
from ..capture.storage import CaptureFailure


class Supervisor:
    def __init__(self, store, docker, boot_id):
        if not isinstance(boot_id, str) or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', boot_id):
            raise CaptureFailure('BOOT_ID_REQUIRED')
        self.store, self.docker, self.boot_id = store, docker, boot_id

    def registry(self, operation):
        self.store.operation(operation)
        return operation + '/exec-journal.json'

    def load(self, operation):
        path = self.registry(operation)
        try:
            value = self.store.read(path)
        except Exception:
            raise CaptureFailure('EXEC_JOURNAL_UNREADABLE') from None
        if (not isinstance(value, dict) or set(value) != {'version', 'operation_id', 'boot_id', 'execs'}
                or type(value.get('version')) is not int or value['version'] != 1
                or value.get('operation_id') != operation or not isinstance(value.get('execs'), list)
                or len(value['execs']) > 1000 or not isinstance(value.get('boot_id'), str)):
            raise CaptureFailure('EXEC_JOURNAL_INVALID')
        return value

    def save(self, operation, value):
        path = self.registry(operation)
        try:
            self.store.write(path, value)
        except Exception:
            raise CaptureFailure('EXEC_JOURNAL_WRITE_FAILED') from None

    def initialize(self, operation):
        path = self.registry(operation)
        if (self.store.root / path).exists() or (self.store.root / path).is_symlink():
            raise CaptureFailure('EXEC_JOURNAL_EXISTS')
        self.save(operation, {'version': 1, 'operation_id': operation,
                             'boot_id': self.boot_id, 'execs': []})

    def execute(self, operation, container, argv, user, output, timeout=600, input_file=None):
        if (not isinstance(container, str) or not re.fullmatch(r'[a-f0-9]{64}', container)
                or user not in ('postgres', 'www-data') or not isinstance(argv, list)
                or not argv or argv[0] not in ('pg_dump', 'pg_dumpall', 'pg_restore', 'psql', 'php')
                or any(not isinstance(a, str) or '\x00' in a for a in argv)
                or len(argv) > 100 or sum(len(a) for a in argv) > 65536
                or type(timeout) is not int or not 1 <= timeout <= 3600):
            raise CaptureFailure('EXEC_CONFIGURATION_INVALID')
        state = self.load(operation)
        if state['boot_id'] != self.boot_id:
            raise CaptureFailure('CAPTURE_BOOT_CHANGED')
        self.assert_idle(operation)
        if len(state['execs']) >= 1000:
            raise CaptureFailure('EXEC_JOURNAL_LIMIT')
        record = {'container_id': container, 'exec_id': None, 'phase': 'create_intent',
                  'command_sha256': hashlib.sha256('\0'.join(argv).encode()).hexdigest(),
                  'exit_code': None}
        state['execs'].append(record)
        self.save(operation, state)
        try:
            identifier = self.docker.create_exec(container, argv, user, input_file is not None,
                                                 'ncbackup-' + operation)
            if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier):
                raise CaptureFailure('EXEC_ID_INVALID')
            record.update(exec_id=identifier, phase='start_intent')
            self.save(operation, state)
            self.docker.stream_exec(identifier, output, timeout, input_file)
            observed = self.docker.inspect_exec(identifier)
            if (observed.get('ID') != identifier or observed.get('ContainerID') != container
                    or observed.get('Running') is not False or type(observed.get('ExitCode')) is not int):
                raise CaptureFailure('EXEC_STATE_AMBIGUOUS')
            record.update(phase='finished', exit_code=observed['ExitCode'])
            self.save(operation, state)
            if observed['ExitCode'] != 0:
                raise CaptureFailure('EXEC_FAILED')
        except BaseException as error:
            # No inference from socket/client exit. Never erase the prior intent.
            if record['phase'] != 'finished':
                record['phase'] = 'inspection_required'
                try:
                    self.save(operation, state)
                except Exception:
                    pass
            elif isinstance(error, CaptureFailure):
                raise error
            raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None

    def blockers(self, operation):
        state = self.load(operation)
        if state['boot_id'] != self.boot_id:
            raise CaptureFailure('CAPTURE_BOOT_CHANGED')
        blockers = []
        for record in state['execs']:
            if (not isinstance(record, dict) or set(record) != {
                    'container_id', 'exec_id', 'phase', 'command_sha256', 'exit_code'}
                    or not isinstance(record.get('command_sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', record['command_sha256'])
                    or record.get('phase') not in (
                    'create_intent','start_intent','finished','inspection_required') or (
                    not isinstance(record.get('container_id'), str) or not re.fullmatch(r'[a-f0-9]{64}', record['container_id']))
                    or (record.get('phase') == 'finished' and type(record.get('exit_code')) is not int)):
                raise CaptureFailure('EXEC_JOURNAL_INVALID')
            identifier = record.get('exec_id')
            if identifier is None:
                blockers.append('UNTRACKED_CREATE_INTENT')
                continue
            if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier):
                raise CaptureFailure('EXEC_JOURNAL_INVALID')
            try:
                observed = self.docker.inspect_exec(identifier)
                if not isinstance(observed, dict):
                    raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
            except CaptureFailure as error:
                # Docker garbage-collects finished exec records. A same-boot
                # journal written after confirmed daemon termination remains
                # evidence; a 404 alone proves neither completion nor safety.
                if (error.code == 'EXEC_RECORD_GONE' and record['phase'] == 'finished'
                        and type(record['exit_code']) is int and 0 <= record['exit_code'] <= 255):
                    try:
                        absent = self.docker.finished_exec_absent(record['container_id'], identifier)
                    except Exception:
                        raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
                    if absent is True:
                        continue
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
            except Exception:
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
            if (observed.get('ID') != identifier or observed.get('ContainerID') != record.get('container_id')
                    or observed.get('Running') is not False or record.get('phase') != 'finished'
                    or observed.get('ExitCode') != record.get('exit_code')):
                blockers.append('EXEC_UNRESOLVED')
        return blockers

    def assert_idle(self, operation):
        if self.blockers(operation):
            raise CaptureFailure('ORPHANED_CAPTURE_PRESENT')

    def assert_no_untracked_execs(self, operation, containers):
        self.assert_idle(operation)
        if (not isinstance(containers, list) or not containers or len(set(containers)) != len(containers)
                or any(not isinstance(c, str) or not re.fullmatch(r'[a-f0-9]{64}', c) for c in containers)):
            raise CaptureFailure('EXEC_CONFIGURATION_INVALID')
        # Even a known exec cannot be running once assert_idle has passed.
        # Healthchecks are not silently exempted by command or application name.
        for container in containers:
            try:
                running = self.docker.running_execs(container)
            except Exception:
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
            if not isinstance(running, list):
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
            if running:
                raise CaptureFailure('ORPHANED_CAPTURE_PRESENT')

    def reconcile(self, operation, inspect_database_sessions):
        """Explicit diagnosis only: no maintenance change or capture resume.

        Reconciliation can prove an exec ended, never resolve an unknown create.
        Unknown/reboot states require administrative investigation, not TTL expiry.
        Caller must hold Store.lock and supply a complete PG session collector.
        """
        state = self.load(operation)
        if state['boot_id'] != self.boot_id:
            raise CaptureFailure('CAPTURE_BOOT_CHANGED')
        try:
            sessions = inspect_database_sessions()
        except Exception:
            raise CaptureFailure('SESSION_INSPECTION_REQUIRED') from None
        if not isinstance(sessions, list):
            raise CaptureFailure('SESSION_INSPECTION_REQUIRED')
        if sessions:
            raise CaptureFailure('DATABASE_SESSIONS_PRESENT')
        self.blockers(operation)  # Validate records; unresolved phases are expected.
        updates = []
        for record in state['execs']:
            if record['exec_id'] is None:
                raise CaptureFailure('UNTRACKED_CREATE_INTENT')
            try:
                observed = self.docker.inspect_exec(record['exec_id'])
            except Exception:
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
            if (not isinstance(observed, dict) or observed.get('ID') != record['exec_id']
                    or observed.get('ContainerID') != record['container_id']
                    or observed.get('Running') is not False or type(observed.get('ExitCode')) is not int):
                raise CaptureFailure('EXEC_STATE_AMBIGUOUS')
            updates.append((record, observed['ExitCode']))
        for record, code in updates:
            record.update(phase='finished', exit_code=code)
        self.save(operation, state)
        return {'code': 'EXEC_TERMINATION_VERIFIED_NO_RESUME', 'backup_complete': False}
