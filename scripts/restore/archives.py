"""Bounded TAR validation and extraction into a new private staging directory.

No overwrite, ownership changes, shell, application hooks or operational paths.
Archives retain ownership metadata for the future authorized destination adapter.
"""
import os
import hashlib
import re
import stat
from pathlib import Path, PurePosixPath
import tarfile

from scripts.backup.sealing.common import Bounds, SealFailure, require
from scripts.backup.safe_files import SafeRoot
from scripts.backup.safe_files import signature


class BoundedTarInfo(tarfile.TarInfo):
    """Cap metadata before tarfile allocates a PAX/GNU payload from the header."""
    def _proc_pax(self,archive):
        require(self.size<=65536,'RESTORE_ARCHIVE_METADATA_LIMIT')
        return super()._proc_pax(archive)

    def _proc_gnulong(self,archive):
        require(self.size<=4096,'RESTORE_ARCHIVE_METADATA_LIMIT')
        return super()._proc_gnulong(archive)

    def _proc_sparse(self,archive):
        raise SealFailure('RESTORE_ARCHIVE_TYPE_REJECTED')


def member_name(name):
    require(isinstance(name,str) and name and '\\' not in name and ':' not in name
            and not any(ord(c)<32 or ord(c)==127 for c in name),'RESTORE_ARCHIVE_PATH_REJECTED')
    if name=='.':return '.'
    if name.startswith('./'):name=name[2:]
    require(name and not name.startswith('/') and all(p not in ('','.', '..') for p in name.split('/')),
            'RESTORE_ARCHIVE_PATH_REJECTED')
    require(len(name.encode('utf-8'))<=4096,'RESTORE_ARCHIVE_PATH_REJECTED')
    if os.name=='nt':
        for part in name.split('/'):
            require(not part.endswith(('.', ' ')) and not any(c in part for c in '<>"|?*')
                    and not re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part,re.I),
                    'RESTORE_ARCHIVE_PATH_REJECTED')
    return PurePosixPath(name).as_posix()


def inspect_archive(stream,bounds=Bounds()):
    members={};size=0;metadata=0
    try:
        with tarfile.open(fileobj=stream,mode='r:',tarinfo=BoundedTarInfo) as archive:
            for member in archive:
                name=member_name(member.name)
                require(name not in members,'RESTORE_ARCHIVE_DUPLICATE')
                require(len(members)<100000,'RESTORE_ARCHIVE_ENTRY_LIMIT')
                require((member.isdir() or member.isfile() or member.issym()) and member.sparse is None,
                        'RESTORE_ARCHIVE_TYPE_REJECTED')
                metadata+=len(member.name.encode('utf-8'))+len(member.linkname.encode('utf-8'))
                metadata+=sum(len(k.encode('utf-8'))+len(v.encode('utf-8')) for k,v in member.pax_headers.items())
                require(metadata<=16*1024**2,'RESTORE_ARCHIVE_METADATA_LIMIT')
                # A directory sticky bit is ordinary backup metadata (e.g. tmp).
                # Staging always uses 0700/0600, never archived permissions.
                # setuid/setgid and sticky bits on non-directories stay forbidden.
                require(not member.mode & 0o6000
                        and (not member.mode & 0o1000 or member.isdir())
                        and member.uid>=0 and member.gid>=0,
                        'RESTORE_ARCHIVE_PERMISSIONS_REJECTED')
                require(name!='.' or member.isdir(),'RESTORE_ARCHIVE_PATH_REJECTED')
                require(type(member.size) is int and member.size>=0 and
                        (member.isfile() or member.size==0),'RESTORE_ARCHIVE_SIZE_REJECTED')
                size+=member.size;require(size<=bounds.total_artifact_bytes and member.size<=bounds.artifact_bytes,
                                          'RESTORE_ARCHIVE_SIZE_REJECTED')
                members[name]=member
    except SealFailure:raise
    except (tarfile.TarError,OSError,EOFError):raise SealFailure('RESTORE_ARCHIVE_INVALID') from None
    for name,member in members.items():
        for parent in PurePosixPath(name).parents:
            ancestor=members.get(parent.as_posix())
            require(ancestor is None or ancestor.isdir(),'RESTORE_ARCHIVE_PARENT_REJECTED')
        if member.issym():
            current=name;seen=set()
            for _ in range(16):
                require(current not in seen,'RESTORE_ARCHIVE_LINK_REJECTED');seen.add(current)
                entry=members.get(current);require(entry is not None,'RESTORE_ARCHIVE_LINK_REJECTED')
                if not entry.issym():
                    require(entry.isfile(),'RESTORE_ARCHIVE_LINK_REJECTED');break
                target=entry.linkname
                require(isinstance(target,str) and target not in ('','.', '..') and
                        not any(c in target for c in ('/','\\',':','\x00')) and
                        not any(ord(c)<32 or ord(c)==127 for c in target),'RESTORE_ARCHIVE_LINK_REJECTED')
                current=(PurePosixPath(current).parent/target).as_posix()
            else:raise SealFailure('RESTORE_ARCHIVE_LINK_REJECTED')
    return members


