"""Strict PostgreSQL PID envelope; synthetic queries only, no operational clients."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.errors import ValidationFailure
from scripts.backup.operational.host_supervisor import HostSupervisor


class HostSupervisorPidsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SimpleNamespace(root=Path(self.tmp.name),operation=Mock(),read=Mock())
        self.docker=SimpleNamespace(running_execs=Mock(return_value=[]))
        self.pg=SimpleNamespace(database='synthetic',containers=['synthetic-container'],command=Mock())
        self.supervisor=HostSupervisor(self.store,self.docker,'synthetic-boot',self.pg)
    def inspect(self,raw):
        def command(executable,args,output,timeout):output.write(raw)
        self.pg.command.side_effect=command
        return self.supervisor.inspect()
    def reject(self,raw,code):
        with self.assertRaises((CaptureFailure,ValidationFailure)) as caught:self.inspect(raw)
        self.assertEqual(caught.exception.code,code)
    def test_empty_object_envelope_no_orphan(self):
        self.assertEqual(self.inspect(b'{"pids":[]}'),[])
    def test_positive_pids_block_and_are_not_terminated(self):
        self.assertEqual(self.inspect(b'{"pids":[12,345]}'),['ORPHANED_POSTGRES_CAPTURE_SESSION'])
        self.store.operation.assert_not_called();self.store.read.assert_not_called()
    def test_query_targets_capture_backend_pids_and_excludes_itself(self):
        self.inspect(b'{"pids":[]}');executable,args,output,timeout=self.pg.command.call_args.args
        self.assertEqual(executable,'psql');self.assertEqual(timeout,30)
        self.assertIn('--dbname',args);self.assertIn('synthetic',args)
        self.assertEqual(args[-1],"SELECT json_build_object('pids',coalesce(json_agg(pid ORDER BY pid),'[]'::json)) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND application_name LIKE 'ncbackup-%'")
    def test_array_root_still_rejected(self):self.reject(b'[]','JSON_OBJECT_REQUIRED')
    def test_scalar_root_rejected(self):self.reject(b'null','JSON_OBJECT_REQUIRED')
    def test_missing_property_rejected(self):self.reject(b'{}','HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_extra_property_rejected(self):self.reject(b'{"pids":[],"approved":true}','HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_null_pids_rejected(self):self.reject(b'{"pids":null}','HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_wrong_pids_type_rejected(self):self.reject(b'{"pids":{}}','HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_duplicate_key_rejected(self):self.reject(b'{"pids":[],"pids":[]}','DUPLICATE_JSON_KEY')
    def test_malformed_json_rejected(self):self.reject(b'{"pids":','INVALID_JSON')
    def test_duplicate_pid_rejected(self):self.reject(b'{"pids":[12,12]}','HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_invalid_pid_values_rejected(self):
        for value in (b'0',b'-1',b'2147483648',b'true',b'"12"',b'null',b'{}',b'[]'):
            with self.subTest(value=value):self.reject(b'{"pids":['+value+b']}', 'HOST_POSTGRES_INSPECTION_REQUIRED')
    def test_fractional_pid_rejected(self):self.reject(b'{"pids":[1.5]}','INVALID_JSON')
    def test_untracked_docker_exec_still_blocks(self):
        self.docker.running_execs.return_value=['synthetic-exec']
        self.assertEqual(self.inspect(b'{"pids":[]}'),['UNTRACKED_DOCKER_EXEC'])
    def test_database_command_failure_propagates_without_success(self):
        self.pg.command.side_effect=CaptureFailure('POSTGRES_EXEC_FAILED')
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_EXEC_FAILED$'):self.supervisor.inspect()
