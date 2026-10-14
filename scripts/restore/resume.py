"""One root-approved pre-database continuation; no CLI, expiry or automatic unlock."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from scripts.backup.safe_files import SafeRoot,signature
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import require,backup_id,version,SealFailure
from scripts.restore.private_files import write_new,tree_digest

CHECKPOINT=('download_and_verify','restore_private_files')
APPROVAL_ROOT=Path('/etc/nextcloud-restore')
FIELDS={'version','kind','binding','commit','run_owner','previous_owner','journal_sha256',
        'exec_journal_sha256','host_command_sha256','lease_sha256','boot_id','tree_sha256'}


def current_boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def validate(document):
    require(isinstance(document,dict) and set(document)==FIELDS and type(document['version']) is int
            and document['version']==1 and document['kind']=='restore-pre-database-resume',
            'RESTORE_RESUME_APPROVAL_INVALID')
    b=document['binding']
    require(isinstance(b,dict) and set(b)=={'operation_id','backup_id','manifest_sha256','completion_version_id','destination'},
            'RESTORE_RESUME_BINDING_REJECTED')
    backup_id(b['operation_id']);backup_id(b['backup_id']);version(b['completion_version_id'])
    destination=b['destination']
    require(isinstance(destination,dict) and set(destination)=={'account','region','instance','data_bucket'}
            and all(isinstance(v,str) for v in destination.values())
            and re.fullmatch('[0-9]{12}',str(destination['account']))
            and re.fullmatch('i-[a-f0-9]{17}',str(destination['instance']))
            and re.fullmatch('[a-z]{2}(?:-[a-z]+)+-[0-9]+',str(destination['region']))
            and re.fullmatch('[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',str(destination['data_bucket'])),
            'RESTORE_RESUME_BINDING_REJECTED')
    for owner in ('run_owner','previous_owner'):
        require(isinstance(document[owner],str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',document[owner]),
                'RESTORE_RESUME_OWNER_REJECTED')
    require(document['run_owner']!=document['previous_owner'],'RESTORE_RESUME_OWNER_REJECTED')
    trees=document['tree_sha256']
    require(isinstance(trees,dict) and set(trees)=={'configuration','html','runtime'},'RESTORE_RESUME_APPROVAL_INVALID')
    hashes=[document[k] for k in ('commit','journal_sha256','exec_journal_sha256','host_command_sha256','lease_sha256')]
    require(isinstance(hashes[0],str) and re.fullmatch('[a-f0-9]{40}',hashes[0])
            and all(isinstance(h,str) and re.fullmatch('[a-f0-9]{64}',h) for h in hashes[1:]+[b['manifest_sha256'],*trees.values()]),
            'RESTORE_RESUME_APPROVAL_INVALID')
    require(isinstance(document['boot_id'],str) and re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',document['boot_id']),
            'RESTORE_RESUME_BOOT_REJECTED')
    return document


@dataclass(frozen=True,repr=False)
class ResumeApproval:
    document: dict
    approval_sha256: str

    def assert_unchanged(self):
        current=load()
        require(current is not None and current.approval_sha256==self.approval_sha256
                and current.document==self.document,'RESTORE_RESUME_APPROVAL_CHANGED')

    def assert_request(self,request,selected,config):
        self.assert_unchanged();d=self.document;b=d['binding']
        require(request['owner']==d['run_owner'] and request['commit']==d['commit']
                and selected.backup_id==b['backup_id'] and selected.manifest_sha256==b['manifest_sha256']
                and selected.completion_version==b['completion_version_id']
                and b['destination']=={k:config[k] for k in ('account','region','instance','data_bucket')},
                'RESTORE_RESUME_REQUEST_REJECTED')

    def checkpoint(self,store,operation,record):
        self.assert_unchanged();d=self.document
        require(operation==d['binding']['operation_id'] and record.get('binding')==d['binding']
                and record.get('phase')=='intervention_required' and record.get('completed_steps')==list(CHECKPOINT)
                and record.get('error_code') in ('RESTORE_STEP_FAILED','JSON_OBJECT_REQUIRED')
                and record.get('restore_proven') is False and record.get('destroy_authorized') is False,
                'RESTORE_RESUME_CHECKPOINT_REJECTED')
        with SafeRoot(store.operation(operation)) as safe:
            for name,key in [('restore-journal.json','journal_sha256'),('exec-journal.json','exec_journal_sha256'),
                             ('host-command.json','host_command_sha256')]:
                with safe.open(name) as f:
                    raw=f.read(1024*1024+1)
                    require(len(raw)<=1024*1024 and hashlib.sha256(raw).hexdigest()==d[key],
                            'RESTORE_RESUME_EVIDENCE_CHANGED')
            safe.assert_unchanged()
        execs=store.read(operation+'/exec-journal.json')
        require(execs.get('operation_id')==operation and execs.get('boot_id')==d['boot_id']
                and isinstance(execs.get('execs'),list) and len(execs['execs'])==3
                and all(isinstance(e,dict) and e.get('phase')=='finished' and type(e.get('exit_code')) is int and e['exit_code']==0 for e in execs['execs']),
                'RESTORE_RESUME_EXECS_REJECTED')
        require(store.read(operation+'/host-command.json').get('phase')=='finished',
                'RESTORE_RESUME_COMMAND_REJECTED')
        directory=store.operation(operation)
        require(not any((directory/name).exists() or (directory/name).is_symlink() for name in
                        ('postgres-fence.json','object-version-map.json','remap-helper.json')),
                'RESTORE_RESUME_CHECKPOINT_REJECTED')

    def claim(self,store,shared,owner):
        self.assert_unchanged();d=self.document;operation=d['binding']['operation_id']
        require(owner==d['run_owner'],'RESTORE_RESUME_OWNER_REJECTED')
        self.checkpoint(store,operation,store.read(operation+'/restore-journal.json'))
        require(current_boot()==d['boot_id'],
                'RESTORE_RESUME_BOOT_REJECTED')
        directory=store.operation(operation)
        require(not (directory/'resume-claim.json').exists() and not (directory/'resume-claim.json').is_symlink(),
                'RESTORE_RESUME_ALREADY_CLAIMED')
        expected={'kind':'deploy-lease','version':1,'owner':d['previous_owner'],'phase':'intervention_required'}
        require(shared.read('deploy-lease.json')==expected,'RESTORE_RESUME_LEASE_REJECTED')
        with SafeRoot(shared.root) as safe,safe.open('deploy-lease.json') as f:
            before=signature(f);raw=f.read(4097)
            require(len(raw)<=4096 and signature(f)==before and hashlib.sha256(raw).hexdigest()==d['lease_sha256'],
                    'RESTORE_RESUME_LEASE_REJECTED')
        # Durable, non-overwritable claim precedes any lease transition. A crash
        # at any later point requires a new explicit administrative decision.
        write_new(directory/'resume-claim.json',json.dumps({'version':1,'owner':owner,
            'approval_sha256':self.approval_sha256,'binding':d['binding']}).encode())
        write_new(directory/'resume-lease-before.json',raw)
        with SafeRoot(directory) as safe,safe.open('restore-journal.json') as f:original=f.read(1024*1024+1)
        write_new(directory/'resume-journal-before.json',original)
        shared.write('deploy-lease.json',{'kind':'deploy-lease','version':1,'owner':owner,'phase':'held'})

    def assert_claimed(self,store,operation):
        self.assert_unchanged();d=self.document
        path=store.operation(operation)/'resume-claim.json'
        require(path.is_file() and not path.is_symlink(),'RESTORE_RESUME_CLAIM_REQUIRED')
        claim=store.read(operation+'/resume-claim.json')
        require(claim=={'version':1,'owner':d['run_owner'],'approval_sha256':self.approval_sha256,'binding':d['binding']},
                'RESTORE_RESUME_CLAIM_REQUIRED')

    def assert_trees(self,directory):
        self.assert_unchanged()
        for name,expected in self.document['tree_sha256'].items():
            require(tree_digest(Path(directory)/name)==expected,'RESTORE_RESUME_FILES_CHANGED')


def load():
    require(os.name=='posix' and os.geteuid()==0,'RESTORE_RESUME_ROOT_REQUIRED')
    path=APPROVAL_ROOT/'resume-approval.json'
    if not path.exists() and not path.is_symlink():return None
    require(not path.is_symlink(),'RESTORE_RESUME_APPROVAL_UNSAFE')
    parent=APPROVAL_ROOT.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid==parent.st_gid==0
            and stat.S_IMODE(parent.st_mode)==0o700,'RESTORE_RESUME_APPROVAL_UNSAFE')
    with SafeRoot(APPROVAL_ROOT) as safe,safe.open('resume-approval.json') as f:
        info=os.fstat(f.fileno());before=signature(f)
        require(info.st_uid==info.st_gid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,
                'RESTORE_RESUME_APPROVAL_UNSAFE')
        raw=f.read(16385)
        require(len(raw)<=16384 and signature(f)==before,'RESTORE_RESUME_APPROVAL_CHANGED');safe.assert_unchanged()
    return ResumeApproval(validate(parse_json(raw,Limits(json_bytes=16384))),hashlib.sha256(raw).hexdigest())


_APPROVAL_WAIT_SECONDS=300


def _waiting_snapshot(store,operation):
    result={}
    with SafeRoot(store.operation(operation)) as safe:
        for name in ('restore-journal.json','exec-journal.json','host-command.json'):
            with safe.open(name) as f:
                before=signature(f);raw=f.read(1024*1024+1)
                require(len(raw)<=1024*1024 and signature(f)==before,'RESTORE_RESUME_EVIDENCE_CHANGED')
                result[name]=(hashlib.sha256(raw).hexdigest(),before)
        safe.assert_unchanged()
    return result


def await_approval(store,selected,config,request):
    """Read-only, bounded coordination; absence never grants execution authority."""
    from scripts.restore.sequence import STEPS
    approval=load()
    if approval is not None:
        approval.assert_request(request,selected,config);return approval
    expected={'backup_id':selected.backup_id,'manifest_sha256':selected.manifest_sha256,
              'completion_version_id':selected.completion_version,
              'destination':{k:config[k] for k in ('account','region','instance','data_bucket')}}
    matches=[]
    for path in store.root.iterdir():
        require(not path.is_symlink(),'RESTORE_JOURNAL_PATH_REJECTED')
        if not path.is_dir():continue
        store.operation(path.name);record=store.read(path.name+'/restore-journal.json')
        binding=record.get('binding',{})
        if {k:binding.get(k) for k in expected}!=expected:continue
        require(binding.get('operation_id')==path.name,'RESTORE_JOURNAL_BINDING_REJECTED')
        if record.get('phase')=='candidate_ready' and record.get('completed_steps')==list(STEPS):continue
        require(record.get('phase')=='intervention_required' and record.get('completed_steps')==list(CHECKPOINT)
                and record.get('error_code') in ('RESTORE_STEP_FAILED','JSON_OBJECT_REQUIRED')
                and record.get('restore_proven') is False and record.get('destroy_authorized') is False,
                'RESTORE_RESUME_CHECKPOINT_REJECTED')
        matches.append(path.name)
    require(len(matches)<=1,'RESTORE_COMPLETED_OPERATION_AMBIGUOUS')
    if not matches:return None # Fresh install or already completed: no new delay.
    operation=matches[0]
    require(not (store.operation(operation)/'resume-claim.json').exists()
            and not (store.operation(operation)/'resume-claim.json').is_symlink(),
            'RESTORE_RESUME_ALREADY_CLAIMED')
    before=_waiting_snapshot(store,operation);deadline=time.monotonic()+_APPROVAL_WAIT_SECONDS
    while time.monotonic()<deadline:
        require(_waiting_snapshot(store,operation)==before,'RESTORE_RESUME_EVIDENCE_CHANGED')
        approval=load()
        if approval is not None:
            approval.assert_request(request,selected,config)
            require(approval.document['binding']['operation_id']==operation,'RESTORE_RESUME_BINDING_REJECTED')
            approval.checkpoint(store,operation,store.read(operation+'/restore-journal.json'))
            return approval
        time.sleep(1)
    raise SealFailure('RESTORE_RESUME_APPROVAL_TIMEOUT')
