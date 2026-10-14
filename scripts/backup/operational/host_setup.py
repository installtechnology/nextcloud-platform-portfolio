"""Offline dependency setup for an explicitly authorized host installation."""
import os
from pathlib import Path
import subprocess
import sys
from .dependencies import audit
from ..orchestration.linux.prepare_posix import verify_wheel
from .fence_policy import require


def private_environment():
    return {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PIP_CONFIG_FILE':'/dev/null','PYTHONNOUSERSITE':'1',
            'PIP_DISABLE_PIP_VERSION_CHECK':'1'}


def install_verified():
    """Invoked only by the newly prepared private venv, never by system pip."""
    require(os.name=='posix' and os.geteuid()==0 and sys.version_info[:2]==(3,12),'LINUX_ROOT_CPYTHON312_REQUIRED')
    root=Path('/usr/local/lib/nextcloud-recovery');wheels=root/'wheels'
    require(Path(sys.prefix).resolve()==root/'venv' and sys.prefix!=sys.base_prefix,'HOST_PRIVATE_VENV_REQUIRED')
    records=audit()
    require({p.name for p in wheels.iterdir()}=={r['filename'] for r in records},'HOST_WHEELHOUSE_SET_MISMATCH')
    for record in records:verify_wheel(wheels/record['filename'],record)
    env=private_environment()
    python=str(root/'venv/bin/python')
    subprocess.run([python,'-I','-m','pip','--isolated','install','--no-index','--require-hashes',
        '--only-binary=:all:','--no-deps','--no-cache-dir','--find-links',str(wheels),
        '-r',str(root/'repository/scripts/backup/operational/dependencies/requirements.lock')],
        env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run([python,'-I','-m','pip','check'],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def install():
    require(os.name=='posix' and os.geteuid()==0 and sys.version_info[:2]==(3,12),'LINUX_ROOT_CPYTHON312_REQUIRED')
    root=Path('/usr/local/lib/nextcloud-recovery');env=private_environment()
    # Ubuntu system Python need not contain pip. ensurepip is local to venv;
    # failure of python3.12-venv never falls back to apt/network/global pip.
    subprocess.run([sys.executable,'-I','-m','venv',str(root/'venv')],env=env,check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
    subprocess.run([str(root/'venv/bin/python'),'-m','scripts.backup.operational.host_setup','verified-install'],
                   cwd=root/'repository',env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)


if __name__=='__main__':
    try:
        if len(sys.argv)==1:install()
        elif sys.argv[1:]==['verified-install']:install_verified()
        else:raise RuntimeError('HOST_SETUP_ARGUMENTS_REJECTED')
        print('HOST_PINNED_DEPENDENCIES_PREPARED')
    except BaseException:raise SystemExit('HOST_OFFLINE_DEPENDENCIES_REJECTED') from None
