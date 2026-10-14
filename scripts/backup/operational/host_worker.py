"""One host-side operation, supervised externally by the fixed systemd wrapper."""
from datetime import datetime,timezone
import os
from pathlib import Path
import re
import sys
import uuid
from .runtime_config import load,validate
from .sdk import TemporaryCredentials,clients
from .ssm import Target
from .fence_policy import FenceScope,require
from .fence_admin_api import AdministrativePolicyAPI
from .registered_authority import RegisteredAuthority
from .admin_broker import BrokerAPI,BrokerAuthority
from .fencing import Coordinator,CaptureStoreView
from .drained_capture import DrainedCoordinator,LiveCapture
from .live_capture import LiveCaptureBackend,LiveWriterBarrier
from .host_probe import RealHostProbe,imds_identity
from .capture_backend import DockerControls
from .docker_api import DockerExecAPI
from .supervision import Supervisor
from .host_supervisor import HostSupervisor
from .postgres import PostgreSQL
from .backup_lease import backup_lease
from .s3 import DataInventory,BackupS3
from .completion import CompletionWriter,OperationalSeal
from .inventory_hooks import OperationalInventoryHooks
from .inventory_projection import InventoryProjection
from ..capture.storage import Store,CaptureFailure,sync_directory
from ..capture.engine import Capture,utc
from contextlib import contextmanager
import signal
from ..json_input import parse_json
from ..limits import Limits
from ..sealing.inventory import inventory
from ..sealing.common import Bounds,json_bytes


@contextmanager
def controlled_signals():
    previous={}
    def cancel(signum,frame):raise CaptureFailure('BACKUP_CANCELLED')
    try:
        for signum in (signal.SIGINT,signal.SIGTERM):previous[signum]=signal.signal(signum,cancel)
        yield
    finally:
        for signum,handler in previous.items():signal.signal(signum,handler)


def credentials(record):
    require(isinstance(record,dict) and set(record)=={'access_key','secret_key','token','expiration'}, 'SESSION_CREDENTIALS_INVALID')
    try:expires=datetime.fromisoformat(record['expiration'].replace('Z','+00:00'))
    except Exception:raise CaptureFailure('SESSION_EXPIRATION_INVALID') from None
    require(expires.tzinfo is not None and 3000<=(expires-datetime.now(timezone.utc)).total_seconds()<=7500,
            'SESSION_LIFETIME_INSUFFICIENT')
    return TemporaryCredentials(record['access_key'],record['secret_key'],record['token'])


def parse_request(raw,config):
    value=parse_json(raw,Limits(json_bytes=65536))
    require(isinstance(value,dict) and set(value)=={'version','owner','commit','repository','confirmation','scope','backup_credentials'}
            and type(value['version']) is int and value['version']==1 and value['confirmation']=='BACKUP'
            and re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}',value['owner']) and re.fullmatch(r'[a-f0-9]{40}',value['commit'])
            and value['repository']==config['repository'],'BACKUP_REQUEST_REJECTED')
    require(value['scope']=={key:config[key] for key in ('account','region','instance','project','environment','data_bucket','backups_bucket')},
            'BACKUP_SCOPE_MISMATCH')
    credentials(value['backup_credentials']);return value


