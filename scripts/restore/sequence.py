"""Journaled destination sequence, injected adapters; no launcher or proof emitter.

The approved production identity/IAM/lease/bootstrap adapter is not implemented.
Mock results never turn into functional evidence or operational authorization.
"""
from scripts.backup.sealing.common import SealFailure,backup_id,require
from scripts.backup.schemas import SchemaCatalog
from scripts.backup.semantics import validate_semantics
from scripts.restore.resume import ResumeApproval,CHECKPOINT

STEPS=('download_and_verify','restore_private_files','restore_roles_and_database',
       'copy_exact_object_versions','remap_storage_explicitly','start_without_install',
       'check_application_and_restart')


def execute_candidate(store,operation,selected,destination,source_instance,state_bucket,resume=None):
    backup_id(operation)
    require(resume is None or type(resume) is ResumeApproval,
            'RESTORE_RESUME_APPROVAL_REQUIRED')
    for role,doc in selected.bundle.items():SchemaCatalog().validate(role,doc)
    validate_semantics(selected.bundle)
    manifest=selected.bundle['manifest']
    with store.lock():
        # No booleans supplied by workflow inputs stand in for this adapter's
        # inspection of actual target identity, emptiness and execution control.
        identity=destination.inspect_target()
        require(isinstance(identity,dict) and set(identity)=={'account','region','instance','data_bucket'}
                and identity['instance']!=source_instance and identity['data_bucket'] not in
                {manifest['source']['data_bucket'],state_bucket,*[a['bucket'] for a in manifest['artifacts'].values()]}
                and identity['account']==manifest['source']['account_id']
                and identity['region']==manifest['source']['region'],'RESTORE_TARGET_IDENTITY_REJECTED')
        binding={'operation_id':operation,'backup_id':selected.backup_id,'manifest_sha256':selected.manifest_sha256,
                 'completion_version_id':selected.completion_version,'destination':identity}
        directory=store.operation(operation);path=directory/'restore-journal.json'
        continuation=False
        if path.exists() or path.is_symlink():
            old=store.read(operation+'/restore-journal.json')
            require(old.get('binding')==binding,'RESTORE_JOURNAL_BINDING_MISMATCH')
            if old.get('phase')=='candidate_ready' and old.get('completed_steps')==list(STEPS):
                require(resume is None,'RESTORE_RESUME_CHECKPOINT_REJECTED')
                destination.check_application_and_restart(read_only=True)
                return {'status':'existing_candidate_revalidated','restore_proven':False,'destroy_authorized':False}
            require(resume is not None,'RESTORE_INTERRUPTED_REQUIRES_RECONCILIATION')
            resume.checkpoint(store,operation,old);resume.assert_claimed(store,operation)
            continuation=True;journal=old
        else:
            require(resume is None,'RESTORE_RESUME_CHECKPOINT_REJECTED')
            destination.assert_empty_and_exclusive()
            require(not directory.exists(),'RESTORE_OPERATION_DIRECTORY_NOT_NEW')
            directory.mkdir(mode=0o700)
            journal={'internal_version':1,'kind':'restore-candidate-journal','binding':binding,
                     'phase':'prepared','completed_steps':[],'restore_proven':False,'destroy_authorized':False}
        def save():store.write(operation+'/restore-journal.json',journal)
        if not continuation:save()
        try:
            if continuation:
                destination.assert_identity_and_control(identity)
                destination.revalidate_pre_database(resume)
                journal['phase']='resume_revalidated'
                journal.pop('error_code',None);save()
            for step in (STEPS[len(CHECKPOINT):] if continuation else STEPS):
                destination.assert_identity_and_control(identity)
                journal['phase']=step+'_intent';save()
                getattr(destination,step)()
                journal['completed_steps'].append(step);journal['phase']=step+'_verified';save()
            destination.assert_identity_and_control(identity)
            journal['phase']='candidate_ready';save()
        except BaseException as e:
            journal['phase']='intervention_required'
            journal['error_code']=e.code if isinstance(e,SealFailure) else 'RESTORE_STEP_FAILED'
            save()
            raise SealFailure(journal['error_code']) from None
        return {'status':'restore_candidate_ready','restore_proven':False,'destroy_authorized':False}
