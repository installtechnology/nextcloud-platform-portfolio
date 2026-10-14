import io
import socket
import errno
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from scripts.backup.operational.docker_api import DockerExecAPI
from scripts.backup.capture.storage import CaptureFailure


def frame(channel, data): return bytes([channel,0,0,0]) + len(data).to_bytes(4, 'big') + data


class Socket:
    def __init__(self, data): self.data = data; self.closed = False
    def settimeout(self, timeout): self.timeout = timeout
    def recv(self, count):
        data, self.data = self.data[:min(count,3)], self.data[min(count,3):]
        return data


class API:
    def __init__(self, data): self.raw = Socket(data); self.calls = []
    def exec_start(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(_sock=self.raw, close=lambda: setattr(self.raw, 'closed', True))


class DockerTests(unittest.TestCase):
    def test_only_sdk_notfound_404_is_collected(self):
        class NotFound(Exception):
            def __init__(self,status):self.response=SimpleNamespace(status_code=status)
        sdk=SimpleNamespace(errors=SimpleNamespace(NotFound=NotFound))
        for error,expected in ((NotFound(404),'EXEC_RECORD_GONE'),(NotFound(403),'EXEC_INSPECTION_REQUIRED'),
                               (RuntimeError('synthetic-secret'),'EXEC_INSPECTION_REQUIRED')):
            with self.subTest(expected=expected),patch.dict('sys.modules',{'docker':sdk}):
                api=SimpleNamespace(exec_inspect=Mock(side_effect=error))
                with self.assertRaisesRegex(CaptureFailure,'^'+expected+'$'):
                    DockerExecAPI(api).inspect_exec('b'*64)
    def test_absent_finished_exec_requires_exact_running_container(self):
        container,identifier='a'*64,'b'*64
        good={'Id':container,'State':{'Running':True},'ExecIDs':None}
        for info,result in ((good,True),({**good,'Id':'c'*64},False),
                            ({**good,'State':{'Running':False}},False),
                            ({**good,'ExecIDs':[identifier]},False),
                            ({**good,'ExecIDs':'bad'},False),({**good,'ExecIDs':[None]},False)):
            with self.subTest(info=info):
                bridge=DockerExecAPI(SimpleNamespace(inspect_container=lambda c:info))
                with patch.object(bridge,'running_execs',return_value=[]):
                    self.assertIs(bridge.finished_exec_absent(container,identifier),result)
    def test_absent_finished_exec_unknown_running_exec_rejected(self):
        bridge=DockerExecAPI(SimpleNamespace(inspect_container=lambda c:{'Id':c,'State':{'Running':True},'ExecIDs':None}))
        with patch.object(bridge,'running_execs',return_value=['c'*64]):
            self.assertFalse(bridge.finished_exec_absent('a'*64,'b'*64))
    def test_absent_finished_exec_daemon_failure_sanitized(self):
        bridge=DockerExecAPI(SimpleNamespace(inspect_container=Mock(side_effect=TimeoutError('synthetic-secret'))))
        with self.assertRaisesRegex(CaptureFailure,'^EXEC_INSPECTION_REQUIRED$'):
            bridge.finished_exec_absent('a'*64,'b'*64)
    def invoke(self, data):
        api = API(data); output = io.BytesIO()
        DockerExecAPI(api).stream_exec('synthetic', output, 2)
        self.assertTrue(api.raw.closed)
        return output.getvalue()
    def test_chunked_framing_stdout_only(self):
        self.assertEqual(self.invoke(frame(1,b'first')+frame(2,b'synthetic-secret')+frame(1,b'last')), b'firstlast')
    def test_empty_stream(self): self.assertEqual(self.invoke(b''), b'')
    def test_truncated_header(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STREAM_TRUNCATED$'): self.invoke(b'\1\0')
    def test_truncated_frame(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STREAM_TRUNCATED$'): self.invoke(frame(1,b'hello')[:-2])
    def test_frame_bound(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_FRAME_LIMIT$'):
            self.invoke(b'\1\0\0\0' + (16*1024*1024+1).to_bytes(4,'big'))
    def test_channel_invalid(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STREAM_INVALID$'): self.invoke(frame(4,b'bad'))
    def test_timeout(self):
        api = API(b'')
        def recv(count): raise socket.timeout()
        api.raw.recv = recv
        with self.assertRaisesRegex(CaptureFailure, '^COMMAND_TIMEOUT$'):
            DockerExecAPI(api).stream_exec('synthetic', io.BytesIO(), 0.001)
        self.assertTrue(api.raw.closed)
    def test_stdin_special_stream_rejected_before_exec(self):
        api = API(b'')
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STREAM_FAILED$'):
            DockerExecAPI(api).stream_exec('synthetic', io.BytesIO(), 2, io.BytesIO(b'private'))
        self.assertFalse(api.calls)


class ExecDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.container, self.exec_id = 'a'*64, 'b'*64
        self.info = {'Id': self.container, 'ExecIDs': [self.exec_id]}
        self.observed = {'ID': self.exec_id, 'ContainerID': self.container, 'Running': True}
        self.api = SimpleNamespace(inspect_container=lambda c: self.info, exec_inspect=lambda e: self.observed)
        self.bridge = DockerExecAPI(self.api)
    def reject(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_INSPECTION_REQUIRED$'):
            self.bridge.running_execs(self.container)
    def test_running_exec_is_reported(self):
        self.assertEqual(self.bridge.running_execs(self.container), [self.exec_id])
    def test_finished_exec_is_not_running(self):
        self.observed['Running'] = False
        self.assertEqual(self.bridge.running_execs(self.container), [])
    def test_explicit_null_list_is_empty(self):
        self.info['ExecIDs'] = None
        self.assertEqual(self.bridge.running_execs(self.container), [])
    def test_missing_execids_is_not_empty(self):
        del self.info['ExecIDs']; self.reject()
    def test_container_mismatch(self):
        self.observed['ContainerID'] = 'c'*64; self.reject()
    def test_duplicate_execids(self):
        self.info['ExecIDs'] *= 2; self.reject()
    def test_ambiguous_running_value(self):
        self.observed['Running'] = 1; self.reject()
    def test_invalid_identifier(self):
        self.info['ExecIDs'] = ['sensitive-invalid']; self.reject()


class HealthcheckDrainTests(unittest.TestCase):
    def setUp(self):
        self.container, self.identifier = 'a'*64, 'b'*64
        self.info = {'Id': self.container, 'ExecIDs': [self.identifier],
            'Config': {'Healthcheck': {'Test': ['CMD', 'php', '/opt/platform/healthcheck.php']}}}
        self.observed = {'ID': self.identifier, 'ContainerID': self.container, 'Running': True,
            'ProcessConfig': {'entrypoint': 'php', 'arguments': ['/opt/platform/healthcheck.php'],
                'privileged': False, 'tty': False}}
        self.api = SimpleNamespace(inspect_container=lambda c: self.info,
            exec_inspect=lambda e: self.observed)
        self.bridge = DockerExecAPI(self.api)

    def test_natural_exit_requires_fresh_clear_scan(self):
        def finish(_): self.observed['Running'] = False
        with patch('scripts.backup.operational.docker_api.time.sleep', side_effect=finish) as sleep:
            self.assertEqual(self.bridge.running_execs(self.container), [])
        sleep.assert_called_once_with(0.1)

    def test_stuck_probe_remains_blocked_at_deadline(self):
        with patch('scripts.backup.operational.docker_api.time.monotonic', side_effect=[0, 11]):
            self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

    def test_unknown_command_is_not_waited_or_ignored(self):
        self.observed['ProcessConfig']['arguments'] = ['unknown.php']
        with patch('scripts.backup.operational.docker_api.time.sleep') as sleep:
            self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])
        sleep.assert_not_called()

    def test_changed_healthcheck_is_not_trusted(self):
        self.info['Config']['Healthcheck']['Test'] = ['CMD', 'php', 'unknown.php']
        self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

    def test_privileged_probe_remains_blocked(self):
        self.observed['ProcessConfig']['privileged'] = True
        self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

    def test_missing_process_metadata_remains_blocked(self):
        del self.observed['ProcessConfig']
        self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

    def test_new_unknown_exec_after_probe_exit_remains_blocked(self):
        def replace(_): self.observed['ProcessConfig']['arguments'] = ['unknown.php']
        with patch('scripts.backup.operational.docker_api.time.sleep', side_effect=replace):
            self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

    def test_reinspection_failure_does_not_clear_blocker(self):
        self.api.exec_inspect = Mock(side_effect=[self.observed, RuntimeError('synthetic-private')])
        self.assertEqual(self.bridge.running_execs(self.container), [self.identifier])

class CatalogStdinTests(unittest.TestCase):
    def invoke(self, command='pg_restore', args=None, exit_code=0, running=False,
               error=None, privileged=False, identifier='synthetic'):
        api = API(frame(1, b'synthetic catalog'))
        sent = threading.Event()
        def sendall(data):
            sent.set()
            raise error or BrokenPipeError(errno.EPIPE, 'synthetic peer closure')
        api.raw.sendall = sendall
        original_recv = api.raw.recv
        def recv(count):
            self.assertTrue(sent.wait(2))
            return original_recv(count)
        api.raw.recv = recv
        api.exec_inspect = lambda _: {'ID': identifier, 'Running': running,
            'ExitCode': exit_code, 'ProcessConfig': {'entrypoint': command,
            'arguments': ['--list'] if args is None else args,
            'privileged': privileged, 'tty': False}}
        with tempfile.TemporaryFile() as source:
            source.write(b'PGDMPsynthetic payload'); source.seek(0)
            DockerExecAPI(api).stream_exec('synthetic', io.BytesIO(), 2, source)
        self.assertTrue(api.raw.closed)

    def test_catalog_early_peer_close_requires_successful_exact_command(self):
        self.invoke()
    def test_nonzero_catalog_still_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(exit_code=1)
    def test_running_catalog_still_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(running=True)
    def test_other_stdin_command_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(command='psql')
    def test_restore_not_catalog_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(args=['--dbname=synthetic'])
    def test_wrong_identifier_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(identifier='other')
    def test_privileged_exec_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'): self.invoke(privileged=True)
    def test_unrelated_io_failure_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^COMMAND_TIMEOUT$'):
            self.invoke(error=OSError(errno.EIO, 'synthetic failure'))
    def test_shutdown_closed_socket_requires_same_proof(self):
        self.invoke(error=OSError(errno.ENOTCONN, 'synthetic peer closure'))
    def test_send_timeout_after_successful_catalog_is_not_command_timeout(self):
        self.invoke(error=socket.timeout())
    def test_send_timeout_nonzero_catalog_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'):
            self.invoke(error=socket.timeout(), exit_code=1)
    def test_send_timeout_other_command_rejected(self):
        with self.assertRaisesRegex(CaptureFailure, '^EXEC_STDIN_FAILED$'):
            self.invoke(error=socket.timeout(), command='psql')
