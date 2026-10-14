"""One root-receipted handoff on an empty, independently verified new host.

Administrative SSH only during provisioning. Not a backup/restore dispatcher,
not installable/activatable by workflow inputs alone and never an unlock helper.
"""
import os
from pathlib import Path
import re
import stat
import sys
from contextlib import contextmanager
import signal
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.safe_files import SafeRoot
from scripts.backup.sealing.common import require,json_bytes
from scripts.backup.capture.storage import Store
from scripts.restore.private_files import write_new
from scripts.restore.host_scope import load


def receipt(host):
    root=Path('/etc/nextcloud-restore')
    with SafeRoot(root) as safe,safe.open('creation-receipt.json') as f:
        info=os.fstat(f.fileno())
        require(info.st_uid==info.st_gid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,
                'CREATION_RECEIPT_UNSAFE')
        d=parse_json(f.read(16385),Limits(json_bytes=16384))
    require(isinstance(d,dict) and set(d)=={'version','owner','commit','configuration_sha256','host'}
            and type(d['version']) is int and d['version']==1
            and isinstance(d['owner'],str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',d['owner'])
            and re.fullmatch('[a-f0-9]{40}',d['commit'])
            and re.fullmatch('[a-f0-9]{64}',d['configuration_sha256'])
            and d['host']==host.identity(),'CREATION_RECEIPT_REJECTED')
    with SafeRoot(root) as safe,safe.open('reviewed-commit') as f:
        require(f.read(42).decode().strip()==d['commit'],'CREATION_RECEIPT_REVISION_MISMATCH')
    return d


def serve(control,d,incoming,outgoing):
    def read():
        raw=incoming.readline(16385)
        require(raw.endswith(b'\n') and len(raw)<=16384,'CREATION_HOST_PROTOCOL_REJECTED')
        r=parse_json(raw,Limits(json_bytes=16384))
        require(isinstance(r,dict) and set(r)=={'version','command','owner','nonce','account','region','instance'}
                and type(r['version']) is int and r['version']==1 and r['owner']==d['owner']
                and {k:r[k] for k in ('account','region','instance')}==d['host']
                and isinstance(r['nonce'],str) and re.fullmatch('[a-f0-9]{64}',r['nonce']),
                'CREATION_HOST_PROTOCOL_REJECTED')
        return r
    first=read();require(first['command']=='acquire','CREATION_HOST_PROTOCOL_REJECTED')
    def reply(r,phase):outgoing.write(json_bytes({**r,'phase':phase})+b'\n');outgoing.flush()
    with control.hold(first['owner']):
        control.assert_held()
        write_new(Path('/etc/nextcloud-restore/initial-lease-claim.json'),json_bytes(d))
        reply(first,'held')
        while True:
            r=read();require({k:v for k,v in r.items() if k!='command'}==
                            {k:v for k,v in first.items() if k!='command'},'CREATION_HOST_PROTOCOL_REJECTED')
            control.assert_held();require(r['command'] in ('assert','release'),'CREATION_HOST_PROTOCOL_REJECTED')
            if r['command']=='release':break
            reply(r,'held')
    reply(r,'released')


@contextmanager
def bounded_session():
    # Process lifetime is bounded even if the SSH peer remains connected and
    # sends nothing. On SIGTERM/timeout the durable lease becomes interrupted;
    # SIGKILL leaves it held. Neither event authorizes automatic release.
    from scripts.backup.capture.storage import CaptureFailure
    previous={}
    require(signal.getitimer(signal.ITIMER_REAL)==(0.0,0.0),'CREATION_TIMER_ALREADY_ACTIVE')
    def cancel(signum,frame):raise CaptureFailure('CREATION_HOST_SESSION_INTERRUPTED')
    try:
        for s in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):previous[s]=signal.signal(s,cancel)
        signal.setitimer(signal.ITIMER_REAL,900)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        for s,handler in previous.items():signal.signal(s,handler)


def main():
    try:
        require(os.name=='posix' and os.geteuid()==0 and len(sys.argv)==1,'CREATION_HOST_ROOT_REQUIRED')
        from scripts.restore.prepare_identity import check_empty_files,check_empty_docker
        from scripts.restore.control import RestoreControl
        import docker
        host=load();d=receipt(host)
        check_empty_files(host.config['deployment_root']);check_empty_docker()
        api=docker.APIClient(base_url='unix:///var/run/docker.sock',version='1.45',timeout=30)
        try:
            with bounded_session():
                serve(RestoreControl(host,api,Store(host.config['staging_root'])),d,sys.stdin.buffer,sys.stdout.buffer)
        finally:api.close()
    except BaseException:raise SystemExit(2) from None


if __name__=='__main__':main()
