"""Manual, grant-gated entrypoint for the existing deploy workflow."""
import json
import os
from pathlib import Path
import re
import sys
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import require,json_bytes
from scripts.restore.creation_lease import CreationLease,digest
from scripts.restore.reprovision import configuration,execute


def approved_inputs():
    from scripts.ci import deploy as ci
    key=ci.required('SSH_PUBLIC_KEY');restore_key=ci.required('RESTORE_SSH_PUBLIC_KEY')
    require(all(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\r\n]+)?',k) for k in (key,restore_key)),
            'CREATION_PUBLIC_KEY_REJECTED')
    import ipaddress
    ssh=json.loads(ci.required('SSH_ALLOWED_CIDRS'));http=json.loads(ci.required('HTTP_ALLOWED_CIDRS'))
    require(isinstance(ssh,list) and isinstance(http,list) and ssh and http,'CREATION_CIDRS_REQUIRED')
    for rows,minimum in ((ssh,24),(http,1)):
        for row in rows:require(ipaddress.IPv4Network(row,strict=True).prefixlen>=minimum,'CREATION_CIDR_REJECTED')
    require(os.environ.get('INSTANCE_TYPE','t3.medium') in ('','t3.medium')
            and os.environ.get('ROOT_VOLUME_SIZE_GIB','30') in ('','30'),'CREATION_SIZING_REJECTED')
    return {'ssh_public_key':key,'restore_public_key':restore_key,'ssh_cidrs':sorted(ssh),'http_cidrs':sorted(http),
            'instance_type':'t3.medium','root_volume_size_gib':30}


def validate_context():
    from scripts.ci import deploy as ci
    account,region,project,environment,buckets=ci.context()
    require(os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and os.environ.get('DEPLOY_PURPOSE')=='create_restore_target'
            and os.environ.get('GITHUB_REF')=='refs/heads/main','CREATION_MANUAL_CONTEXT_REQUIRED')
    c=configuration(ci.required('REPROVISION_CONFIGURATION'))
    require((c['account'],c['region'],c['project'],c['github_environment'])==(account,region,project,environment)
            and [c[k] for k in ('source_data_bucket','backups_bucket','state_bucket')]==buckets
            and c['repository']==ci.required('GITHUB_REPOSITORY')
            and c['terraform_inputs_sha256']==digest(approved_inputs()),'CREATION_CONTEXT_MISMATCH')
    require(ci.required('REPROVISION_ROLE_ARN')==f'arn:aws:iam::{account}:role/{project}-{environment}-reprovision',
            'CREATION_DEDICATED_ROLE_REQUIRED')
    # Validate the actual reviewed pins, not just the supplied digest syntax.
    from scripts.restore.deployment import validate_preparation
    from scripts.restore.reprovision import host_configuration
    h=host_configuration(c,'i-'+'0'*17)
    validate_preparation(json.dumps(h),account,region,project,
                         [c['data_bucket'],c['backups_bucket'],c['state_bucket']],h['instance'],ci.ROOT)
    return c


def run():
    from scripts.ci import deploy as ci
    c=validate_context()
    require(sys.platform=='linux' and sys.version_info[:2]==(3,12) and ci.required('DEPLOY_MODE')=='apply'
            and ci.required('CONFIRMATION')=='APPLY','CREATION_OPERATION_NOT_AUTHORIZED')
    owner=ci.required('GITHUB_RUN_ID',r'[0-9]{1,20}')+':'+ci.required('GITHUB_RUN_ATTEMPT',r'[0-9]{1,4}')
    commit=ci.required('GITHUB_SHA',r'[a-f0-9]{40}')
    from scripts.backup.operational.ssm import Target
    from scripts.backup.operational.oidc import assume_oidc
    from scripts.backup.operational.host_worker import credentials
    from scripts.backup.operational.sdk import clients
    from scripts.restore.reprovision_operations import ReprovisionOperations
    target=Target(c['account'],c['region'],c['source_instance'],c['project']+'-'+c['github_environment']+'-reprovision')
    session=assume_oidc(ci.required('REPROVISION_ROLE_ARN'),target,'nc-create-'+owner.replace(':','-'))
    previous=dict(os.environ)
    try:
        sdk=clients(credentials(session),target)
        arn=sdk['sts'].get_caller_identity()['Arn']
        require(arn.startswith(f"arn:aws:sts::{c['account']}:assumed-role/{target.role_name}/"),'CREATION_IDENTITY_REJECTED')
        # CLI subprocesses receive only the ephemeral session through env, never
        # argv/files. No configure-credentials, persistent profile or logging.
        os.environ.update(AWS_ACCESS_KEY_ID=session['access_key'],AWS_SECRET_ACCESS_KEY=session['secret_key'],
                          AWS_SESSION_TOKEN=session['token'],AWS_DEFAULT_REGION=c['region'])
        for key in ('AWS_PROFILE','AWS_DEFAULT_PROFILE','AWS_ENDPOINT_URL'):os.environ.pop(key,None)
        prefix=f"control/reprovision/{c['account']}/{c['project']}/{c['github_environment']}/"
        lease=CreationLease(sdk['s3'],c['account'],c['state_bucket'],prefix,owner,commit,c)
        operations=ReprovisionOperations(sdk,ci,c,owner,commit)
        # CLI connect/cleanup bind to the new resource environment only after
        # context/grant validation. Normal existing-host deploy is unchanged.
        os.environ.update(DEPLOY_ENVIRONMENT=c['destination_environment'],DATA_BUCKET_NAME=c['data_bucket'])
        try:return execute(lease,operations,wait_seconds=300)
        finally:
            if (ci.paths()/'runtime.json').exists():ci.cleanup()
            for name in ('restore-creation.tfplan','restore-creation.tfvars.json','restore-creation.tfbackend',
                         'creation-prepare.json','created-target.json'):(ci.paths()/name).unlink(missing_ok=True)
    finally:
        session.clear();os.environ.clear();os.environ.update(previous)


def main():
    try:
        require(len(sys.argv)==1,'CREATION_ARGUMENTS_REJECTED')
        sys.stdout.buffer.write(json_bytes(run())+b'\n')
    except BaseException as e:
        code=e.code if isinstance(e,CaptureFailure) and re.fullmatch('[A-Z0-9_]{1,96}',str(e.code)) else 'CREATION_INTERVENTION_REQUIRED'
        sys.stdout.buffer.write(json_bytes({'code':code,'backup_complete':False,'restore_proven':False,'destroy_authorized':False})+b'\n')
        raise SystemExit(2) from None


if __name__=='__main__':main()
