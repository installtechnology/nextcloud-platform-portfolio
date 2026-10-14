"""Manual restore controller; ephemeral OIDC, dedicated role/key, no plaintext backup."""
import os
from pathlib import Path
import re
import sys
import tempfile
from scripts.backup.sealing.common import require,backup_id,json_bytes
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.operational.ssm import Target,SSMChannel
from scripts.backup.operational.oidc import assume_oidc
from scripts.backup.operational.sdk import clients,validate_ec2
from scripts.backup.operational.host_worker import credentials
from scripts.backup.operational.ssh_channel import verified_plugin
from scripts.restore.channel import RestoreSSHChannel


def configuration():
    require(sys.platform=='linux' and sys.version_info[:2]==(3,12),'RESTORE_RUNNER_PLATFORM_REJECTED')
    def value(name,pattern):
        v=os.environ.get(name,'');require(re.fullmatch(pattern,v) is not None,'RESTORE_RUNNER_CONFIG_REJECTED');return v
    require(os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and os.environ.get('GITHUB_REF')=='refs/heads/main'
            and os.environ.get('CONFIRMATION')=='RESTORE','RESTORE_MANUAL_CONFIRMATION_REQUIRED')
    account=value('AWS_ACCOUNT_ID','[0-9]{12}');region=value('AWS_REGION','[a-z]{2}(?:-[a-z]+)+-[0-9]+')
    project=value('PROJECT_NAME','[a-z][a-z0-9-]{1,24}');env=value('RESTORE_GITHUB_ENVIRONMENT','[a-z][a-z0-9-]{0,62}')
    resource_env=value('RESTORE_RESOURCE_ENVIRONMENT','[a-z][a-z0-9-]{0,62}')
    role=f'arn:aws:iam::{account}:role/{project}-{env}-restore'
    require(os.environ.get('RESTORE_ROLE_ARN')==role,'RESTORE_DEDICATED_ROLE_REQUIRED')
    selection=os.environ.get('RESTORE_TARGET_SELECTION','configured')
    require(selection in ('configured','discover_prepared'),'RESTORE_TARGET_SELECTION_REJECTED')
    source=value('RESTORE_SOURCE_INSTANCE_ID','i-[a-f0-9]{17}')
    # The source ID is an OIDC/client bootstrap scope only in discovery mode;
    # no source session or request is sent before replacing it with the target.
    instance=source if selection=='discover_prepared' else value('RESTORE_INSTANCE_ID','i-[a-f0-9]{17}')
    require(selection=='discover_prepared' or instance!=source,'RESTORE_SOURCE_INSTANCE_REJECTED')
    mode=os.environ.get('RESTORE_SELECTION','');bid=os.environ.get('RESTORE_BACKUP_ID','')
    require(mode in ('specific','latest_valid'),'RESTORE_SELECTION_REJECTED')
    if mode=='specific':backup_id(bid)
    else:require(bid=='','RESTORE_SELECTION_AMBIGUOUS')
    owner=value('GITHUB_RUN_ID','[0-9]{1,20}')+':'+value('GITHUB_RUN_ATTEMPT','[0-9]{1,4}')
    repository=value('GITHUB_REPOSITORY','[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+');commit=value('GITHUB_SHA','[a-f0-9]{40}')
    target=Target(account,region,instance,f'{project}-{env}-restore')
    return target,role,{'version':1,'owner':owner,'repository':repository,'commit':commit,'confirmation':'RESTORE',
        'github_environment':env,'resource_environment':resource_env,'selection':mode,'backup_id':bid,
        'scope':{'account':account,'region':region,'instance':instance,'source_instance':source,'project':project}}


def restore():
    target,role,request=configuration()
    alias='ncbackup-'+target.instance;known=os.environ.get('RESTORE_KNOWN_HOSTS','');key=os.environ.get('RESTORE_SSH_PRIVATE_KEY','')
    discover=os.environ.get('RESTORE_TARGET_SELECTION','configured')=='discover_prepared'
    if not discover:
        require(len(known.splitlines())==1 and len(known.split())==3 and known.split()[:2]==[alias,'ssh-ed25519']
                and re.fullmatch('[A-Za-z0-9+/]+={0,2}',known.split()[2]),'RESTORE_INDEPENDENT_HOST_KEY_REQUIRED')
    require(key.startswith('-----BEGIN OPENSSH PRIVATE KEY-----') and len(key)<=16384,'RESTORE_SSH_KEY_REQUIRED')
    plugin=verified_plugin(os.environ.get('SSM_PLUGIN_PATH',''),os.environ.get('SSM_PLUGIN_SHA256',''))
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    session=assume_oidc(role,target,'nc-restore-'+request['owner'].replace(':','-'))
    try:
        sdk=clients(credentials(session),target)
        if discover:
            from scripts.restore.discover_target import discover as discover_target
            target,known=discover_target(sdk['ec2'],target.account,target.region,request['scope']['project'],
                request['resource_environment'],request['scope']['source_instance'],request['commit'],target.role_name)
            alias='ncbackup-'+target.instance;request['scope']['instance']=target.instance
        validate_ec2(sdk['ec2'],target,request['scope']['project'],request['resource_environment'])
        node=sdk['ec2'].describe_instances(InstanceIds=[target.instance])['Reservations'][0]['Instances'][0]
        require(node['InstanceId']==target.instance,'RESTORE_TARGET_CHANGED');request['public_ip']=node['PublicIpAddress']
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix='nc-restore-') as directory:
            root=Path(directory)
            for name,data in (('known_hosts',known.strip()+'\n'),('ssh-key',key.replace('\r\n','\n').strip()+'\n')):
                with (root/name).open('x',encoding='utf-8') as stream:stream.write(data)
                os.chmod(root/name,0o600)
            channel=RestoreSSHChannel(SSMChannel(sdk['ssm'],sdk['sts'],target),plugin,root/'known_hosts',root/'ssh-key',alias)
            request['restore_credentials']=session
            with channel.open():result=validate_result(channel.request(request))
        return result
    finally:session.clear()


def validate_result(result):
    require(isinstance(result,dict) and result.get('restore_proven') is False and result.get('destroy_authorized') is False,
            'RESTORE_REMOTE_RESULT_REJECTED')
    if set(result)=={'code','restore_proven','destroy_authorized'}:
        code=result.get('code')
        require(isinstance(code,str) and re.fullmatch('(?:RESTORE|S3|SESSION|POSTGRES|EXEC|DEPLOY|CAPTURE|HOST|UNSAFE|PROCESS|PINNED)_[A-Z0-9_]{1,80}',code),
                'RESTORE_REMOTE_RESULT_REJECTED')
        raise CaptureFailure(code)
    require(set(result)=={'code','operation_id','backup_id','restore_proven','destroy_authorized'}
            and result['code']=='RESTORE_CANDIDATE_READY','RESTORE_REMOTE_RESULT_REJECTED')
    backup_id(result['operation_id']);backup_id(result['backup_id']);return result


def main():
    try:
        require(len(sys.argv)==1,'RESTORE_RUNNER_ARGUMENTS_REJECTED');result=restore()
        sys.stdout.buffer.write(json_bytes(result)+b'\n')
    except BaseException as error:
        code=error.code if isinstance(error,CaptureFailure) and re.fullmatch('[A-Z0-9_]{1,96}',error.code) else 'RESTORE_INTERVENTION_REQUIRED'
        sys.stdout.buffer.write(json_bytes({'code':code,'restore_proven':False,'destroy_authorized':False})+b'\n')
        raise SystemExit(2) from None


if __name__=='__main__':main()