def extract_private(stream,destination,expected_sha256,bounds=Bounds()):
    """Caller must supply a verified regular-file stream, never a live volume."""
    destination=Path(destination)
    require(destination.is_absolute(),'RESTORE_ARCHIVE_DESTINATION_REJECTED')
    require(not destination.exists() and not destination.is_symlink(),'RESTORE_ARCHIVE_DESTINATION_NOT_NEW')
    require(isinstance(expected_sha256,str) and re.fullmatch('[a-f0-9]{64}',expected_sha256),
            'RESTORE_ARCHIVE_HASH_REQUIRED')
    info=os.fstat(stream.fileno())
    require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=bounds.artifact_bytes,
            'RESTORE_ARCHIVE_INPUT_REJECTED')
    initial=signature(stream)
    def check_bytes():
        stream.seek(0);h=hashlib.sha256();count=0
        while chunk:=stream.read(bounds.chunk_bytes):
            count+=len(chunk);require(count<=bounds.artifact_bytes,'RESTORE_ARCHIVE_SIZE_REJECTED');h.update(chunk)
        require(initial==signature(stream),'RESTORE_ARCHIVE_INPUT_CHANGED')
        require(h.hexdigest()==expected_sha256,'RESTORE_ARCHIVE_HASH_MISMATCH')
    check_bytes();stream.seek(0);members=inspect_archive(stream,bounds);stream.seek(0)
    # New private tree under a confined parent; links are created last and no
    # write subsequently traverses them. Partial output is retained on failure.
    with SafeRoot(destination.parent):destination.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=stream,mode='r:',tarinfo=BoundedTarInfo) as archive:
            for member in members.values():
                if member.issym() or member.name=='.':continue
                path=destination/member_name(member.name)
                path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                if member.isdir():path.mkdir(mode=0o700,exist_ok=True);continue
                with SafeRoot(path.parent):
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
                    with os.fdopen(fd,'wb') as output:
                        source=archive.extractfile(member);require(source is not None,'RESTORE_ARCHIVE_INVALID')
                        count=0
                        with source:
                            while chunk:=source.read(bounds.chunk_bytes):
                                count+=len(chunk);require(count<=member.size,'RESTORE_ARCHIVE_TRUNCATED');output.write(chunk)
                        require(count==member.size,'RESTORE_ARCHIVE_TRUNCATED');output.flush();os.fsync(output.fileno())
            for name,member in members.items():
                if member.issym():
                    path=destination/name
                    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    with SafeRoot(path.parent):os.symlink(member.linkname,path)
    except SealFailure:raise
    except (tarfile.TarError,OSError,EOFError):raise SealFailure('RESTORE_ARCHIVE_EXTRACTION_FAILED') from None
    check_bytes()
    return {'entries':len(members),'bytes':sum(m.size for m in members.values()),
            'ownership_applied':False,'application_started':False}
