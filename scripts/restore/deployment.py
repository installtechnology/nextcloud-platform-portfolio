"""Separate deploy preparation: no application startup, restore or network on import."""
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import hashlib
import stat
import tarfile

from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import require
from scripts.restore.host_scope import validate


def validate_preparation(raw, account, region, project, buckets, instance, root):
    require(isinstance(raw, str), 'RESTORE_PREPARATION_INVALID')
    config = validate(parse_json(raw.encode('utf-8'), Limits(json_bytes=65536))).config
    require((config['account'], config['region'], config['project'], config['instance']) ==
            (account, region, project, instance), 'RESTORE_PREPARATION_SCOPE_MISMATCH')
    require([config[k] for k in ('data_bucket', 'backups_bucket', 'state_bucket')] == buckets,
            'RESTORE_PREPARATION_BUCKET_MISMATCH')
    # Pins must equal this reviewed repository, never caller-supplied alternatives.
    compose = (Path(root)/'docker/compose.yml').read_text(encoding='utf-8')
    for service, image in config['images'].items():
        match = re.search(r'^  '+re.escape(service)+r':\s*\n(?:.*\n)*?    image: ([^\s]+)', compose, re.M)
        require(match is not None and match.group(1) == image, 'RESTORE_PREPARATION_IMAGE_MISMATCH')
    return config


def export_reviewed(root, destination, commit):
    """Only committed blobs from approved tooling trees; no checkout private files."""
    require(isinstance(commit, str) and re.fullmatch('[a-f0-9]{40}', commit), 'RESTORE_CODE_REVISION_INVALID')
    def git(*args):
        result = subprocess.run(['git', *args], cwd=root, capture_output=True, check=False)
        require(result.returncode == 0, 'RESTORE_CODE_EXPORT_FAILED')
        return result.stdout
    require(git('rev-parse', 'HEAD').decode().strip() == commit, 'RESTORE_CODE_REVISION_MISMATCH')
    listing = git('ls-tree', '-rz', commit, '--', 'scripts/restore', 'scripts/backup', 'contracts/recovery', 'docker')
    require(len(listing) <= 2*1024**2, 'RESTORE_CODE_EXPORT_LIMIT')
    entries = []; total = 0
    for record in listing.split(b'\0'):
        if not record: continue
        try:
            head, raw_path = record.split(b'\t', 1)
            mode, kind, digest = head.decode('ascii').split()
            name = raw_path.decode('utf-8'); path = PurePosixPath(name)
        except (ValueError, UnicodeError):
            raise ValueError('RESTORE_CODE_EXPORT_INVALID') from None
        require(mode in ('100644', '100755') and kind == 'blob' and
                re.fullmatch('[a-f0-9]{40,64}', digest) and not path.is_absolute() and
                all(p not in ('', '.', '..') for p in name.split('/')) and '\\' not in name and ':' not in name and
                any(name.startswith(prefix+'/') for prefix in ('scripts/restore', 'scripts/backup', 'contracts/recovery', 'docker')),
                'RESTORE_CODE_EXPORT_INVALID')
        size = int(git('cat-file', '-s', digest)); total += size
        require(0 <= size <= 4*1024**2 and total <= 64*1024**2 and len(entries) < 1000,
                'RESTORE_CODE_EXPORT_LIMIT')
        entries.append((name, digest, size))
    require(all(any(n.startswith(prefix+'/') for n, _, _ in entries)
                for prefix in ('scripts/restore', 'scripts/backup', 'contracts/recovery', 'docker')),
            'RESTORE_REVIEWED_CODE_INCOMPLETE')
    destination = Path(destination); destination.mkdir(mode=0o700)
    for name, digest, size in entries:
        data = git('cat-file', 'blob', digest)
        require(len(data) == size, 'RESTORE_CODE_EXPORT_CHANGED')
        target = destination/name; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
    return str(destination)


def package_reviewed(exported, archive):
    """Transfer the private Git export once, without links or extra roots."""
    exported = Path(exported); archive = Path(archive)
    require(exported.is_dir() and not exported.is_symlink(), 'RESTORE_ARCHIVE_SOURCE_INVALID')
    entries = sorted(exported.rglob('*')); total = 0
    require(len(entries) <= 5000, 'RESTORE_ARCHIVE_LIMIT')
    for path in entries:
        mode = path.lstat().st_mode
        name = path.relative_to(exported).as_posix()
        require(not path.is_symlink() and (stat.S_ISDIR(mode) or stat.S_ISREG(mode))
                and name.split('/')[0] in ('scripts', 'contracts', 'docker')
                and all(part not in ('', '.', '..') and ':' not in part and '\\' not in part
                        for part in name.split('/')), 'RESTORE_ARCHIVE_ENTRY_INVALID')
        if stat.S_ISREG(mode):
            total += path.stat().st_size
            require(total <= 64*1024**2, 'RESTORE_ARCHIVE_LIMIT')
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w') as bundle:
        for path in entries:
            info = bundle.gettarinfo(str(path), arcname=path.relative_to(exported).as_posix())
            require(info.isdir() or info.isfile(), 'RESTORE_ARCHIVE_ENTRY_INVALID')
            info.uid = info.gid = 0; info.uname = info.gname = ''
            info.mode = 0o700 if info.isdir() else 0o600
            if info.isfile():
                with path.open('rb') as source: bundle.addfile(info, source)
            else: bundle.addfile(info)
    digest = hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
    return str(archive), digest.hexdigest()


def preparation_variables(config, root, directory, wheels, commit):
    from scripts.backup.operational.dependencies import audit
    from scripts.backup.orchestration.linux.prepare_posix import verify_wheel
    wheels = Path(wheels); records = audit()
    require(wheels.is_dir() and not wheels.is_symlink() and
            {p.name for p in wheels.iterdir()} == {r['filename'] for r in records},
            'RESTORE_APPROVED_WHEELHOUSE_REQUIRED')
    for record in records: verify_wheel(wheels/record['filename'], record)
    exported = export_reviewed(root, Path(directory)/'restore-reviewed-export', commit)
    archive, checksum = package_reviewed(exported, Path(directory)/'restore-reviewed-export.tar')
    return {'restore_install_confirmation': 'PREPARE_RESTORE_TARGET',
            'restore_host_configuration': config,
            'restore_reviewed_code_export': exported,
            'restore_reviewed_code_archive': archive, 'restore_reviewed_code_sha256': checksum,
            'restore_reviewed_wheelhouse': str(wheels), 'restore_reviewed_commit': commit}
