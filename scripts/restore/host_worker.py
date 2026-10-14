"""One explicitly authorized destination restore, under the fixed systemd supervisor."""
from datetime import datetime,timezone
from contextlib import contextmanager
import ipaddress
from pathlib import Path
import re
import sys
import uuid
import signal
from scripts.backup.capture.storage import Store,CaptureFailure
from scripts.backup.sealing.common import require,json_bytes,backup_id
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.safe_files import SafeRoot,signature
import os
import stat
from scripts.backup.operational.host_worker import credentials
from scripts.backup.operational.sdk import clients,validate_ec2
from scripts.backup.operational.ssm import Target
from scripts.restore.host_scope import load
from scripts.restore.access import load_access
from scripts.restore.selection import Selector
from scripts.restore.s3_reader import BackupReader,SourceObjects,DestinationObjects
from scripts.restore.control import RestoreControl
from scripts.restore.host_runtime import HostRuntime
from scripts.restore.executor import execute_restore
from scripts.restore.sequence import STEPS
from scripts.backup.operational import runtime_config
from scripts.restore.resume import await_approval,ResumeApproval


@contextmanager
def controlled_signals():
    previous={}
    def cancel(signum,frame):raise CaptureFailure('RESTORE_CANCELLED')
    try:
        for signum in (signal.SIGINT,signal.SIGTERM):previous[signum]=signal.signal(signum,cancel)
        yield
    finally:
        for signum,handler in previous.items():signal.signal(signum,handler)


def completed_operation(store,selected,config,resume=None):
    matches=[]
    expected={'backup_id':selected.backup_id,'manifest_sha256':selected.manifest_sha256,
              'completion_version_id':selected.completion_version,
              'destination':{k:config[k] for k in ('account','region','instance','data_bucket')}}
    for path in store.root.iterdir():
        require(not path.is_symlink(),'RESTORE_JOURNAL_PATH_REJECTED')
        if not path.is_dir():continue
        store.operation(path.name);record=store.read(path.name+'/restore-journal.json')
        binding=record.get('binding',{})
        if {k:binding.get(k) for k in expected}==expected:
            require(binding.get('operation_id')==path.name,'RESTORE_JOURNAL_BINDING_REJECTED')
            if record.get('phase')!='candidate_ready' or record.get('completed_steps')!=list(STEPS):
                require(type(resume) is ResumeApproval,'RESTORE_INTERRUPTED_REQUIRES_RECONCILIATION')
                resume.checkpoint(store,path.name,record)
            matches.append(path.name)
    require(len(matches)<=1,'RESTORE_COMPLETED_OPERATION_AMBIGUOUS')
    return matches[0] if matches else None


