"""Archive the whole HTML volume without following links or copying PGDATA."""
import os
from pathlib import Path
import stat
import tarfile

from ..safe_files import SafeRoot, signature
from .storage import CaptureFailure


def archive_tree(source, output):
    source = Path(source)
    with SafeRoot(source) as safe, tarfile.open(fileobj=output, mode='w|', format=tarfile.PAX_FORMAT) as archive:
        def directory_signature(path):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CaptureFailure('LOCAL_TREE_CHANGED')
            # Directory timestamps can be deferred on Windows while a handle is
            # open; compare entry names as well to detect added/removed files.
            entries = tuple(sorted(item.name for item in path.iterdir()))
            return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, entries)
        observed_directories = {source: directory_signature(source)}
        observed_links = {}
        def link_signature(path):
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                raise CaptureFailure('LOCAL_TREE_CHANGED')
            return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, os.readlink(path))
        archive.addfile(archive.gettarinfo(str(source), arcname='.'))
        for current, directories, files in os.walk(source, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories + files:
                path = Path(current) / name
                relative = path.relative_to(source).as_posix()
                info = path.lstat()
                # Preserve same-directory file aliases, such as Nextcloud's JS
                # license links. Never follow links, traverse parents, or admit
                # directory links, dangling links or external targets. Bounded
                # same-directory chains occur in Nextcloud's license aliases.
                if stat.S_ISLNK(info.st_mode):
                    before_link = link_signature(path)
                    target = before_link[-1]
                    target_path = path
                    seen = set()
                    for _ in range(16):
                        if target_path in seen:
                            raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW')
                        seen.add(target_path)
                        current_link = link_signature(target_path)
                        if target_path in observed_links and observed_links[target_path] != current_link:
                            raise CaptureFailure('LOCAL_TREE_CHANGED')
                        observed_links[target_path] = current_link
                        name = observed_links[target_path][-1]
                        if (not name or name in ('.', '..')
                                or any(c in name for c in ('/', '\\', ':', '\x00'))):
                            raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW')
                        target_path = path.parent / name
                        try:
                            target_info = target_path.lstat()
                        except OSError:
                            raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW') from None
                        if not stat.S_ISLNK(target_info.st_mode):
                            break
                    else:
                        raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW')
                    if not stat.S_ISREG(target_info.st_mode):
                        raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW')
                    with safe.open(target_path.relative_to(source).as_posix()) as stream:
                        before_target = signature(stream)
                        member = archive.gettarinfo(str(path), arcname=relative)
                        if not member.issym() or member.linkname != target:
                            raise CaptureFailure('LOCAL_TREE_CHANGED')
                        archive.addfile(member)
                        if before_target != signature(stream) or before_link != link_signature(path):
                            raise CaptureFailure('LOCAL_TREE_CHANGED')
                    observed_links[path] = before_link
                    continue
                if getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise CaptureFailure('LOCAL_LINK_REQUIRES_REVIEW')
                if stat.S_ISDIR(info.st_mode):
                    observed_directories[path] = directory_signature(path)
                    archive.addfile(archive.gettarinfo(str(path), arcname=relative))
                elif stat.S_ISREG(info.st_mode):
                    with safe.open(relative) as stream:
                        before = signature(stream)
                        member = archive.gettarinfo(str(path), arcname=relative)
                        if member.size != before[2]:
                            raise CaptureFailure('LOCAL_FILE_CHANGED')
                        archive.addfile(member, stream)
                        if before != signature(stream):
                            raise CaptureFailure('LOCAL_FILE_CHANGED')
                else:
                    raise CaptureFailure('LOCAL_SPECIAL_FILE_REQUIRES_REVIEW')
        safe.assert_unchanged()
        for path, before in observed_links.items():
            if link_signature(path) != before:
                raise CaptureFailure('LOCAL_TREE_CHANGED')
        for path, before in observed_directories.items():
            if directory_signature(path) != before:
                raise CaptureFailure('LOCAL_TREE_CHANGED')
