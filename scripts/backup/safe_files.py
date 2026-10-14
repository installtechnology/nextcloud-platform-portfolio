"""Read-only confined file access. Reject all links, reparse points and hardlinks."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from .errors import ValidationFailure


def relative_parts(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValidationFailure('UNSAFE_PATH')
    if (any(ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value or ':' in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValidationFailure('UNSAFE_PATH')
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{n}' for p in ('COM', 'LPT') for n in range(1, 10)}
    if any(not p or p in ('.', '..') or p.endswith(('.', ' '))
           or p.split('.')[0].upper() in reserved or any(c in p for c in '*?<>|"') for p in parts):
        raise ValidationFailure('UNSAFE_PATH')
    return parts


def _is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _windows_open(path, directory=False):
    # OPEN_REPARSE_POINT prevents following the final link; final-path comparison
    # catches an intermediate junction swap before any content is read.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
    handle = create(str(path), 0x80000000, 1, None, 3, flags, None)  # READ, share READ only, OPEN_EXISTING
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), 'FILE_OPEN_FAILED')
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    final_name = api.GetFinalPathNameByHandleW
    final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_name.restype = wintypes.DWORD
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_name(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ValidationFailure('UNSAFE_PATH')
        final = buffer.value
        if final.startswith('\\\\?\\UNC\\'):
            raise ValidationFailure('UNSAFE_PATH')
        if final.startswith('\\\\?\\'):
            final = final[4:]
        if os.path.normcase(os.path.normpath(final)) != os.path.normcase(str(path)):
            raise ValidationFailure('UNSAFE_PATH')
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # fd owns the handle now.
        return fd
    finally:
        if handle is not None:
            close(handle)


class SafeRoot:
    def __init__(self, root):
        self.path = Path(os.path.abspath(root))
        self.fd = None
        self.observed = {}
        if str(self.path).startswith(('\\\\', '//')):
            raise ValidationFailure('UNSAFE_ROOT')
        try:
            # Every ancestor is checked, not only the final pathname.
            for p in reversed((self.path, *self.path.parents)):
                info = p.lstat()
                if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValidationFailure('UNSAFE_ROOT')
            if os.name == 'nt':
                import ctypes
                if ctypes.windll.kernel32.GetDriveTypeW(self.path.anchor) == 4:
                    raise ValidationFailure('UNSAFE_ROOT')
                self.fd = _windows_open(self.path, directory=True)
            elif os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
                fd = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    for part in self.path.parts[1:]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        os.close(fd)
                        fd = child
                    self.fd = fd
                except BaseException:
                    os.close(fd)
                    raise
            else:
                raise ValidationFailure('SAFE_IO_UNSUPPORTED', 5)
        except OSError:
            raise ValidationFailure('ROOT_UNAVAILABLE') from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def open(self, relative):
        parts = relative_parts(relative)
        fd = None
        directories = []
        try:
            if os.name == 'nt':
                path = self.path
                for part in parts:
                    path = path / part
                    info = path.lstat()
                    if _is_reparse(info):
                        raise ValidationFailure('UNSAFE_PATH')
                fd = _windows_open(path)
            else:
                parent = self.fd
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    directories.append(child)
                    parent = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(fd)
            if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationFailure('UNSAFE_FILE')
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                current = signature(stream)
                if relative in self.observed and self.observed[relative] != current:
                    raise ValidationFailure('FILE_CHANGED')
                self.observed[relative] = current
                yield stream
        except FileNotFoundError:
            raise ValidationFailure('FILE_MISSING') from None
        except OSError as error:
            code = 'UNSAFE_PATH' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'FILE_UNREADABLE'
            # Windows FileNotFoundError is populated through winerror by ctypes callers.
            if os.name == 'nt' and error.errno in (2, 3):
                code = 'FILE_MISSING'
            raise ValidationFailure(code) from None
        finally:
            if fd is not None:
                os.close(fd)
            for directory in reversed(directories):
                os.close(directory)

    def assert_unchanged(self):
        for path in tuple(self.observed):
            with self.open(path) as stream:
                if signature(stream) != self.observed[path]:
                    raise ValidationFailure('FILE_CHANGED')


def signature(stream):
    info = os.fstat(stream.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
