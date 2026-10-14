"""Manual runner controller. Backup and deploy guard use different OIDC roles.

No administrative credentials, plaintext capture, shell commands or artifacts.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import tempfile
from .fence_policy import require
from .sdk import clients
from .ssm import Target,SSMChannel
from .ssh_channel import SSHChannel,verified_plugin
from .remote_lease import RemoteDeployLease
from .oidc import assume_oidc
from .host_worker import credentials
from ..capture.storage import CaptureFailure
from ..sealing.common import json_bytes


def configuration(mode):
    require(mode in ('backup','deploy'),'RUNNER_MODE_REJECTED')
    require(sys.platform=='linux' and sys.version_info[:2]==(3,12),'CPYTHON312_LINUX_REQUIRED')
    def value(name,pattern):
        found=os.environ.get(name,'');require(re.fullmatch(pattern,found) is not None,'RUNNER_CONFIGURATION_INVALID');return found
    branch=value('DEPLOY_BRANCH',r'[A-Za-z0-9_./-]{1,128}')
    require(os.environ.get('GITHUB_REF')=='refs/heads/'+branch
            and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch','MANUAL_BRANCH_REQUIRED')
    environment=value('DEPLOY_ENVIRONMENT',r'[a-z][a-z0-9-]{0,62}')
    project=value('PROJECT_NAME',r'[a-z][a-z0-9-]{0,62}')
    role=value('BACKUP_ROLE_ARN' if mode=='backup' else 'DEPLOY_GUARD_ROLE_ARN',r'arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}')
    account=value('AWS_ACCOUNT_ID',r'[0-9]{12}')
    expected=f'{project}-{environment}-'+('backup' if mode=='backup' else 'deploy-guard')
    require(role==f'arn:aws:iam::{account}:role/{expected}','DEDICATED_ROLE_REQUIRED')
    target=Target(account,value('AWS_REGION',r'[a-z]{2}(?:-[a-z]+)+-[0-9]+'),
                  value('RECOVERY_INSTANCE_ID',r'i-[a-f0-9]{17}'),expected)
    scope={'project':project,'environment':environment,
        **{k:value(v,r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]') for k,v in
           (('data_bucket','DATA_BUCKET_NAME'),('backups_bucket','BACKUPS_BUCKET_NAME'),('state_bucket','STATE_BUCKET_NAME'))}}
    require(len({scope[k] for k in ('data_bucket','backups_bucket','state_bucket')})==3,'STORAGE_SEPARATION')
    owner=value('GITHUB_RUN_ID',r'[0-9]{1,20}')+':'+value('GITHUB_RUN_ATTEMPT',r'[0-9]{1,4}')
    require(os.environ.get('CONFIRMATION')==('BACKUP' if mode=='backup' else 'APPLY'),'EXPLICIT_CONFIRMATION_REQUIRED')
    return target,scope,owner,role


@contextmanager
def connection(mode):
    target,scope,owner,role=configuration(mode)
    session=assume_oidc(role,target,'nc-'+mode+'-'+owner.replace(':','-'))
    sdk=clients(credentials(session),target)
    from .sdk import validate_ec2
    validate_ec2(sdk['ec2'],target,scope['project'],scope['environment'])
    plugin=verified_plugin(os.environ.get('SSM_PLUGIN_PATH',''),os.environ.get('SSM_PLUGIN_SHA256',''))
    # Inputs are a separately provisioned non-administrative SSH key and a host
    # key independently verified through an authenticated administrative channel.
    alias='ncbackup-'+target.instance
    known=os.environ.get('RECOVERY_KNOWN_HOSTS','')
    require(len(known.splitlines())==1 and len(known.split())==3 and known.split()[:2]==[alias,'ssh-ed25519']
            and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}',known.split()[2]),'INDEPENDENT_HOST_KEY_REQUIRED')
    key=os.environ.get('RECOVERY_SSH_PRIVATE_KEY','')
    require(len(key)<=16384 and key.startswith('-----BEGIN OPENSSH PRIVATE KEY-----'),'DEDICATED_SSH_KEY_REQUIRED')
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='nc-recovery-') as directory:
        base=Path(directory);os.chmod(base,0o700)
        for name,data in (('known_hosts',known.strip()+'\n'),('ssh-key',key.replace('\r\n','\n').strip()+'\n')):
            with (base/name).open('x',encoding='utf-8') as stream:stream.write(data)
            os.chmod(base/name,0o600)
        channel=SSHChannel(SSMChannel(sdk['ssm'],sdk['sts'],target),plugin,base/'known_hosts',base/'ssh-key',alias)
        try:
            with channel.open('backup' if mode=='backup' else 'lease'):
                yield channel,target,scope,owner,session
        finally:
            session.clear()
            # This file contains an SSH key, never AWS credentials or an age
            # identity. Runner cleanup is not recovery of the remote operation.
            (base/'ssh-key').unlink(missing_ok=True)


def backup():
    with connection('backup') as (channel,target,scope,owner,session):
        request={'version':1,'owner':owner,'commit':os.environ.get('GITHUB_SHA',''),
            'repository':os.environ.get('GITHUB_REPOSITORY',''),'confirmation':'BACKUP',
            'scope':{'account':target.account,'region':target.region,'instance':target.instance,
                     **{k:v for k,v in scope.items() if k!='state_bucket'}},'backup_credentials':session}
        result=channel.request(request)
        require(isinstance(result,dict) and result.get('code')=='BACKUP_INTEGRITY_VERIFIED'
                and result.get('backup_complete') is True and result.get('restore_proven') is False
                and result.get('destroy_authorized') is False,'REMOTE_BACKUP_NOT_COMPLETED')
        from ..sealing.common import backup_id
        bid=backup_id(result.get('backup_id'))
        # No raw remote diagnostic is passed through to GitHub.
        return {'code':'BACKUP_INTEGRITY_VERIFIED','backup_id':bid,
                'backup_complete':True,'restore_proven':False,'destroy_authorized':False}


def deploy():
    from scripts.ci.deploy import guarded_deploy
    with connection('deploy') as (channel,target,scope,owner,session):
        guarded_deploy(RemoteDeployLease(channel,target,owner,scope,session))
    return {'code':'GUARDED_DEPLOY_FINISHED'}


def main():
    try:
        require(len(sys.argv)==2 and sys.argv[1] in ('backup','deploy'),'RUNNER_MODE_REJECTED')
        result=backup() if sys.argv[1]=='backup' else deploy()
        sys.stdout.buffer.write(json_bytes(result)+b'\n')
    except BaseException as error:
        code=error.code if isinstance(error,CaptureFailure) and re.fullmatch(r'[A-Z0-9_]{1,96}',error.code) else 'OPERATION_INTERVENTION_REQUIRED'
        sys.stdout.buffer.write(json_bytes({'code':code,'backup_complete':False,'restore_proven':False,'destroy_authorized':False})+b'\n')
        raise SystemExit(2) from None


if __name__=='__main__':main()
