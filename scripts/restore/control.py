"""Reuse the durable PR7 host lease for restore. No automatic recovery/unlock."""
from contextlib import contextmanager
import os
import re
from pathlib import Path
from scripts.backup.capture.storage import Store
from scripts.backup.operational.lease import deploy_lease
from scripts.backup.operational.docker_api import DockerExecAPI
from scripts.backup.operational import runtime_config
from scripts.backup.sealing.common import require,backup_id,version
from scripts.restore.host_scope import RestoreHost
from scripts.backup.operational.supervision import Supervisor
from scripts.restore.sequence import STEPS
from scripts.restore.resume import ResumeApproval
from scripts.backup.operational.interlock import deployment_guard
from scripts.backup.capture.storage import CaptureFailure


class RestoreControl:
    def __init__(self,host,api,staging):
        import docker
        require(type(host) is RestoreHost and type(api) is docker.APIClient and type(staging) is Store
                and not staging.synthetic and api.base_url=='http+docker://localhost'
                and api._custom_adapter.socket_path=='/var/run/docker.sock','RESTORE_REAL_CONTROL_REQUIRED')
        require(staging.root==Path(host.config['staging_root']),'RESTORE_STAGING_SCOPE_REJECTED')
        self.host,self.api,self.staging=host,api,staging
        self.shared=Store('/var/lib/nextcloud-recovery');self.owner=None;self.resume_authorization=None

    def inspect(self):
        blockers=[];bridge=DockerExecAPI(self.api)
        for item in self.api.containers(all=True):
            if bridge.running_execs(item['Id']):blockers.append('UNTRACKED_DOCKER_EXEC')
        for path in self.staging.root.iterdir():
            require(not path.is_symlink(),'RESTORE_JOURNAL_PATH_REJECTED')
            if not path.is_dir():
                require(path.name=='capture.lock' and path.is_file(),'RESTORE_STAGING_ENTRY_REJECTED')
                continue
            relative=path.name+'/restore-journal.json';self.staging.operation(path.name)
            record=self.staging.read(relative)
            c=self.host.config
            require(isinstance(record,dict) and type(record.get('internal_version')) is int and record['internal_version']==1
                    and record.get('kind')=='restore-candidate-journal' and isinstance(record.get('binding'),dict)
                    and set(record['binding'])=={'operation_id','backup_id','manifest_sha256','completion_version_id','destination'}
                    and record['binding'].get('operation_id')==path.name
                    and record['binding'].get('destination')=={k:c[k] for k in ('account','region','instance','data_bucket')}
                    and record.get('restore_proven') is False and record.get('destroy_authorized') is False,
                    'RESTORE_JOURNAL_BINDING_REJECTED')
            backup_id(record['binding']['backup_id']);version(record['binding']['completion_version_id'])
            require(isinstance(record['binding']['manifest_sha256'],str)
                    and re.fullmatch('[a-f0-9]{64}',record['binding']['manifest_sha256']),
                    'RESTORE_JOURNAL_BINDING_REJECTED')
            if record.get('phase')!='candidate_ready' or record.get('completed_steps')!=list(STEPS):
                approval=getattr(self,'resume_authorization',None)
                if type(approval) is ResumeApproval and path.name==approval.document['binding']['operation_id']:
                    approval.checkpoint(self.staging,path.name,record)
                    require(not (path/'resume-claim.json').exists(),'RESTORE_RESUME_ALREADY_CLAIMED')
                else:blockers.append('RESTORE_INTERRUPTED')
            if (path/'host-command.json').exists():
                command=self.staging.read(path.name+'/host-command.json')
                if command.get('phase')!='finished':blockers.append('RESTORE_COMMAND_INTERRUPTED')
            if (path/'exec-journal.json').exists():
                boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                supervisor=Supervisor(self.staging,bridge,boot)
                execution=supervisor.load(path.name)
                require(re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',execution['boot_id']),
                        'EXEC_JOURNAL_INVALID')
                historical=(execution['boot_id']!=boot and record.get('phase')=='candidate_ready'
                    and record.get('completed_steps')==list(STEPS)
                    and (path/'host-command.json').exists()
                    and command.get('phase')=='finished')
                if historical:
                    # A reboot does not invalidate durably completed history.
                    # Current Docker execs are still inspected above; unfinished
                    # operations keep the original reboot/intervention guard.
                    identifiers=set()
                    for entry in execution['execs']:
                        require(isinstance(entry,dict) and set(entry)=={
                            'container_id','exec_id','phase','command_sha256','exit_code'}
                            and entry.get('phase')=='finished'
                            and type(entry.get('exit_code')) is int and 0<=entry['exit_code']<=255
                            and all(isinstance(entry.get(key),str) and re.fullmatch('[a-f0-9]{64}',entry[key])
                                    for key in ('container_id','exec_id','command_sha256'))
                            and entry['exec_id'] not in identifiers,'EXEC_JOURNAL_INVALID')
                        identifiers.add(entry['exec_id'])
                else:
                    blockers.extend(supervisor.blockers(path.name))
        return blockers

    def assert_held(self):
        require(self.owner is not None,'RESTORE_EXCLUSIVE_CONTROL_REQUIRED')
        self.host.identity();active=self.shared.read('deploy-lease.json')
        require(active=={'kind':'deploy-lease','version':1,'owner':self.owner,'phase':'held'},
                'RESTORE_EXCLUSIVE_CONTROL_LOST')

    @contextmanager
    def hold(self,owner,resume=None):
        # The separately installed PR7 guard must be bound to the new destination.
        # No initial-install bypass, remote-source lease or expiry-based unlock.
        require(os.name=='posix' and os.geteuid()==0 and self.owner is None,'RESTORE_LINUX_CONTROL_REQUIRED')
        self.host.identity();binding=runtime_config.load()
        for key in ('account','region','instance','project','data_bucket','backups_bucket','state_bucket'):
            require(binding[key]==self.host.config[key],'RESTORE_SHARED_GUARD_SCOPE_MISMATCH')
        require(resume is None or type(resume) is ResumeApproval,'RESTORE_RESUME_APPROVAL_REQUIRED')
        if resume is None:
            with deploy_lease(self.shared,owner,self.inspect):
                self.owner=owner
                try:self.assert_held();yield
                finally:self.owner=None
            return
        require(owner==resume.document['run_owner'],'RESTORE_RESUME_OWNER_REJECTED')
        self.resume_authorization=resume
        try:
            with deployment_guard(self.shared,self.inspect):
                # The same kernel lock and all unrelated recovery guards remain
                # mandatory. Only this exact reviewed interrupted lease changes.
                resume.claim(self.staging,self.shared,owner)
                self.owner=owner
                try:
                    self.assert_held();yield
                except BaseException:
                    self.shared.write('deploy-lease.json',{'kind':'deploy-lease','version':1,
                        'owner':owner,'phase':'intervention_required'})
                    raise
                else:
                    self.shared.write('deploy-lease.json',{'kind':'deploy-lease','version':1,
                        'owner':owner,'phase':'released'})
                finally:self.owner=None
        finally:self.resume_authorization=None
