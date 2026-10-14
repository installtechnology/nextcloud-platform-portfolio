"""Dedicated restore SSH command through existing verified SSM primitives."""
from contextlib import contextmanager
import os
import selectors
import shlex
import subprocess
import time
from scripts.backup.operational.ssh_channel import SSHChannel
from scripts.backup.operational.ssm import ssh_options,need
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import json_bytes


class RestoreSSHChannel(SSHChannel):
    @contextmanager
    def open(self):
        need(self.ssm.target.role_name.endswith('-restore'),'RESTORE_DEDICATED_ROLE_REQUIRED')
        with self.ssm.session(self.bridge) as socket_path:
            proxy=shlex.join([self.python,'-m','scripts.backup.operational.proxy',socket_path])
            argv=['/usr/bin/ssh',*ssh_options(str(self.known_hosts),str(self.identity),self.alias),
                  '-o','ProxyCommand='+proxy,'ncrestore@'+self.ssm.target.instance,
                  'sudo','-n','/usr/local/libexec/nextcloud-restore','restore']
            try:
                self.process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
                yield self
                need(self.process.poll() in (None,0),'RESTORE_SSH_CHANNEL_FAILED')
            finally:
                if self.process:
                    if self.process.stdin:self.process.stdin.close()
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
                    self.process.stdout.close()
                self.process=None;self.buffer.clear()

    def request(self,request):
        need(self.process is not None and self.process.poll() is None,'RESTORE_SSH_NOT_OPEN')
        raw=json_bytes(request)+b'\n';need(len(raw)<=65536,'RESTORE_REQUEST_LIMIT')
        try:
            self.process.stdin.write(raw);self.process.stdin.flush();deadline=time.monotonic()+2700
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout,selectors.EVENT_READ)
                while b'\n' not in self.buffer:
                    remaining=deadline-time.monotonic()
                    need(remaining>0 and selector.select(timeout=remaining),'RESTORE_SSH_TIMEOUT')
                    chunk=os.read(self.process.stdout.fileno(),16384)
                    need(chunk and len(self.buffer)+len(chunk)<=16384,'RESTORE_SSH_RESPONSE_REJECTED')
                    self.buffer.extend(chunk)
            line,_,tail=self.buffer.partition(b'\n');self.buffer=bytearray(tail)
            return parse_json(line,Limits(json_bytes=16384))
        except CaptureFailure:raise
        except BaseException:raise CaptureFailure('RESTORE_SSH_OUTCOME_UNKNOWN') from None
