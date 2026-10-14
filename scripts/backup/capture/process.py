"""Linux local subprocess transport: bounded streams, timeout and no raw diagnostics."""
import io
import os
import selectors
import signal
import subprocess
import tempfile
import time

from .storage import CaptureFailure


class Runner:
    def stream(self, argv, output, timeout=300, input_bytes=None, input_file=None):
        if os.name != 'posix':
            raise CaptureFailure('LINUX_REQUIRED')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('AWS_', 'NC_', 'NEXTCLOUD_', 'POSTGRES_', 'REDIS_', 'DOCKER_'))}
        env.update(AWS_EC2_METADATA_DISABLED='true', DOCKER_HOST='unix:///var/run/docker.sock')
        with tempfile.TemporaryFile(mode='w+b') as source:
            if input_bytes is not None:
                if len(input_bytes) > 1024 * 1024:
                    raise CaptureFailure('INPUT_LIMIT')
                source.write(input_bytes)
                source.seek(0)
            process = subprocess.Popen(argv, stdin=input_file if input_file is not None else source, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise CaptureFailure('COMMAND_TIMEOUT')
                        for key, _ in selector.select(min(remaining, 0.2)):
                            data = os.read(key.fd, 1024 * 1024)
                            if not data:
                                selector.unregister(key.fileobj)
                            else:
                                output.write(data)
                if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                    raise CaptureFailure('LOCAL_COMMAND_FAILED')
            except subprocess.TimeoutExpired:
                raise CaptureFailure('COMMAND_TIMEOUT') from None
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                process.stdout.close()

    def run(self, argv, timeout=60, input_bytes=None):
        class Limited(io.BytesIO):
            def write(self, value):
                if self.tell() + len(value) > 1024 * 1024:
                    raise CaptureFailure('COMMAND_OUTPUT_LIMIT')
                return super().write(value)
        output = Limited()
        self.stream(argv, output, timeout, input_bytes)
        return output.getvalue()
