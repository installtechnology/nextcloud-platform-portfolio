"""One explicit administrative grant; injected clients, no import/network IO.

Not a workflow permission, unlock or retry helper. The caller must independently
review the configuration, exact published SHA and run/attempt before invoking it.
"""
from scripts.backup.sealing.common import require
from scripts.backup.capture.storage import CaptureFailure
from scripts.restore.reprovision import configuration
from scripts.restore.creation_lease import CreationLease,encoded,digest


class ResumeObserver:
    """Administrative read-only collector for the existing grant issuer command.

    Pass an independently authenticated administrative S3 reader, not the issuer
    role (which cannot read Terraform state). No credentials or raw state logged.
    The existing gh authentication reads run/job status; it never dispatches.
    """
    def __init__(self,state_reader,gh='gh'):
        self.state_reader=state_reader;self.gh=gh

    def github(self,path):
        import json,subprocess
        result=subprocess.run([self.gh,'api',path],capture_output=True,timeout=30)
        require(result.returncode==0 and len(result.stdout)<=1048576,
                'CREATION_RESUME_WORKFLOW_UNVERIFIED')
        from scripts.backup.json_input import parse_json
        from scripts.backup.limits import Limits
        return parse_json(result.stdout,Limits(json_bytes=1048576))

    def collect(self,c,owner,commit):
        import hashlib
        from scripts.restore.reprovision import state_key
        run,attempt=map(int,owner.split(':'))
        path=f"repos/{c['repository']}/actions/runs/{run}"
        latest=self.github(path)
        prior=self.github(path+f'/attempts/{attempt}')
        jobs=self.github(path+f'/attempts/{attempt}/jobs?per_page=100')
        require(all(isinstance(r,dict) and r.get('id')==run and r.get('head_sha')==commit
                    and r.get('status')=='completed' and r.get('conclusion') in ('failure','cancelled')
                    for r in (latest,prior)) and prior.get('run_attempt')==attempt
                and isinstance(jobs,dict) and isinstance(jobs.get('jobs'),list) and jobs['jobs']
                and jobs.get('total_count')==len(jobs['jobs'])
                and all(j.get('status')=='completed' for j in jobs['jobs']),
                'CREATION_RESUME_WORKFLOW_UNVERIFIED')
        value=self.state_reader.get_object(Bucket=c['state_bucket'],Key=state_key(c),ExpectedBucketOwner=c['account'])
        body=value['Body']
        try:
            raw=body.read(1048577)
            require(isinstance(raw,bytes) and len(raw)<=1048576 and value.get('ContentLength')==len(raw)
                    and value.get('ServerSideEncryption')=='AES256'
                    and isinstance(value.get('VersionId'),str) and value['VersionId'] not in ('','null'),
                    'CREATION_RESUME_STATE_UNVERIFIED')
        finally:body.close()
        return {'workflow':{'repository':c['repository'],'run_id':run,'attempt':attempt,'commit':commit,
                            'status':'completed','conclusion':prior['conclusion']},
                'state':{'version_id':value['VersionId'],'sha256':hashlib.sha256(raw).hexdigest()}}


def observed_ssm_host_key(ssm,command_id,instance):
    """Read a completed administrative SSM observation; never send a command.

    The administrator invokes the public-key-only command against the reviewed
    instance. This evidence is SSM evidence, not reconstructed console output.
    """
    import json
    from scripts.restore.reprovision import validate_resume_host_key
    result=ssm.get_command_invocation(CommandId=command_id,InstanceId=instance)
    require(result.get('CommandId')==command_id and result.get('InstanceId')==instance
            and result.get('DocumentName')=='AWS-RunShellScript'
            and result.get('Status')=='Success' and result.get('ResponseCode')==0
            and not result.get('StandardErrorContent'), 'CREATION_SSM_HOST_KEY_UNVERIFIED')
    raw=result.get('StandardOutputContent')
    require(isinstance(raw,str) and len(raw)<=4096,'CREATION_SSM_HOST_KEY_UNVERIFIED')
    try:value=json.loads(raw)
    except (ValueError,TypeError):raise CaptureFailure('CREATION_SSM_HOST_KEY_UNVERIFIED') from None
    require(isinstance(value,dict) and value.get('instance')==instance
            and value.get('sshd_host_key_configured') is True,'CREATION_SSM_HOST_KEY_UNVERIFIED')
    proof={'instance':instance,'public_key':value.get('public_key'),'ssm_command_id':command_id}
    validate_resume_host_key(proof,instance)
    return proof


