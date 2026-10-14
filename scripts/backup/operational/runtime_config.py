"""Root-owned activation configuration, not an operational_allowed switch."""
import os
from pathlib import Path
import re
import stat
from .capture_backend import CaptureTarget
from .fence_policy import FenceScope,require
from .ssm import Target
from ..json_input import parse_json
from ..limits import Limits

FIELDS={'version','account','region','instance','project','environment','data_bucket','backups_bucket','state_bucket',
        'installation_identity','deployment_root','staging_root','images','admin_role','emergency_role',
        'backup_role','guard_role','storage_encryption','repository'}


def validate(value):
    require(isinstance(value,dict) and set(value)==FIELDS and type(value['version']) is int and value['version']==2,
            'HOST_CONFIGURATION_INVALID')
    target=CaptureTarget(value['account'],value['region'],value['instance'],value['project'],value['data_bucket'],
                         value['installation_identity'],value['deployment_root'],value['images'])
    require(re.fullmatch(r'[a-z][a-z0-9-]{0,62}',value['environment'])
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',value['backups_bucket'])
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',value['state_bucket'])
            and len({value['data_bucket'],value['backups_bucket'],value['state_bucket']})==3,'HOST_CONFIGURATION_INVALID')
    base=f"arn:aws:iam::{value['account']}:role/{value['project']}-{value['environment']}"
    require(value['backup_role']==base+'-backup' and value['guard_role']==base+'-deploy-guard'
            and value['admin_role']==base+'-data-policy-admin', 'HOST_ROLE_BINDING_INVALID')
    FenceScope(value['account'],value['data_bucket'],value['admin_role'],value['emergency_role'])
    require(value['storage_encryption']=='SSE-S3'
            and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repository']), 'HOST_CRYPTO_CONFIGURATION_INVALID')
    require(value['staging_root']=='/var/lib/nextcloud-recovery', 'HOST_PRIVATE_PATH_REJECTED')
    return target


def load(path='/etc/nextcloud-recovery/config.json'):
    require(os.name=='posix' and os.geteuid()==0, 'LINUX_ROOT_REQUIRED')
    path=Path(path)
    require(str(path)=='/etc/nextcloud-recovery/config.json', 'HOST_CONFIGURATION_PATH_REJECTED')
    for p in (path,*path.parents):require(not p.is_symlink(), 'HOST_CONFIGURATION_UNSAFE')
    info=path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o600,
            'HOST_CONFIGURATION_UNSAFE')
    with path.open('rb') as stream:
        value=parse_json(stream.read(65537),Limits(json_bytes=65536))
        from .ssh_channel import stable_stat
        require(stable_stat(os.fstat(stream.fileno()))==stable_stat(info), 'HOST_CONFIGURATION_CHANGED')
    validate(value);return value