def parse_request(raw,host,access,revision):
    r=parse_json(raw,Limits(json_bytes=65536))
    fields={'version','owner','repository','commit','confirmation','github_environment','resource_environment',
            'selection','backup_id','scope','public_ip','restore_credentials'}
    require(isinstance(r,dict) and set(r)==fields and type(r['version']) is int and r['version']==1
            and r['confirmation']=='RESTORE','RESTORE_REQUEST_REJECTED')
    require(r['repository']==access['repository'] and r['commit']==revision and re.fullmatch('[a-f0-9]{40}',revision)
            and r['github_environment']==access['github_environment']
            and r['resource_environment']==access['resource_environment'],'RESTORE_REQUEST_BINDING_REJECTED')
    require(isinstance(r['owner'],str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',r['owner'])
            and r['scope']=={k:host[k] for k in ('account','region','instance','source_instance','project')},
            'RESTORE_REQUEST_SCOPE_REJECTED')
    require(r['selection'] in ('specific','latest_valid'),'RESTORE_SELECTION_REJECTED')
    if r['selection']=='specific':backup_id(r['backup_id'])
    else:require(r['backup_id']=='','RESTORE_SELECTION_AMBIGUOUS')
    try:public=ipaddress.IPv4Address(r['public_ip'])
    except (ValueError,TypeError):raise CaptureFailure('RESTORE_PUBLIC_HOST_REJECTED') from None
    require(public.is_global and str(public)==r['public_ip'],'RESTORE_PUBLIC_HOST_REJECTED')
    credentials(r['restore_credentials']);return r


def execute(host,access,request):
    host.identity();c=host.config
    guard=runtime_config.load()
    require(guard['environment']==access['resource_environment'] and guard['repository']==access['repository']
            and guard['images']==c['images'] and guard['deployment_root']==c['deployment_root']
            and guard['installation_identity']==access['source']['installation_identity_sha256'],
            'RESTORE_SHARED_GUARD_BINDING_REJECTED')
    target=Target(c['account'],c['region'],c['instance'],access['role_arn'].rsplit('/',1)[1])
    sdk=clients(credentials(request['restore_credentials']),target)
    identity=sdk['sts'].get_caller_identity()
    require(identity['Account']==target.account and re.fullmatch('arn:aws:sts::'+target.account+':assumed-role/'+
            re.escape(target.role_name)+'/[A-Za-z0-9+=,.@_-]+',identity['Arn']),'RESTORE_CALLER_ROLE_REJECTED')
    validate_ec2(sdk['ec2'],target,c['project'],access['resource_environment'])
    node=sdk['ec2'].describe_instances(InstanceIds=[target.instance])['Reservations'][0]['Instances'][0]
    require(node['InstanceId']==c['instance'] and node['PublicIpAddress']==request['public_ip'],'RESTORE_PUBLIC_HOST_CHANGED')
    reader=BackupReader(sdk['s3'],c['backups_bucket'],c['account'],c['region'])
    source=SourceObjects(sdk['s3'],c['source_data_bucket'],c['account'],c['region'])
    selected=Selector(reader,source,access['source']).select(request['selection'],request['backup_id'] or None)
    objects=DestinationObjects(sdk['s3'],c['data_bucket'],c['account'],c['region'],c['source_data_bucket'],
                               [c['backups_bucket'],c['state_bucket']])
    import docker
    store=Store(c['staging_root']);resume=await_approval(store,selected,c,request)
    operation=completed_operation(store,selected,c,resume)
    require(resume is None or operation==resume.document['binding']['operation_id'],
            'RESTORE_RESUME_CHECKPOINT_REJECTED')
    if operation is None:operation=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+str(uuid.uuid4())
    api=docker.APIClient(base_url='unix:///var/run/docker.sock',version='1.45',timeout=30)
    try:
        control=RestoreControl(host,api,store)
        runtime=HostRuntime(host,api,control,store,operation,Path('/usr/local/lib/nextcloud-recovery/repository'),request['public_ip'])
        execute_restore(selected,reader,source,objects,runtime,request['owner'],resume)
        return {'code':'RESTORE_CANDIDATE_READY','operation_id':operation,'backup_id':selected.backup_id,
                'restore_proven':False,'destroy_authorized':False}
    finally:api.close()


def main():
    r=None
    try:
        require(len(sys.argv)==1,'RESTORE_WORKER_ARGUMENTS_REJECTED');host=load();access=load_access(host.config)
        with SafeRoot('/etc/nextcloud-restore') as safe,safe.open('reviewed-commit') as stream:
            before=signature(stream);info=os.fstat(stream.fileno())
            require(info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,
                    'RESTORE_REVISION_FILE_UNSAFE')
            revision=stream.read(42).decode('ascii').strip()
            require(signature(stream)==before,'RESTORE_REVISION_FILE_CHANGED');safe.assert_unchanged()
        raw=sys.stdin.buffer.readline(65537);require(raw.endswith(b'\n') and len(raw)<=65536,'RESTORE_REQUEST_LIMIT')
        r=parse_request(raw,host.config,access,revision)
        with controlled_signals():result=execute(host,access,r)
        sys.stdout.buffer.write(json_bytes(result)+b'\n');sys.stdout.buffer.flush()
    except BaseException as error:
        code=error.code if isinstance(error,CaptureFailure) and re.fullmatch('[A-Z0-9_]{1,96}',error.code) else 'RESTORE_INTERVENTION_REQUIRED'
        sys.stdout.buffer.write(json_bytes({'code':code,'restore_proven':False,'destroy_authorized':False})+b'\n')
        raise SystemExit(2) from None
    finally:
        if r is not None:r['restore_credentials'].clear()


if __name__=='__main__':main()
