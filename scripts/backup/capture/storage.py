"""Private, durable local staging and exclusion lock; production writes are POSIX-only."""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import stat

from ..json_input import parse_json
from ..limits import Limits
from ..safe_files import SafeRoot, relative_parts, signature


class CaptureFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path, value):
    # Refuse symlink/junction parents even for a syntactically valid relative path.
    with SafeRoot(path.parent):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CaptureFailure('UNSAFE_JOURNAL_PATH')
    temporary = path.with_name(path.name + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    except BaseException:
        # Pending file stays for diagnosis; never delete capture artifacts automatically.
        raise


class Store:
    def __init__(self, root, synthetic=False):
        self.root = Path(os.path.abspath(root))
        if not synthetic and (os.name != 'posix' or os.geteuid() != 0):
            raise CaptureFailure('LINUX_ROOT_REQUIRED')
        with SafeRoot(self.root):
            info = self.root.stat()
            if os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise CaptureFailure('PRIVATE_STAGING_REQUIRED')
        self.synthetic = synthetic

    @contextmanager
    def lock(self):
        path = self.root / 'capture.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CaptureFailure('UNSAFE_LOCK')
            if os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise CaptureFailure('UNSAFE_LOCK_PERMISSIONS')
            try:
                if os.name == 'nt':
                    import msvcrt
                    if info.st_size == 0:
                        os.write(fd, b'0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CaptureFailure('CAPTURE_BUSY') from None
            yield
        finally:
            os.close(fd)

    def read(self, relative):
        with SafeRoot(self.root) as safe:
            with safe.open(relative) as stream:
                data = stream.read(1024 * 1024 + 1)
        parts = relative_parts(relative)
        if len(parts) != 2 or parts[-1] != 'postgres-fence.json':
            return parse_json(data, Limits(json_bytes=1024 * 1024))
        self.operation(parts[0])
        # Only this documented internal field represents a <=64 KiB HBA file.
        # All other strings/documents keep the original defensive bounds.
        encoded_limit = ((65536 + 2) // 3) * 4
        value = parse_json(data, Limits(json_bytes=1024 * 1024, string_chars=encoded_limit))
        if (not isinstance(value, dict) or value.get('kind') != 'postgres-auth-fence'
                or value.get('operation_id') != parts[0]
                or not isinstance(value.get('before_base64'), str)):
            raise CaptureFailure('POSTGRES_FENCE_JOURNAL_INVALID')
        try:
            original = base64.b64decode(value['before_base64'], validate=True)
        except (ValueError, TypeError):
            raise CaptureFailure('POSTGRES_FENCE_JOURNAL_INVALID') from None
        if not 0 < len(original) <= 65536 or hashlib.sha256(original).hexdigest() != value.get('before_sha256'):
            raise CaptureFailure('POSTGRES_FENCE_JOURNAL_INVALID')
        remainder = dict(value, before_base64='')
        parse_json(json.dumps(remainder).encode(), Limits(json_bytes=1024 * 1024))
        return value

    def write(self, relative, value):
        relative_parts(relative)
        atomic_json(self.root / relative, value)

    def operation(self, operation_id):
        import re
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', operation_id):
            raise CaptureFailure('INVALID_OPERATION_ID')
        return self.root / operation_id

    def artifact(self, directory, name, producer, limit):
        if len(relative_parts(name)) != 1 or directory.parent != self.root or directory.is_symlink():
            raise CaptureFailure('UNSAFE_ARTIFACT_PATH')
        path = directory / (name + '.partial')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            class Bounded:
                def __init__(self):
                    self.size = 0
                def write(self, value):
                    if self.size + len(value) > limit:
                        raise CaptureFailure('ARTIFACT_LIMIT')
                    written = stream.write(value)
                    self.size += written
                    return written
            producer(Bounded())
            stream.flush()
            os.fsync(stream.fileno())
            produced_signature = signature(stream)
        size = produced_signature[2]
        if not 0 < size <= limit:
            raise CaptureFailure('ARTIFACT_SIZE_INVALID')
        digest = hashlib.sha256()
        with SafeRoot(directory) as safe:
            with safe.open(path.name) as stream:
                before = signature(stream)
                if before != produced_signature:
                    raise CaptureFailure('ARTIFACT_CHANGED')
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if before != signature(stream):
                    raise CaptureFailure('ARTIFACT_CHANGED')
            safe.assert_unchanged()
        final = directory / name
        if final.exists():
            raise CaptureFailure('ARTIFACT_ALREADY_EXISTS')
        os.rename(path, final)
        sync_directory(directory)
        return {'size_bytes': size, 'plaintext_sha256': digest.hexdigest()}

    def reserve(self, directory):
        fd = os.open(directory / 'journal.reserve', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(bytes(1024 * 1024))
            stream.flush()
            os.fsync(stream.fileno())

    def release_reserve(self, directory):
        # Only our non-data reservation is truncated, never a dump/config/volume.
        with SafeRoot(directory) as safe:
            with safe.open('journal.reserve'):
                pass
        fd = os.open(directory / 'journal.reserve', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'r+b') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CaptureFailure('UNSAFE_RESERVE')
            stream.truncate(0)
            stream.flush()
            os.fsync(stream.fileno())
