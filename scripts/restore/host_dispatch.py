"""Fixed root dispatcher: one restore worker, systemd owns its process group."""
import os
import subprocess
import sys
import uuid
from scripts.backup.sealing.common import require
from scripts.restore.host_scope import load
from scripts.restore.access import load_access


def dispatch(mode):
    require(os.name=='posix' and os.geteuid()==0,'RESTORE_LINUX_ROOT_REQUIRED')
    require(mode=='restore','RESTORE_HOST_COMMAND_REJECTED')
    host=load();load_access(host.config)
    command=['/usr/bin/systemd-run','--quiet','--wait','--pipe','--collect',
        '--unit=nextcloud-restore-'+uuid.uuid4().hex,'--property=Type=exec',
        '--property=KillMode=control-group','--property=RuntimeMaxSec=2700',
        '--property=TimeoutStopSec=30','--property=UMask=0077',
        '--property=LimitCORE=0',
        '--property=WorkingDirectory=/usr/local/lib/nextcloud-recovery/repository',
        '/usr/local/lib/nextcloud-recovery/venv/bin/python','-m','scripts.restore.host_worker']
    result=subprocess.run(command,stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=subprocess.DEVNULL,
                          env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONNOUSERSITE':'1'})
    require(result.returncode==0,'RESTORE_SUPERVISOR_INTERVENTION_REQUIRED')


def main():
    try:
        require(len(sys.argv)==2,'RESTORE_HOST_COMMAND_REJECTED');dispatch(sys.argv[1])
    except BaseException:raise SystemExit(2) from None


if __name__=='__main__':main()
