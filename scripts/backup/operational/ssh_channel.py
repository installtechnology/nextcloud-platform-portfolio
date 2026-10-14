"""SSM plugin + verified SSH stdio. Local socket private; no direct SSH path."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from .ssm import SSMChannel, ssh_options, need
from .remote_lease import AuthenticatedHostChannel
from ..capture.storage import CaptureFailure
from ..json_input import parse_json
from ..limits import Limits


def private_file(path):
    path = Path(path)
    need(path.is_absolute(), 'SSH_PRIVATE_INPUT_REQUIRED')
    # Every ancestor is examined, not only the final component.
    for part in (path, *path.parents): need(not part.is_symlink(), 'SSH_PRIVATE_INPUT_REQUIRED')
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
         and not stat.S_IMODE(info.st_mode) & 0o077, 'SSH_PRIVATE_INPUT_REQUIRED')
    return path


def stable_stat(info):
    return tuple(getattr(info,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_nlink','st_uid'))


def verified_plugin(path, expected_sha256, expected_version='1.2.707.0'):
    import re
    need(re.fullmatch(r'[a-f0-9]{64}', expected_sha256 or '') is not None, 'PLUGIN_DIGEST_REQUIRED')
    path = private_file(path)
    # Official Unix build has a fixed global config path, independent of HOME.
    # No existing debug configuration may override the verified default (off).
    need(not Path('/usr/local/sessionmanagerplugin/seelog.xml').exists()
         and not Path('/usr/local/sessionmanagerplugin/seelog.xml').is_symlink(), 'PLUGIN_LOGGING_OVERRIDE_REJECTED')
    before = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    need(stable_stat(before) == stable_stat(path.stat()) and digest == expected_sha256, 'PLUGIN_DIGEST_MISMATCH')
    try:
        result = subprocess.run([str(path), '--version'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=10, check=True,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(path.parent)})
        need(result.stdout.strip().decode('ascii') == expected_version, 'PLUGIN_VERSION_MISMATCH')
    except CaptureFailure: raise
    except Exception: raise CaptureFailure('PLUGIN_VERSION_UNVERIFIED') from None
    return path


class SSHChannel(AuthenticatedHostChannel):
    def __init__(self, ssm, plugin, known_hosts, identity, alias, python=sys.executable):
        need(type(ssm) is SSMChannel and os.name == 'posix', 'SSM_SSH_REQUIRED')
        self.ssm, self.plugin = ssm, private_file(plugin)
        self.known_hosts, self.identity = private_file(known_hosts), private_file(identity)
        self.alias, self.python = alias, str(Path(python).resolve())
        self.process = None
        self.mode = None
        self.buffer = bytearray()

    @contextmanager
    def bridge(self, argv, environment):
        # Plugin never inherits GitHub/AWS credentials or proxy settings.
        with tempfile.TemporaryDirectory(prefix='nc-ssm-') as directory:
            os.chmod(directory, 0o700)
            path = str(Path(directory)/'stream.sock')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path); os.chmod(path, 0o600); listener.listen(1); listener.settimeout(30)
            processes, failure = [], []
            def accept():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        process = subprocess.Popen([str(self.plugin), *argv], stdin=connection,
                            stdout=connection, stderr=subprocess.DEVNULL,
                            env={**environment, 'PATH':'/usr/bin:/bin', 'HOME':directory},
                            start_new_session=True)
                        processes.append(process)
                        process.wait()
                except Exception: failure.append(True)
            thread = threading.Thread(target=accept, daemon=True); thread.start()
            try:
                yield path
                need(not failure, 'SSM_PLUGIN_FAILED')
            finally:
                listener.close()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                thread.join(timeout=2)

    @contextmanager
    def open(self, mode):
        need(mode in ('lease', 'backup'), 'SSH_COMMAND_REJECTED')
        self.mode = mode
        with self.ssm.session(self.bridge) as socket_path:
            # Only the fixed installed root-owned dispatcher is invoked. A forced
            # command on the dedicated SSH key must enforce the same allowlist.
            proxy = shlex.join([self.python, '-m', 'scripts.backup.operational.proxy', socket_path])
            argv = ['/usr/bin/ssh', *ssh_options(str(self.known_hosts), str(self.identity), self.alias),
                    '-o', 'ProxyCommand='+proxy, 'ncbackup@'+self.ssm.target.instance,
                    'sudo', '-n', '/usr/local/libexec/nextcloud-recovery', mode]
            try:
                self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                    start_new_session=True)
                yield self
                need(self.process.poll() in (None, 0), 'SSH_CHANNEL_FAILED')
            except CaptureFailure: raise
            except BaseException: raise CaptureFailure('SSH_CHANNEL_FAILED') from None
            finally:
                if self.process:
                    if self.process.stdin: self.process.stdin.close()
                    if self.process.poll() is None:
                        self.process.terminate()
                        try: self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
                    self.process.stdout.close()
                self.process = None
                self.mode = None
                self.buffer.clear()

    def request(self, request):
        need(self.process is not None and self.process.poll() is None, 'SSH_CHANNEL_NOT_OPEN')
        raw = json.dumps(request, separators=(',', ':')).encode()+b'\n'
        need(len(raw) <= 65536, 'SSH_REQUEST_LIMIT')
        try:
            self.process.stdin.write(raw); self.process.stdin.flush()
            deadline = time.monotonic() + (2700 if self.mode == 'backup' else 30)
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while b'\n' not in self.buffer:
                    remaining = deadline - time.monotonic()
                    need(remaining > 0 and selector.select(timeout=remaining), 'SSH_RESPONSE_TIMEOUT')
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    need(chunk and len(self.buffer)+len(chunk) <= 65536, 'SSH_RESPONSE_INVALID')
                    self.buffer.extend(chunk)
            line, _, tail = self.buffer.partition(b'\n'); self.buffer = bytearray(tail)
            return parse_json(line, Limits(json_bytes=65536))
        except CaptureFailure: raise
        except Exception: raise CaptureFailure('SSH_REQUEST_OUTCOME_UNKNOWN') from None
