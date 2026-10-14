"""Pinned official age binary. Encrypt-only adapter; no private identity support."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time

from ..safe_files import SafeRoot, signature, relative_parts
from ..capture.storage import sync_directory
from .common import Bounds, Budget, SealFailure, require


class Age:
    def __init__(self, executable, executable_sha256, bounds=Bounds()):
        self.path = Path(os.path.abspath(executable))
        self.expected_hash, self.bounds = executable_sha256, bounds
        require(re.fullmatch(r'[a-f0-9]{64}', executable_sha256 or ''), 'AGE_PROVENANCE_REQUIRED')
        self._verify_binary()
        try:
            result = subprocess.run([str(self.path), '--version'], capture_output=True, timeout=10,
                                    env=self.environment(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise SealFailure('AGE_UNAVAILABLE') from None
        require(result.returncode == 0 and result.stdout.strip() == b'v1.3.2', 'AGE_VERSION_MISMATCH')

    @staticmethod
    def environment():
        # No SSH agent, passphrase, plugin, identity or AWS discovery inherited.
        return {k: v for k, v in os.environ.items()
                if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL')}

    def _verify_binary(self):
        digest = hashlib.sha256()
        with SafeRoot(self.path.parent) as safe:
            with safe.open(self.path.name) as stream:
                before = signature(stream)
                require(0 < before[2] <= 64 * 1024 ** 2, 'AGE_BINARY_INVALID')
                while chunk := stream.read(1024 ** 2):
                    digest.update(chunk)
                require(before == signature(stream), 'AGE_BINARY_CHANGED')
            safe.assert_unchanged()
        require(digest.hexdigest() == self.expected_hash, 'AGE_PROVENANCE_MISMATCH')

    def encrypt(self, root, source, target, recipient, cancelled=lambda: False, plaintext_sha256=None):
        require(isinstance(recipient, str) and re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}', recipient),
                'INVALID_PUBLIC_RECIPIENT')
        require(len(relative_parts(target)) == 1, 'UNSAFE_CIPHERTEXT_PATH')
        self._verify_binary()
        root = Path(root)
        budget = Budget(self.bounds, cancelled)
        with SafeRoot(root) as safe, safe.open(source) as plain:
            before = signature(plain)
            require(0 < before[2] <= self.bounds.artifact_bytes, 'PLAINTEXT_LIMIT')
            if plaintext_sha256 is not None:
                digest = hashlib.sha256()
                while chunk := plain.read(self.bounds.chunk_bytes):
                    budget.check()
                    digest.update(chunk)
                require(digest.hexdigest() == plaintext_sha256 and before == signature(plain), 'PLAINTEXT_CHANGED')
                plain.seek(0)
            # EXCL refuses existing files, links and historical local partials.
            target_path = root / target
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                fd = (os.open(target, flags, 0o600, dir_fd=safe.fd) if os.name == 'posix'
                      else os.open(target_path, flags, 0o600))
            except FileExistsError:
                raise SealFailure('CIPHERTEXT_ALREADY_EXISTS') from None
            except OSError:
                raise SealFailure('CIPHERTEXT_IO_ERROR') from None
            with os.fdopen(fd, 'wb') as output:
                process = None
                try:
                    budget.check()
                    process = subprocess.Popen([str(self.path), '--encrypt', '--recipient', recipient],
                                               stdin=plain, stdout=output, stderr=subprocess.DEVNULL,
                                               env=self.environment())
                    while process.poll() is None:
                        budget.check()
                        require(os.fstat(output.fileno()).st_size <= self.bounds.artifact_bytes + 1024 ** 2,
                                'CIPHERTEXT_LIMIT')
                        time.sleep(0.02)
                    require(process.returncode == 0, 'ENCRYPTION_FAILED')
                    require(before == signature(plain), 'PLAINTEXT_CHANGED')
                    safe.assert_unchanged()
                    output.flush()
                    os.fsync(output.fileno())
                except SealFailure:
                    raise
                except Exception:
                    raise SealFailure('ENCRYPTION_FAILED') from None
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
            sync_directory(root)
            with safe.open(target) as cipher:
                info = signature(cipher)
                require(0 < info[2] <= self.bounds.artifact_bytes, 'CIPHERTEXT_LIMIT')
                require(cipher.read(22) == b'age-encryption.org/v1\n', 'INVALID_AGE_ENVELOPE')
                # Bounded framing check only. MAC/AEAD authentication requires decryption
                # and is tested separately with disposable identities, never in product code.
                header_bytes, x25519 = 22, False
                while True:
                    line = cipher.readline(1025)
                    header_bytes += len(line)
                    require(line.endswith(b'\n') and len(line) <= 1024 and header_bytes <= 16384,
                            'INVALID_AGE_ENVELOPE')
                    x25519 |= line.startswith(b'-> X25519 ')
                    if line.startswith(b'--- '):
                        require(re.fullmatch(rb'--- [A-Za-z0-9+/]{43}\n', line) and x25519
                                and info[2] - header_bytes >= 32, 'INVALID_AGE_ENVELOPE')
                        break
                cipher.seek(0)
                digest = hashlib.sha256()
                while chunk := cipher.read(self.bounds.chunk_bytes):
                    budget.check()
                    digest.update(chunk)
                require(info == signature(cipher), 'CIPHERTEXT_CHANGED')
            safe.assert_unchanged()
        return {'size_bytes': info[2], 'ciphertext_sha256': digest.hexdigest()}