def issue(s3,sts,raw_configuration,owner,commit,observer=None,host_key_observation=None):
    c=configuration(raw_configuration)
    identity=sts.get_caller_identity()
    # STS assumed-role ARNs contain the role name, not the IAM role path.
    role=c['grant_issuer_arn'].rsplit('/',1)[1]
    require(identity.get('Account')==c['account'] and
            isinstance(identity.get('Arn'),str) and identity['Arn'].startswith(
                f"arn:aws:sts::{c['account']}:assumed-role/{role}/"),
            'CREATION_ADMINISTRATIVE_ISSUER_REQUIRED')
    lease=CreationLease(s3,c['account'],c['state_bucket'],
        f"control/reprovision/{c['account']}/{c['project']}/{c['github_environment']}/",owner,commit,c)
    lease.verify_authority()
    require(lease.versions(lease.grant_key)==[],'CREATION_GRANT_ALREADY_EXISTS')
    rows=lease.versions(lease.key)
    resume=None
    if rows:
        row=next(r for r in rows if r['IsLatest']);previous,_=lease.read(lease.key,row['VersionId'])
        if previous.get('host') is not None and previous.get('handoff_verified') is False:
            prior=CreationLease(s3,c['account'],c['state_bucket'],lease.prefix,
                                previous['owner'],previous['commit'],c)
            prior.authorize()
            require(prior.grant[1]==previous.get('grant_sha256'),'CREATION_RESUME_BINDING_CHANGED')
            require(observer is not None,'CREATION_RESUME_OBSERVATION_REQUIRED')
            try:observation=observer.collect(c,previous['owner'],previous['commit'])
            except Exception:raise CaptureFailure('CREATION_RESUME_OBSERVATION_FAILED') from None
            require(isinstance(observation,dict) and set(observation)=={'workflow','state'},
                    'CREATION_RESUME_OBSERVATION_FAILED')
            resume={'previous':{'version_id':row['VersionId'],'record':previous},**observation,
                    'creation_grant_sha256':prior.resume['creation_grant_sha256'] if prior.resume else prior.grant[1]}
            if host_key_observation is not None:resume['host_key']=host_key_observation
            from scripts.restore.reprovision import validate_resume_binding
            validate_resume_binding(resume,c)
            require(previous['owner']!=owner,'CREATION_GRANT_ALREADY_USED')
            require(lease.versions(lease.key)==rows,'CREATION_AUTHORITY_LOST')
        else:lease.previous_resolution(row,previous)
    grant={'version':1,'kind':'recovery-host-creation','owner':owner,'commit':commit,
           'configuration_sha256':digest(c),'configuration':c}
    require(host_key_observation is None or resume is not None,'CREATION_RESUME_HOST_KEY_INVALID')
    if resume is not None:grant['resume']=resume
    result=lease.call('put_object',Bucket=c['state_bucket'],Key=lease.grant_key,
                      Body=encoded(grant),ServerSideEncryption='AES256',IfNoneMatch='*')
    version=result.get('VersionId')
    require(isinstance(version,str) and version not in ('','null'),'CREATION_WRITE_UNCONFIRMED')
    # A concurrent/ambiguous write leaves history intact and denies creation.
    lease.authorize()
    require(lease.grant[0]==version,'CREATION_WRITE_UNCONFIRMED')
    return {'code':'CREATION_GRANT_ISSUED','owner':owner,'commit':commit,
            'version_id':version,'configuration_sha256':digest(c)}


