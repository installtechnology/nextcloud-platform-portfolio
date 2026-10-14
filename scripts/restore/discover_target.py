"""Explicit discovery of a prepared recovery host, never a stale-ID fallback."""
import base64
import hashlib
import re
from scripts.backup.sealing.common import require
from scripts.backup.operational.ssm import Target


def discover(ec2,account,region,project,environment,source,commit,role_name):
    response=ec2.describe_instances(Filters=[{'Name':'tag:Project','Values':[project]},
        {'Name':'tag:Environment','Values':[environment]},
        {'Name':'tag:RecoveryPreparedCommit','Values':[commit]},
        {'Name':'instance-state-name','Values':['running']}])
    require('NextToken' not in response and isinstance(response.get('Reservations'),list),
            'RESTORE_DISCOVERY_AMBIGUOUS')
    nodes=[n for r in response['Reservations'] if r.get('OwnerId')==account for n in r.get('Instances',[])]
    require(len(nodes)==1,'RESTORE_DISCOVERY_AMBIGUOUS')
    node=nodes[0];instance=node.get('InstanceId')
    require(isinstance(instance,str) and re.fullmatch('i-[a-f0-9]{17}',instance) and instance!=source,
            'RESTORE_SOURCE_INSTANCE_REJECTED')
    tags={t['Key']:t['Value'] for t in node.get('Tags',[])}
    require(tags.get('Project')==project and tags.get('Environment')==environment
            and tags.get('RecoveryPreparedCommit')==commit and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',tags.get('RecoveryCreationOwner',''))
            and node.get('State',{}).get('Name')=='running','RESTORE_PREPARED_TARGET_REJECTED')
    public=tags.get('RecoveryHostKey','').split()
    require(len(public)==2 and public[0]=='ssh-ed25519' and re.fullmatch('[A-Za-z0-9+/]+={0,2}',public[1]),
            'RESTORE_INDEPENDENT_HOST_KEY_REQUIRED')
    console=ec2.get_console_output(InstanceId=instance,Latest=True).get('Output','')
    require(isinstance(console,str) and len(console)<=256*1024,'RESTORE_CONSOLE_UNVERIFIED')
    if 'BEGIN SSH HOST KEY' not in console:
        try:console=base64.b64decode(console,validate=True).decode()
        except Exception:require(False,'RESTORE_CONSOLE_UNVERIFIED')
    from scripts.ci.deploy import console_fingerprints
    fp='SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(public[1],validate=True)).digest()).decode().rstrip('=')
    require(fp in console_fingerprints(console),'RESTORE_INDEPENDENT_HOST_KEY_REQUIRED')
    return Target(account,region,instance,role_name),'ncbackup-'+instance+' '+' '.join(public)