def storage_preflight(client,config):
    for bucket in (config['data_bucket'],config['backups_bucket']):
        args={'Bucket':bucket,'ExpectedBucketOwner':config['account']}
        try:
            location=client.get_bucket_location(**args).get('LocationConstraint') or 'us-east-1'
            require(location==config['region'],'STORAGE_REGION_MISMATCH')
            require(client.get_bucket_versioning(**args).get('Status')=='Enabled','STORAGE_VERSIONING_REQUIRED')
            rules=client.get_bucket_encryption(**args)['ServerSideEncryptionConfiguration']['Rules']
            require(len(rules)==1 and rules[0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']=='AES256',
                    'STORAGE_ENCRYPTION_MISMATCH')
        except CaptureFailure:raise
        except BaseException:raise CaptureFailure('STORAGE_PREFLIGHT_FAILED') from None


def execute(config,request):
    target=validate(config)
    require(imds_identity()=={'account':target.account,'region':target.region,'instance':target.instance}, 'HOST_IDENTITY_MISMATCH')
    store=Store(config['staging_root'])
    operation=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+str(uuid.uuid4())
    directory=store.operation(operation);directory.mkdir(mode=0o700);sync_directory(store.root)
    caller=Target(target.account,target.region,target.instance,config['backup_role'].rsplit('/',1)[1])
    backup_clients=clients(credentials(request['backup_credentials']),caller)
    for client,identity in ((backup_clients,caller),):
        found=client['sts'].get_caller_identity()
        require(found['Account']==identity.account and re.fullmatch(
            f'arn:aws:sts::{identity.account}:assumed-role/{re.escape(identity.role_name)}/[A-Za-z0-9+=,.@_-]+',found['Arn']),
            'HOST_SESSION_IDENTITY_MISMATCH')
    storage_preflight(backup_clients['s3'],config)
    with backup_lease(store,operation,request['owner']):
        # Docker connects only after host, sessions and root configuration match.
        import docker
        api=docker.APIClient(base_url='unix:///var/run/docker.sock',version='1.45',timeout=30)
        docker_exec=DockerExecAPI(api);boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        supervisor=Supervisor(store,docker_exec,boot);supervisor.initialize(operation)
        probe=RealHostProbe(target,api,store,operation,supervisor,Path(__file__).with_name('read-state.php'))
        found=probe.containers();state=probe.read_state()
        pg=PostgreSQL(supervisor,operation,found['postgres']['Id'],state['database']);pg.containers=[i['Id'] for i in found.values()]
        host=HostSupervisor(store,docker_exec,boot,pg)
        source={'account_id':target.account,'project':target.project,'environment':config['environment'],
                'installation_identity_sha256':target.installation_identity,'data_bucket':target.data_bucket,'region':target.region}
        data=DataInventory(backup_clients['s3'],target.data_bucket,target.account,target.region)
        scope=FenceScope(target.account,target.data_bucket,config['admin_role'],config['emergency_role'])
        policy=BrokerAPI(scope,InventoryProjection(data,source,operation)).open()
        authority=BrokerAuthority(policy)
        barrier=LiveWriterBarrier(store,host.inspect)
        coordinator=DrainedCoordinator(store,policy,scope,authority,barrier)
        with coordinator.window(operation) as window:
            backend=LiveCaptureBackend(target,probe,DockerControls(api),supervisor,window);barrier.attach(backend)
            hooks=OperationalInventoryHooks(data,source,window)
            result=LiveCapture(store,backend,hooks,coordinator,window).start();bid=result['operation_id']
        journal=store.read(bid+'/journal.json');original=journal['original']
        compatibility={'repository_commit':request['commit'],'nextcloud_version':'34.0.4','postgres_major':17,
            'nextcloud_image':target.images['nextcloud'],'postgres_image':target.images['postgres'],
            'database_name':original['database'],'database_owner':original['owner'],'table_prefix':original['table_prefix']}
        capture_scope={k:True for k in ('postgres_application_database','database_roles_and_extensions_in_parameters',
            'nextcloud_all_config_and_storage_marker','mutable_local_files_apps_themes','required_secret_values_sse_s3',
            'observability_volumes_excluded','redis_cache_excluded')}
        backups=BackupS3(backup_clients['s3'],config['backups_bucket'],target.account,target.region,bid)
        completion=CompletionWriter(backup_clients['s3'],config['backups_bucket'],target.account,bid,target.region)
        try:
            return OperationalSeal(store,data,backups,completion,source,compatibility,capture_scope,Bounds(timeout_seconds=1800)).seal(bid)
        finally:
            policy.close();api.close()


def main():
    try:
        config=load();raw=sys.stdin.buffer.readline(65537)
        require(raw.endswith(b'\n') and len(raw)<=65536,'BACKUP_REQUEST_LIMIT')
        request=parse_request(raw,config)
        with controlled_signals():result=execute(config,request)
        sys.stdout.buffer.write(json_bytes(result)+b'\n');sys.stdout.buffer.flush()
    except BaseException as error:
        code=error.code if isinstance(error,CaptureFailure) and re.fullmatch(r'[A-Z0-9_]{1,96}',error.code) else 'BACKUP_INTERVENTION_REQUIRED'
        sys.stdout.buffer.write(json_bytes({'code':code,'backup_complete':False,'restore_proven':False,'destroy_authorized':False})+b'\n')
        raise SystemExit(2) from None


if __name__=='__main__':main()
