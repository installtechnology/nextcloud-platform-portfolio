import copy
import io
from pathlib import Path
import unittest
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.operational.supervision import Supervisor
from scripts.backup.operational.postgres import PostgreSQL

OP = '20260101T000000Z-01234567-89ab-4cde-8fab-0123456789ab'
BOOT = '01234567-89ab-4cde-8fab-0123456789ab'
CONTAINER = 'a' * 64
EXEC = 'b' * 64


class Store:
    root = Path('not-created-synthetic-test-store')
    def __init__(self): self.data = {}; self.writes = []
    def operation(self, op):
        if op != OP: raise CaptureFailure('INVALID_OPERATION_ID')
    def read(self, path): return copy.deepcopy(self.data[path])
    def write(self, path, value):
        self.data[path] = copy.deepcopy(value)
        self.writes.append(copy.deepcopy(value))


class Docker:
    def __init__(self, store):
        self.store = store; self.running = False; self.exit_code = 0
        self.failure = None; self.commands = []
    def create_exec(self, container, argv, user, stdin, app):
        assert self.store.writes[-1]['execs'][-1]['phase'] == 'create_intent'
        self.commands.append((container, argv, user, stdin, app))
        if self.failure == 'create': raise RuntimeError('synthetic-secret')
        return EXEC
    def stream_exec(self, identifier, output, timeout, source):
        assert self.store.writes[-1]['execs'][-1]['phase'] == 'start_intent'
        if self.failure == 'stream': raise TimeoutError('synthetic-secret')
        output.write(b'PGDMPsynthetic')
    def inspect_exec(self, identifier):
        if self.failure == 'inspect': raise RuntimeError('synthetic-secret')
        return {'ID': EXEC, 'ContainerID': CONTAINER, 'Running': self.running, 'ExitCode': self.exit_code}


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.docker = Docker(self.store)
        self.supervisor = Supervisor(self.store, self.docker, BOOT)
        self.supervisor.initialize(OP)
    def code(self, code, action):
        with self.assertRaises(CaptureFailure) as caught: action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('synthetic-secret', str(caught.exception))
    def execute(self):
        return self.supervisor.execute(OP, CONTAINER, ['pg_dump', '--format=custom'], 'postgres', io.BytesIO())
    def test_intents_durable_before_each_side_effect(self):
        self.execute()
        self.assertEqual([w['execs'][-1]['phase'] for w in self.store.writes[1:]], ['create_intent', 'start_intent', 'finished'])
        self.supervisor.assert_idle(OP)
    def test_create_unknown_blocks_and_does_not_retry(self):
        self.docker.failure = 'create'; self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.assertEqual(self.supervisor.blockers(OP), ['UNTRACKED_CREATE_INTENT'])
        self.code('ORPHANED_CAPTURE_PRESENT', self.execute)
        self.assertEqual(len(self.docker.commands), 1)
    def test_client_timeout_not_daemon_termination(self):
        self.docker.failure = 'stream'; self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.docker.failure = None
        self.code('ORPHANED_CAPTURE_PRESENT', lambda: self.supervisor.assert_idle(OP))
    def test_running_exec_blocks(self):
        self.docker.running = True; self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.code('ORPHANED_CAPTURE_PRESENT', lambda: self.supervisor.assert_idle(OP))
    def test_nonzero_exit_preserves_exact_error(self):
        self.docker.exit_code = 4; self.code('EXEC_FAILED', self.execute)
        self.assertEqual(self.store.writes[-1]['execs'][-1]['exit_code'], 4)
        self.supervisor.assert_idle(OP)  # Terminated is not a successful dump.
    def test_reboot_never_auto_recovers(self):
        other = Supervisor(self.store, self.docker, '11234567-89ab-4cde-8fab-0123456789ab')
        self.code('CAPTURE_BOOT_CHANGED', lambda: other.assert_idle(OP))
    def test_inspection_failure_sanitized(self):
        self.execute(); self.docker.failure = 'inspect'
        self.code('EXEC_INSPECTION_REQUIRED', lambda: self.supervisor.assert_idle(OP))
    def gone(self):
        self.execute()
        self.docker.inspect_exec = lambda identifier: (_ for _ in ()).throw(CaptureFailure('EXEC_RECORD_GONE'))
        self.docker.finished_exec_absent = lambda container, identifier: True
    def test_collected_successful_record_same_boot_clear_container(self):
        self.gone();before=copy.deepcopy(self.store.data)
        self.assertEqual(self.supervisor.blockers(OP), [])
        self.assertEqual(self.store.data,before)
    def test_collected_nonzero_is_terminated_not_successful(self):
        self.gone();self.store.data[self.supervisor.registry(OP)]['execs'][0]['exit_code']=1
        self.assertEqual(self.supervisor.blockers(OP),[])
        self.assertEqual(self.store.data[self.supervisor.registry(OP)]['execs'][0]['exit_code'],1)
    def test_collected_invalid_exit_rejected(self):
        self.gone()
        for code in (-1,256,True):
            self.store.data[self.supervisor.registry(OP)]['execs'][0]['exit_code']=code
            self.code('EXEC_JOURNAL_INVALID' if code is True else 'EXEC_INSPECTION_REQUIRED',lambda:self.supervisor.blockers(OP))
    def test_collected_unfinished_record_rejected(self):
        self.gone();self.store.data[self.supervisor.registry(OP)]['execs'][0]['phase']='inspection_required'
        self.code('EXEC_INSPECTION_REQUIRED',lambda:self.supervisor.blockers(OP))
    def test_collected_record_changed_boot_rejected(self):
        self.gone();self.store.data[self.supervisor.registry(OP)]['boot_id']='11234567-89ab-4cde-8fab-0123456789ab'
        self.code('CAPTURE_BOOT_CHANGED',lambda:self.supervisor.blockers(OP))
    def test_absence_confirmation_false_or_nonboolean_rejected(self):
        self.gone()
        for result in (False,None,1,[],{}):
            with self.subTest(result=result):
                self.docker.finished_exec_absent=lambda c,e:result
                self.code('EXEC_INSPECTION_REQUIRED',lambda:self.supervisor.blockers(OP))
    def test_absence_confirmation_failure_sanitized(self):
        self.gone()
        def failed(c,e):raise RuntimeError('synthetic-secret')
        self.docker.finished_exec_absent=failed
        self.code('EXEC_INSPECTION_REQUIRED',lambda:self.supervisor.blockers(OP))
    def test_new_supervised_query_after_collected_history(self):
        self.gone();original=copy.deepcopy(self.store.data[self.supervisor.registry(OP)]['execs'][0])
        fresh='c'*64
        self.docker.create_exec=lambda *args:fresh
        def inspect(identifier):
            if identifier==EXEC:raise CaptureFailure('EXEC_RECORD_GONE')
            return {'ID':fresh,'ContainerID':CONTAINER,'Running':False,'ExitCode':0}
        self.docker.inspect_exec=inspect
        self.supervisor.execute(OP,CONTAINER,['psql','-X','-c','SELECT 1'],'postgres',io.BytesIO())
        records=self.store.data[self.supervisor.registry(OP)]['execs']
        self.assertEqual(records[0],original)
        self.assertEqual(records[1]['phase'],'finished')
        self.assertEqual(records[1]['exit_code'],0)
        self.assertEqual(self.supervisor.blockers(OP),[])
    def test_malformed_boot(self):
        self.code('BOOT_ID_REQUIRED', lambda: Supervisor(self.store, self.docker, '-' * 36))
    def test_exec_allowlist(self):
        self.code('EXEC_CONFIGURATION_INVALID', lambda: self.supervisor.execute(OP, CONTAINER, ['sh', '-c', 'evil'], 'postgres', io.BytesIO()))
    def test_reconcile_requires_absent_sessions(self):
        self.docker.failure = 'stream'; self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.docker.failure = None
        self.code('DATABASE_SESSIONS_PRESENT', lambda: self.supervisor.reconcile(OP, lambda: [123]))
        self.assertEqual(self.supervisor.reconcile(OP, lambda: [])['backup_complete'], False)
        self.supervisor.assert_idle(OP)
    def test_reconcile_unknown_create_cannot_clear(self):
        self.docker.failure = 'create'; self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.code('UNTRACKED_CREATE_INTENT', lambda: self.supervisor.reconcile(OP, lambda: []))
    def test_reconcile_unknown_sessions(self):
        self.code('SESSION_INSPECTION_REQUIRED', lambda: self.supervisor.reconcile(OP, lambda: None))
    def test_journal_bad_container(self):
        self.execute(); path = self.supervisor.registry(OP)
        self.store.data[path]['execs'][0]['container_id'] = 'wrong'
        self.code('EXEC_JOURNAL_INVALID', lambda: self.supervisor.blockers(OP))
    def test_controller_interruption_stays_blocked(self):
        def interrupted(*args): raise KeyboardInterrupt()
        self.docker.stream_exec = interrupted
        self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.code('ORPHANED_CAPTURE_PRESENT', lambda: self.supervisor.assert_idle(OP))
    def test_disk_failure_before_create_has_no_daemon_side_effect(self):
        def failed(*args): raise OSError('synthetic-secret ENOSPC')
        self.store.write = failed
        self.code('EXEC_JOURNAL_WRITE_FAILED', self.execute)
        self.assertFalse(self.docker.commands)
    def test_durable_start_failure_never_starts_exec(self):
        original = self.store.write
        def failed(path, value):
            if value['execs'] and value['execs'][-1]['phase'] == 'start_intent':
                raise OSError('synthetic-secret ENOSPC')
            original(path, value)
        self.store.write = failed
        self.code('EXEC_INSPECTION_REQUIRED', self.execute)
        self.assertEqual(self.store.writes[-1]['execs'][-1]['phase'], 'inspection_required')
    def test_unknown_journal_read_sanitized(self):
        def failed(*args): raise OSError('synthetic-sensitive-path')
        self.store.read = failed
        self.code('EXEC_JOURNAL_UNREADABLE', lambda: self.supervisor.assert_idle(OP))


class UnknownExecTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.docker = Docker(self.store)
        self.supervisor = Supervisor(self.store, self.docker, BOOT)
        self.supervisor.initialize(OP)
    def check(self): self.supervisor.assert_no_untracked_execs(OP, [CONTAINER])
    def test_untracked_running_exec_blocks(self):
        self.docker.running_execs = lambda c: [EXEC]
        with self.assertRaisesRegex(CaptureFailure, '^ORPHANED_CAPTURE_PRESENT$'): self.check()
    def test_empty_verified_execs_permitted(self):
        self.docker.running_execs = lambda c: []
        self.check()
    def test_missing_collector_fails_closed(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_INSPECTION_REQUIRED$'): self.check()
    def test_collector_error_sanitized(self):
        def broken(c): raise OSError('synthetic-sensitive')
        self.docker.running_execs = broken
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_INSPECTION_REQUIRED$'): self.check()


class PGTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.calls = []; self.response = b'{"sessions":[]}'
        class Bridge:
            store = self.store
            def execute(_, *args):
                self.calls.append(args)
                args[4].write(self.response)
        self.pg = PostgreSQL(Bridge(), OP, CONTAINER, 'nextcloud')
    def test_dump_custom_no_password(self):
        self.pg.dump(io.BytesIO()); argv = self.calls[-1][2]
        self.assertIn('--format=custom', argv); self.assertIn('--create', argv)
        self.assertIn('--host=/var/run/postgresql', argv)
        self.assertNotIn('--password', argv)
    def test_globals_exclude_passwords(self):
        self.pg.globals(); self.assertIn('--no-role-passwords', self.calls[-1][2])
    def test_sessions_include_external_connections(self):
        self.response = b'{"sessions":[11,12]}'; self.assertEqual(self.pg.sessions(), [11,12])
    def test_sessions_malformed_fails_closed(self):
        self.response = b'{"sessions":[true]}'
        with self.assertRaisesRegex(CaptureFailure, '^SESSION_INSPECTION_REQUIRED$'): self.pg.sessions()
    def test_catalog_header_bad(self):
        with self.assertRaisesRegex(CaptureFailure, '^DUMP_FORMAT_INVALID$'): self.pg.check_dump(io.BytesIO(b'bad'))
        self.assertFalse(self.calls)
    def test_catalog_does_not_restore(self):
        self.pg.check_dump(io.BytesIO(b'PGDMPsynthetic'))
        self.assertEqual(self.calls[-1][2], ['pg_restore', '--list'])


class PostgreSQLCompatibilityTests(unittest.TestCase):
    def probe(self, server=b'170011\n', client=b'pg_dump (PostgreSQL) 17.11\n'):
        from unittest.mock import Mock
        supervisor=Mock();supervisor.store=Store()
        def execute(operation, container, argv, user, output, timeout, *rest):
            if argv[0]=='psql':output.write(server)
            else:
                # Reproduce the early --version handling: it must be argv[1].
                if argv!=['pg_dump','--version']:raise CaptureFailure('EXEC_FAILED')
                output.write(client)
        supervisor.execute.side_effect=execute
        return PostgreSQL(supervisor,OP,CONTAINER,'synthetic'),supervisor

    def test_version_is_supervised_without_connection_flags(self):
        pg,supervisor=self.probe();pg.compatibility()
        calls=supervisor.execute.call_args_list
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0].args[2][:3],['psql','--host=/var/run/postgresql','--username=postgres'])
        self.assertEqual(calls[1].args[:4],(OP,CONTAINER,['pg_dump','--version'],'postgres'))
        self.assertEqual(calls[1].args[5],30)

    def test_other_dump_major_remains_rejected(self):
        pg,_=self.probe(client=b'pg_dump (PostgreSQL) 16.9\n')
        with self.assertRaises(CaptureFailure) as failure:pg.compatibility()
        self.assertEqual(failure.exception.code,'DUMP_CLIENT_MAJOR_MISMATCH')

    def test_server_major_rejected_before_client_probe(self):
        pg,supervisor=self.probe(server=b'160009\n')
        with self.assertRaises(CaptureFailure) as failure:pg.compatibility()
        self.assertEqual(failure.exception.code,'POSTGRES_MAJOR_MISMATCH')
        self.assertEqual(supervisor.execute.call_count,1)

    def test_dump_and_sql_keep_unix_socket_authentication(self):
        pg,supervisor=self.probe()
        supervisor.execute.side_effect=None
        pg.dump(io.BytesIO())
        argv=supervisor.execute.call_args.args[2]
        self.assertEqual(argv,['pg_dump','--host=/var/run/postgresql','--username=postgres','--format=custom','--create','--dbname','synthetic'])
