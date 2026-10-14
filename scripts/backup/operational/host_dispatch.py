"""Fixed privileged dispatcher. Never invokes the administrative broker."""
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from .fence_policy import FenceScope,policy_document,require
from .policy_ownership import guard_statement
from .runtime_config import load
from .host_lease import serve_lease
from .host_probe import imds_identity,RealHostProbe
from .host_worker import credentials
from .sdk import clients
from .ssm import Target
from .supervision import Supervisor
from .docker_api import DockerExecAPI
from .host_supervisor import HostSupervisor
from .postgres import PostgreSQL
from ..capture.storage import Store,CaptureFailure,sync_directory
from ..capture.engine import utc


def serve_live_lease(config,incoming,outgoing):
    """One flock-backed lease. Auth scope never comes from Terraform outputs."""
    store=Store(config['staging_root']);sdk={}
    scope={k:config[k] for k in ('project','environment','data_bucket','backups_bucket','state_bucket')}
    fence=FenceScope(config['account'],config['data_bucket'],config['admin_role'],config['emergency_role'])
    def authorize(record,request):
        require(request['scope']==scope,'LEASE_SCOPE_MISMATCH')
        target=Target(config['account'],config['region'],config['instance'],config['guard_role'].rsplit('/',1)[1])
        sdk.update(clients(credentials(record),target))
        found=sdk['sts'].get_caller_identity()
        require(found['Account']==target.account and re.fullmatch(
            f'arn:aws:sts::{target.account}:assumed-role/{re.escape(target.role_name)}/[A-Za-z0-9+=,.@_-]+',found['Arn']),
            'LEASE_GUARD_ROLE_MISMATCH')
    def identity():
        require(imds_identity()=={k:config[k] for k in ('account','region','instance')},'HOST_IDENTITY_MISMATCH')
        response=sdk['s3'].get_bucket_policy(Bucket=fence.bucket,ExpectedBucketOwner=fence.account)
        policy=policy_document(response['Policy'])
        require([s for s in policy['Statement'] if s.get('Sid')=='ExclusiveAdministrativeDataPolicyOwner']==[guard_statement(fence)],
                'POLICY_OWNER_GUARD_MISSING')
        # A permanent guard is not enough if a fence remains or a session expired.
        sdk['sts'].get_caller_identity()
        return {k:config[k] for k in ('account','region','instance')}
    def inspect():
        # Called while the deploy lease owns Store.lock, before any apply.
        import docker
        from .runtime_config import validate
        operation=utc().replace('-','').replace(':','')[:16]+'-'+str(uuid.uuid4())
        # Use the same normative operation format as capture.
        from datetime import datetime,timezone
        operation=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+str(uuid.uuid4())
        store.operation(operation).mkdir(mode=0o700);sync_directory(store.root)
        api=docker.APIClient(base_url='unix:///var/run/docker.sock',version='1.45',timeout=30)
        try:
            exec_api=DockerExecAPI(api);boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            supervisor=Supervisor(store,exec_api,boot);supervisor.initialize(operation)
            probe=RealHostProbe(validate(config),api,store,operation,supervisor,Path(__file__).with_name('read-state.php'))
            containers=probe.containers();state=probe.read_state()
            pg=PostgreSQL(supervisor,operation,containers['postgres']['Id'],state['database'])
            pg.containers=[i['Id'] for i in containers.values()]
            return HostSupervisor(store,exec_api,boot,pg).inspect()
        finally:api.close()
    serve_lease(store,identity,inspect,incoming,outgoing,authorize)


def dispatch(mode):
    require(os.name=='posix' and os.geteuid()==0,'LINUX_ROOT_REQUIRED')
    require(mode in ('lease','backup'),'HOST_COMMAND_REJECTED')
    config=load()
    if mode=='lease':
        serve_live_lease(config,sys.stdin.buffer,sys.stdout.buffer);return
    # No caller-provided unit, argv, environment or service. systemd owns the
    # entire process group; timeout never signifies successful reconciliation.
    command=['/usr/bin/systemd-run','--quiet','--wait','--pipe','--collect',
        '--unit=nextcloud-capture-'+uuid.uuid4().hex,'--property=Type=exec',
        '--property=KillMode=control-group','--property=RuntimeMaxSec=2700',
        '--property=TimeoutStopSec=30','--property=UMask=0077',
        '--property=WorkingDirectory=/usr/local/lib/nextcloud-recovery/repository',
        '/usr/local/lib/nextcloud-recovery/venv/bin/python','-m','scripts.backup.operational.host_worker']
    result=subprocess.run(command,stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=subprocess.DEVNULL,
                          env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    require(result.returncode==0,'CAPTURE_SUPERVISOR_INTERVENTION_REQUIRED')


def main():
    try:
        require(len(sys.argv)==2,'HOST_COMMAND_REJECTED');dispatch(sys.argv[1])
    except BaseException:
        # Any worker JSON result precedes this exit. No exception contents.
        raise SystemExit(2) from None


if __name__=='__main__':main()
