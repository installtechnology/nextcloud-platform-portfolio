"""Standalone read-only IMDSv2 precondition, run before Ansible changes a host."""
import json
import os
import re
import stat
import subprocess
from pathlib import Path
import sys
import urllib.request


def check_identity(config,observed):
    keys=('account','region','instance')
    if (not isinstance(config,dict) or not isinstance(observed,dict) or
        any(not isinstance(config.get(k),str) for k in (*keys,'source_instance')) or
        not re.fullmatch('[0-9]{12}',config['account']) or
        not re.fullmatch('[a-z]{2}(?:-[a-z]+)+-[0-9]+',config['region']) or
        any(not re.fullmatch('i-[a-f0-9]{17}',config[k]) for k in ('instance','source_instance')) or
        config['instance']==config['source_instance'] or observed!={k:config[k] for k in keys}):
        raise ValueError('RESTORE_PREPARE_IDENTITY_REJECTED')


def unique(pairs):
    result={}
    for k,v in pairs:
        if k in result:raise ValueError('RESTORE_PREPARE_JSON_REJECTED')
        result[k]=v
    return result


def check_empty_files(root):
    """Preparation cannot overwrite a running or previously restored installation."""
    root=Path(root)
    if root.is_symlink():raise ValueError('RESTORE_PREPARE_NONEMPTY_TARGET')
    if not root.exists():return
    if not root.is_dir():raise ValueError('RESTORE_PREPARE_NONEMPTY_TARGET')
    allowed={'docker','monitoring','data','backups'}
    for child in root.iterdir():
        if child.name not in allowed or child.is_symlink() or not child.is_dir() or any(child.iterdir()):
            raise ValueError('RESTORE_PREPARE_NONEMPTY_TARGET')


def check_empty_docker():
    socket=Path('/var/run/docker.sock')
    if not socket.exists() and not socket.is_symlink():return
    info=socket.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0:
        raise ValueError('RESTORE_PREPARE_DOCKER_REJECTED')
    # No inherited DOCKER_HOST/context, no writes or secret-bearing arguments.
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/root'}
    for args in (['ps','--all','--quiet'],['volume','ls','--quiet']):
        result=subprocess.run(['/usr/bin/docker','--host=unix:///var/run/docker.sock',*args],
                              env=env,capture_output=True,timeout=10,check=False)
        if result.returncode!=0 or result.stdout.strip():
            raise ValueError('RESTORE_PREPARE_NONEMPTY_TARGET')


def check_idle_preparation(proc=Path('/proc'), roots=None):
    """Fail closed on an interrupted preparation, lease, or application writer.

    Called before any Ansible role. No journal is edited and no process killed.
    Alternate paths are for unit tests only, never workflow/configuration inputs.
    """
    roots = roots if roots is not None else (Path('/var/lib/nextcloud-recovery'),
                                            Path('/var/lib/nextcloud-restore'))
    ignored=set();pid=os.getpid()
    for _ in range(64):
        if pid in ignored or pid<=0:break
        ignored.add(pid)
        text=(proc/str(pid)/'status').read_text()
        parent=re.search(r'^PPid:\s+(\d+)$',text,re.M)
        if parent is None:raise ValueError('RESTORE_PREPARE_PROCESS_UNVERIFIED')
        pid=int(parent.group(1))
    markers=('AnsiballZ','ansible-playbook','pg_dump','pg_restore','psql',
             'scripts.restore','scripts.backup','nextcloud-recovery/venv',
             'nextcloud-restore/venv')
    for node in proc.iterdir():
        if not node.name.isdigit() or int(node.name) in ignored:continue
        try:
            with (node/'cmdline').open('rb') as stream:raw=stream.read(65537)
        except FileNotFoundError:continue
        if len(raw)>65536:raise ValueError('RESTORE_PREPARE_PROCESS_UNVERIFIED')
        command=raw.decode('utf-8',errors='replace').replace('\0',' ')
        if any(marker in command for marker in markers):
            raise ValueError('RESTORE_PREPARE_PROCESS_BUSY')
        executable=Path(command.split(' ',1)[0]).name
        if executable in ('apt','apt-get','dpkg','tar'):
            raise ValueError('RESTORE_PREPARE_PROCESS_BUSY')
    import fcntl
    for root in roots:
        if root.is_symlink():raise ValueError('RESTORE_PREPARE_JOURNAL_UNSAFE')
        if not root.exists():continue
        info=root.stat()
        if not root.is_dir() or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('RESTORE_PREPARE_JOURNAL_UNSAFE')
        for path in root.iterdir():
            info=path.lstat()
            if (path.name!='capture.lock' or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink!=1 or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600):
                raise ValueError('RESTORE_PREPARE_JOURNAL_PRESENT')
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('RESTORE_PREPARE_LOCK_BUSY') from None
            finally:os.close(fd)
    for name in ('creation-receipt.json','initial-lease-claim.json'):
        path=Path('/etc/nextcloud-restore')/name
        if path.exists() or path.is_symlink():raise ValueError('RESTORE_PREPARE_HANDOFF_PRESENT')


def main():
    try:
        if os.name!='posix' or os.geteuid()!=0:raise ValueError('RESTORE_PREPARE_ROOT_REQUIRED')
        raw=sys.stdin.buffer.read(65537)
        if not 0<len(raw)<=65536:raise ValueError('RESTORE_PREPARE_JSON_REJECTED')
        config=json.loads(raw,object_pairs_hook=unique)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base='http://169.254.169.254/latest/'
        request=urllib.request.Request(base+'api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
        with opener.open(request,timeout=3) as r:token=r.read(4097)
        if not 0<len(token)<=4096:raise ValueError('RESTORE_PREPARE_METADATA_REJECTED')
        request=urllib.request.Request(base+'dynamic/instance-identity/document',headers={'X-aws-ec2-metadata-token':token.decode('ascii')})
        with opener.open(request,timeout=3) as r:body=r.read(16385)
        if len(body)>16384:raise ValueError('RESTORE_PREPARE_METADATA_REJECTED')
        metadata=json.loads(body,object_pairs_hook=unique)
        check_identity(config,{'account':metadata['accountId'],'region':metadata['region'],'instance':metadata['instanceId']})
        if config.get('deployment_root')!='/opt/nextcloud-platform':
            raise ValueError('RESTORE_PREPARE_PATH_REJECTED')
        check_empty_files(config['deployment_root']);check_empty_docker();check_idle_preparation()
        print('RESTORE_TARGET_IDENTITY_AND_EMPTY_HOST_VERIFIED')
    except BaseException as error:
        allowed={'RESTORE_PREPARE_IDENTITY_REJECTED','RESTORE_PREPARE_ROOT_REQUIRED','RESTORE_PREPARE_JSON_REJECTED',
                 'RESTORE_PREPARE_METADATA_REJECTED','RESTORE_PREPARE_NONEMPTY_TARGET',
                 'RESTORE_PREPARE_DOCKER_REJECTED','RESTORE_PREPARE_PATH_REJECTED',
                 'RESTORE_PREPARE_PROCESS_UNVERIFIED','RESTORE_PREPARE_PROCESS_BUSY',
                 'RESTORE_PREPARE_JOURNAL_UNSAFE','RESTORE_PREPARE_JOURNAL_PRESENT',
                 'RESTORE_PREPARE_LOCK_BUSY','RESTORE_PREPARE_HANDOFF_PRESENT'}
        code=str(error) if isinstance(error,ValueError) and str(error) in allowed else 'RESTORE_PREPARE_TARGET_REJECTED'
        raise SystemExit(code) from None


if __name__=='__main__':main()
