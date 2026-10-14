"""Administrative restore binding. No credentials and no activation flag."""
import os
from pathlib import Path
import re
import stat
from scripts.backup.safe_files import SafeRoot,signature
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import require

FIELDS={'version','repository','branch','github_environment','resource_environment','role_arn','source'}
SOURCE={'account_id','project','environment','installation_identity_sha256','data_bucket','region'}


def validate_access(value,host):
    require(isinstance(value,dict) and set(value)==FIELDS and type(value['version']) is int and value['version']==1,
            'RESTORE_ACCESS_CONFIG_REJECTED')
    require(value['branch']=='main' and isinstance(value['repository'],str)
            and re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repository']),
            'RESTORE_ACCESS_REPOSITORY_REJECTED')
    for key in ('github_environment','resource_environment'):
        require(isinstance(value[key],str) and re.fullmatch('[a-z][a-z0-9-]{0,62}',value[key]),
                'RESTORE_ACCESS_ENVIRONMENT_REJECTED')
    role=f"arn:aws:iam::{host['account']}:role/{host['project']}-{value['github_environment']}-restore"
    require(value['role_arn']==role,'RESTORE_DEDICATED_ROLE_REQUIRED')
    s=value['source']
    require(isinstance(s,dict) and set(s)==SOURCE and s['account_id']==host['account'] and s['region']==host['region']
            and s['project']==host['project'] and s['data_bucket']==host['source_data_bucket']
            and isinstance(s['environment'],str) and re.fullmatch('[a-z][a-z0-9-]{0,62}',s['environment'])
            and isinstance(s['installation_identity_sha256'],str)
            and re.fullmatch('[a-f0-9]{64}',s['installation_identity_sha256']),
            'RESTORE_ACCESS_SOURCE_REJECTED')
    return value


def load_access(host):
    require(os.name=='posix' and os.geteuid()==0,'RESTORE_LINUX_ROOT_REQUIRED')
    root=Path('/etc/nextcloud-restore')
    with SafeRoot(root) as safe,safe.open('access.json') as stream:
        info=os.fstat(stream.fileno());before=signature(stream)
        require(info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,'RESTORE_ACCESS_FILE_UNSAFE')
        value=parse_json(stream.read(65537),Limits(json_bytes=65536))
        require(signature(stream)==before,'RESTORE_ACCESS_FILE_CHANGED');safe.assert_unchanged()
    return validate_access(value,host)