def reconcile_before_apply(s3,sts,raw_configuration,owner,commit,expected,observer):
    """Administrative attestation after live reads, never a success/force-unlock.

    observer.collect returns workflow/API observations and the exact state bytes.
    Production uses the existing administrative identity; mocks are only tests.
    Only the protected receipt is written. Journal/history and grants stay intact.
    """
    import hashlib
    from scripts.backup.json_input import parse_json
    from scripts.backup.limits import Limits
    from scripts.restore.reprovision import retained_state
    c=configuration(raw_configuration);identity=sts.get_caller_identity()
    role=c['grant_issuer_arn'].rsplit('/',1)[1]
    require(identity.get('Account')==c['account'] and isinstance(identity.get('Arn'),str)
            and identity['Arn'].startswith(f"arn:aws:sts::{c['account']}:assumed-role/{role}/"),
            'CREATION_ADMINISTRATIVE_ISSUER_REQUIRED')
    require(isinstance(expected,dict) and set(expected)=={'lease_version_id','lease_sha256','state_version_id','state_sha256'},
            'CREATION_RECONCILIATION_APPROVAL_REQUIRED')
    lease=CreationLease(s3,c['account'],c['state_bucket'],
        f"control/reprovision/{c['account']}/{c['project']}/{c['github_environment']}/",owner,commit,c)
    lease.verify_authority();history=lease.versions(lease.key)
    require(history,'CREATION_RECONCILIATION_REQUIRED')
    row=next(r for r in history if r['IsLatest']);old,_=lease.read(lease.key,row['VersionId'])
    require(row['VersionId']==expected['lease_version_id'] and digest(old)==expected['lease_sha256']
            and old.get('owner')==owner and old.get('commit')==commit
            and old.get('phase')=='intervention_required' and old.get('host') is None
            and old.get('handoff_verified') is False,'CREATION_RECONCILIATION_BINDING_REJECTED')
    grant=lease.authorize();require(digest(grant)==old['grant_sha256'],'CREATION_RECONCILIATION_BINDING_REJECTED')
    try:observation=observer.collect(c,owner,commit)
    except Exception:raise CaptureFailure('CREATION_RECONCILIATION_OBSERVATION_FAILED') from None
    workflow={'repository':c['repository'],'run_id':int(owner.split(':')[0]),'attempt':int(owner.split(':')[1]),
              'commit':commit,'status':'completed','conclusion':'failure'}
    require(isinstance(observation,dict) and set(observation)=={'account','workflow','runtime','state'}
            and observation['account']==c['account'] and observation['workflow']==workflow,
            'CREATION_RECONCILIATION_WORKFLOW_UNVERIFIED')
    runtime=observation['runtime']
    require(isinstance(runtime,dict) and set(runtime)=={'instances','vpcs','subnets','internet_gateways',
        'route_tables','security_groups','key_pairs','network_interfaces','volumes','host_role','host_profile'}
        and all(type(value) is list and value==[] for value in runtime.values()),
        'CREATION_RECONCILIATION_RUNTIME_PRESENT')
    snapshot=observation['state']
    require(isinstance(snapshot,dict) and set(snapshot)=={'version_id','bytes'}
            and snapshot['version_id']==expected['state_version_id'] and isinstance(snapshot['bytes'],bytes)
            and len(snapshot['bytes'])<=1048576
            and hashlib.sha256(snapshot['bytes']).hexdigest()==expected['state_sha256'],
            'CREATION_RECONCILIATION_STATE_CHANGED')
    document=parse_json(snapshot['bytes'],Limits(json_bytes=1048576))
    require(retained_state(document,c) is not None,'CREATION_RECONCILIATION_STATE_CHANGED')
    key=lease.prefix+'grants/reconciliations/'+owner.replace(':','-')+'.json'
    require(lease.versions(key)==[],'CREATION_RECONCILIATION_ALREADY_EXISTS')
    receipt={'version':1,'kind':'administrative-creation-abort-before-apply',
        'binding':{'owner':owner,'commit':commit,'lease_version_id':row['VersionId'],'lease_sha256':digest(old),
                   'configuration_sha256':digest(c)},
        'state':{'version_id':snapshot['version_id'],'sha256':expected['state_sha256'],
                 'lineage':document['lineage'],'serial':document['serial']},
        'workflow':workflow,'absence_sha256':digest(runtime)}
    require(lease.versions(lease.key)==history,'CREATION_AUTHORITY_LOST')
    actual,_=lease.read(lease.key,row['VersionId']);require(actual==old,'CREATION_AUTHORITY_LOST')
    result=lease.call('put_object',Bucket=c['state_bucket'],Key=key,Body=encoded(receipt),ServerSideEncryption='AES256',IfNoneMatch='*')
    version=result.get('VersionId');require(isinstance(version,str) and version not in ('','null'),'CREATION_WRITE_UNCONFIRMED')
    rows=lease.versions(key);require(len(rows)==1 and rows[0]['VersionId']==version and rows[0]['IsLatest'],'CREATION_WRITE_UNCONFIRMED')
    actual,_=lease.read(key,version);require(actual==receipt,'CREATION_WRITE_UNCONFIRMED')
    require(lease.versions(lease.key)==history,'CREATION_AUTHORITY_LOST')
    return {'code':'CREATION_ABORT_BEFORE_APPLY_RECONCILED','owner':owner,'receipt_version_id':version,
            'journal_unchanged':True,'handoff_verified':False,'backup_complete':False,'restore_proven':False}
