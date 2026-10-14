"""Docker SDK bridge; explicit construction, injected low-level API client.

No SDK import/install, daemon connection, network or process at module import.
Socket streaming handles stdout/stderr framing and bounded stdin explicitly.
"""
import socket
import errno
import os
import stat
import threading
import time
from ..capture.storage import CaptureFailure


class DockerExecAPI:
    def __init__(self, api):
        self.api = api

    def create_exec(self, container, argv, user, stdin, application_name):
        try:
            response = self.api.exec_create(container=container, cmd=argv, user=user,
                stdout=True, stderr=True, stdin=stdin, tty=False,
                environment={'PGAPPNAME': application_name})
            return response['Id']
        except Exception:
            raise CaptureFailure('EXEC_CREATE_FAILED') from None

    def inspect_exec(self, identifier):
        try:
            return self.api.exec_inspect(identifier)
        except Exception as error:
            # Only the SDK's explicit NotFound response is absence. Connection,
            # permission and daemon errors must never be interpreted as GC.
            try:
                import docker
            except ImportError:
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None
            if (isinstance(error, docker.errors.NotFound)
                    and getattr(error.response, 'status_code', None) == 404):
                raise CaptureFailure('EXEC_RECORD_GONE') from None
            raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None

    def finished_exec_absent(self, container, identifier):
        """Read-only confirmation for a durably terminated historical exec."""
        try:
            info = self.api.inspect_container(container)
            ids = info.get('ExecIDs')
            if (info.get('Id') != container or info.get('State', {}).get('Running') is not True
                    or not (ids is None or isinstance(ids, list))
                    or (isinstance(ids, list) and (identifier in ids or
                        any(not isinstance(i, str) for i in ids)))):
                return False
            return self.running_execs(container) == []
        except Exception:
            raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None

    def running_execs(self, container):
        # Docker registers health probes as execs too. Never hide a running
        # probe: wait for its natural exit, then require a fresh clear scan.
        # Unknown commands remain immediate blockers; no process is stopped.
        deadline = time.monotonic() + 10
        while True:
            result = self._running_execs(container)
            if not result or not self._only_known_healthchecks(container, result):
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(0.1)

    def _only_known_healthchecks(self, container, identifiers):
        known = (
            ['CMD', 'php', '/opt/platform/healthcheck.php'],
            ['CMD-SHELL', 'pg_isready -h 127.0.0.1 -U postgres -d "$POSTGRES_DB"'],
            ['CMD-SHELL', 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --no-auth-warning ping | grep -qx PONG'],
        )
        try:
            info = self.api.inspect_container(container)
            config = info['Config']
            test = config['Healthcheck']['Test']
            if info.get('Id') != container or test not in known:
                return False
            argv = test[1:] if test[0] == 'CMD' else ['/bin/sh', '-c', test[1]]
            if test[0] == 'CMD-SHELL' and config.get('Shell') not in (None, [], ['/bin/sh', '-c']):
                return False
            for identifier in identifiers:
                observed = self.inspect_exec(identifier)
                process = observed.get('ProcessConfig', {})
                if (observed.get('ID') != identifier or observed.get('ContainerID') != container
                        or type(observed.get('Running')) is not bool
                        or process.get('entrypoint') != argv[0]
                        or process.get('arguments') != argv[1:]
                        or process.get('privileged') is not False
                        or process.get('tty') is not False):
                    return False
            return True
        except Exception:
            # Classification failure grants no exemption from the raw blocker.
            return False

    def _running_execs(self, container):
        import re
        try:
            info = self.api.inspect_container(container)
            identifiers = info['ExecIDs']
            if identifiers is None:
                identifiers = []
            if (info.get('Id') != container or not isinstance(identifiers, list)
                    or len(identifiers) > 1000 or len(set(identifiers)) != len(identifiers)):
                raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
            result = []
            for identifier in identifiers:
                if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier):
                    raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
                observed = self.inspect_exec(identifier)
                if (observed.get('ID') != identifier or observed.get('ContainerID') != container
                        or type(observed.get('Running')) is not bool):
                    raise CaptureFailure('EXEC_INSPECTION_REQUIRED')
                if observed['Running']:
                    result.append(identifier)
            return result
        except Exception:
            raise CaptureFailure('EXEC_INSPECTION_REQUIRED') from None

    def stream_exec(self, identifier, output, timeout, input_file=None):
        channel = None
        failure = []
        writer = None
        end = time.monotonic() + timeout
        try:
            if input_file is not None:
                # A pipe/FIFO could block the writer after the controller timeout.
                # Production stdin must be a previously confined regular file.
                info = os.fstat(input_file.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise CaptureFailure('EXEC_STDIN_REGULAR_FILE_REQUIRED')
            channel = self.api.exec_start(identifier, detach=False, tty=False, socket=True)
            raw = channel._sock
            raw.settimeout(min(1.0, timeout))
            def send():
                try:
                    while True:
                        if time.monotonic() >= end:
                            raise CaptureFailure('COMMAND_TIMEOUT')
                        chunk = input_file.read(1024 * 1024)
                        if not chunk: break
                        raw.sendall(chunk)
                    raw.shutdown(socket.SHUT_WR)
                except BaseException as error:
                    failure.append(error)
            if input_file is not None:
                writer = threading.Thread(target=send, daemon=True)
                writer.start()
            def read(size, allow_eof=False):
                result = bytearray()
                while len(result) < size:
                    if (failure and not self._catalog_stdin_closed(failure[0])) or time.monotonic() >= end:
                        raise CaptureFailure('COMMAND_TIMEOUT')
                    try:
                        chunk = raw.recv(min(1024 * 1024, size-len(result)))
                    except socket.timeout:
                        continue
                    if not chunk:
                        if not result and allow_eof: return b''
                        raise CaptureFailure('EXEC_STREAM_TRUNCATED')
                    result.extend(chunk)
                return bytes(result)
            while header := read(8, allow_eof=True):
                if header[0] not in (1,2) or header[1:4] != b'\0\0\0':
                    raise CaptureFailure('EXEC_STREAM_INVALID')
                remaining = int.from_bytes(header[4:8], 'big')
                if remaining > 16 * 1024 * 1024:
                    raise CaptureFailure('EXEC_FRAME_LIMIT')
                while remaining:
                    chunk = read(min(1024 * 1024, remaining))
                    if header[0] == 1: output.write(chunk)
                    elif hasattr(output, 'write_stderr'): output.write_stderr(chunk)
                    remaining -= len(chunk)
            if writer:
                writer.join(timeout=max(0, end-time.monotonic()))
                if writer.is_alive(): raise CaptureFailure('EXEC_STDIN_FAILED')
                if failure:
                    # pg_restore --list only reads the archive catalog. It may
                    # finish before consuming payload bytes. Accept early EOF
                    # only for that exact command with independently observed
                    # successful termination; never infer success from a socket.
                    observed = self.inspect_exec(identifier)
                    process = observed.get('ProcessConfig', {})
                    if (not self._catalog_stdin_closed(failure[0])
                            or time.monotonic() >= end
                            or observed.get('ID') != identifier
                            or observed.get('Running') is not False
                            or type(observed.get('ExitCode')) is not int
                            or observed['ExitCode'] != 0
                            or process.get('entrypoint') != 'pg_restore'
                            or process.get('arguments') != ['--list']
                            or process.get('privileged') is not False
                            or process.get('tty') is not False):
                        raise CaptureFailure('EXEC_STDIN_FAILED')
        except CaptureFailure:
            raise
        except BaseException:
            raise CaptureFailure('EXEC_STREAM_FAILED') from None
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass  # Never expose raw socket diagnostics during unwinding.
            if writer and writer.is_alive():
                writer.join(timeout=2)

    @staticmethod
    def _catalog_stdin_closed(error):
        # Docker may keep the hijacked write side open after the catalog reader
        # exits, yielding a socket send timeout rather than EPIPE. This only
        # defers judgment to exact-command, EOF and exit-code checks above.
        return (isinstance(error, socket.timeout)
                or isinstance(error, OSError) and error.errno in (errno.EPIPE, errno.ENOTCONN))
