"""Reconstruct verified backup files in a NEW private tree, never a live volume."""
import hashlib
import os
from pathlib import Path
import stat

from scripts.backup.safe_files import SafeRoot, signature
from scripts.backup.capture.storage import sync_directory
from scripts.backup.sealing.common import require
from scripts.restore.archives import extract_private, inspect_archive
from scripts.restore.parameters import load_parameters, RUNTIME, SECRETS


def verified_file(directory, name, artifact, consumer):
    with SafeRoot(directory) as safe, safe.open(name) as stream:
        before=signature(stream);h=hashlib.sha256();count=0
        while chunk:=stream.read(1024**2):
            count+=len(chunk);require(count<=artifact['size_bytes'],'RESTORE_LOCAL_SIZE_MISMATCH');h.update(chunk)
        require(before==signature(stream) and count==artifact['size_bytes'] and h.hexdigest()==artifact['sha256'],
                'RESTORE_LOCAL_ARTIFACT_CHANGED')
        stream.seek(0);result=consumer(stream)
        require(before==signature(stream),'RESTORE_LOCAL_ARTIFACT_CHANGED');safe.assert_unchanged()
        return result


def tree_digest(root):
    """Include names/types/link targets/bytes, without following links or logging them."""
    root=Path(root);records=[]
    with SafeRoot(root) as safe:
        for current, dirs, files in os.walk(root,followlinks=False):
            dirs.sort();files.sort()
            for name in dirs+files:
                path=Path(current)/name;relative=path.relative_to(root).as_posix();info=path.lstat()
                if stat.S_ISLNK(info.st_mode):kind='link';value=os.readlink(path)
                elif stat.S_ISDIR(info.st_mode):kind='directory';value=''
                elif stat.S_ISREG(info.st_mode):
                    require(info.st_nlink==1,'RESTORE_CONFIGURATION_UNSAFE')
                    with safe.open(relative) as stream:
                        before=signature(stream);h=hashlib.sha256()
                        while chunk:=stream.read(1024**2):h.update(chunk)
                        require(before==signature(stream),'RESTORE_CONFIGURATION_CHANGED');value=h.hexdigest()
                    kind='file'
                else:require(False,'RESTORE_CONFIGURATION_UNSAFE')
                after=path.lstat()
                fields=('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
                require(tuple(getattr(info,k) for k in fields)==tuple(getattr(after,k) for k in fields),
                        'RESTORE_CONFIGURATION_CHANGED')
                records.append((relative,kind,value))
        safe.assert_unchanged()
    require(len(records)<=100000,'RESTORE_CONFIGURATION_LIMIT')
    import json
    return hashlib.sha256(json.dumps(sorted(records),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def write_new(path,data,mode=0o600):
    require(isinstance(data,bytes) and 0<len(data)<=4*1024**2,'RESTORE_PRIVATE_FILE_INVALID')
    path=Path(path)
    with SafeRoot(path.parent):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),mode)
        if os.name=='posix':os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        sync_directory(path.parent)


def stage_private(directory,manifest,reviewed_runtime):
    """Artifact hashes rechecked at use; backup code must equal reviewed code."""
    directory=Path(directory);artifacts=manifest['artifacts']
    require(set(reviewed_runtime)==RUNTIME,'RESTORE_REVIEWED_RUNTIME_INCOMPLETE')
    params_artifact=artifacts['recovery_parameters']
    def parameters(stream):
        require(params_artifact['size_bytes']<=8*1024**2,'RESTORE_PARAMETERS_LIMIT')
        return load_parameters(stream.read(8*1024**2+1),manifest)
    params=verified_file(directory,params_artifact['key'].rsplit('/',1)[1],params_artifact,parameters)
    require(params.runtime_code==reviewed_runtime,'RESTORE_RUNTIME_REVIEW_REQUIRED')
    result={}
    for role,folder in (('configuration','configuration'),('local_files','html')):
        artifact=artifacts[role];name=artifact['key'].rsplit('/',1)[1]
        result[role]=verified_file(directory,name,artifact,
            lambda stream:extract_private(stream,directory/folder,artifact['sha256']))
    require((directory/'html/config').is_dir() and not (directory/'html/config').is_symlink(),
            'RESTORE_CONFIGURATION_MISSING')
    require(tree_digest(directory/'configuration')==tree_digest(directory/'html/config'),
            'RESTORE_CONFIGURATION_ARCHIVES_MISMATCH')
    # Only a new staging directory. Nothing is copied into Docker volumes here.
    runtime=directory/'runtime';runtime.mkdir(mode=0o700)
    secrets=runtime/'secrets';secrets.mkdir(mode=0o700)
    for name in sorted(RUNTIME):
        mode=0o700 if name.endswith('.sh') else 0o600
        data=params.runtime_code[name]
        if name.endswith('.sh'):require(data.startswith(b'#!/') and b'\r' not in data.split(b'\n',1)[0],
                                       'RESTORE_RUNTIME_SHEBANG_INVALID')
        write_new(runtime/name,data,mode)
    for name in sorted(SECRETS):write_new(secrets/name,params.secrets[name])
    # Keep the original env private as evidence; the destination env is rebuilt separately.
    write_new(runtime/'.env.source',params.compose_env)
    return params, {'configuration_digest':tree_digest(directory/'configuration'),
                    'runtime_reviewed':True,'private_files_staged':True,
                    'restore_proven':False,'destroy_authorized':False}
