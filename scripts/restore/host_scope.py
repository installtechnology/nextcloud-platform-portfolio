"""Separate restore-host scope; no activation through simulation/ready flags."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.operational.host_probe import imds_identity
from scripts.backup.sealing.common import require
from scripts.backup.safe_files import SafeRoot,signature


FIELDS={'version','account','region','instance','source_instance','project','source_data_bucket',
        'data_bucket','backups_bucket','state_bucket','staging_root','deployment_root','images'}


@dataclass(frozen=True,repr=False)
class RestoreHost:
    config: dict

    def identity(self):
        require(os.name=='posix' and os.geteuid()==0,'RESTORE_LINUX_ROOT_REQUIRED')
        observed=imds_identity()
        expected={k:self.config[k] for k in ('account','region','instance')}
        require(observed==expected and observed['instance']!=self.config['source_instance'],
                'RESTORE_HOST_IDENTITY_MISMATCH')
        return observed


def validate(config):
    require(isinstance(config,dict) and set(config)==FIELDS and type(config['version']) is int
            and config['version']==1,'RESTORE_HOST_CONFIG_INVALID')
    require(all(isinstance(config[k],str) and re.fullmatch(r'i-[a-f0-9]{17}',config[k])
                for k in ('instance','source_instance')) and config['instance']!=config['source_instance'],
            'RESTORE_HOST_SOURCE_REJECTED')
    require(isinstance(config['account'],str) and re.fullmatch('[0-9]{12}',config['account'])
            and isinstance(config['region'],str) and re.fullmatch('[a-z]{2}(?:-[a-z]+)+-[0-9]+',config['region'])
            and isinstance(config['project'],str) and re.fullmatch('[a-z][a-z0-9-]{1,62}',config['project']),
            'RESTORE_HOST_SCOPE_INVALID')
    buckets=[config[k] for k in ('source_data_bucket','data_bucket','backups_bucket','state_bucket')]
    require(len(set(buckets))==4 and all(isinstance(b,str) and re.fullmatch('[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',b)
                                       for b in buckets),'RESTORE_HOST_STORAGE_SEPARATION')
    require(config['staging_root']=='/var/lib/nextcloud-restore' and
            config['deployment_root']=='/opt/nextcloud-platform','RESTORE_HOST_PATH_REJECTED')
    require(isinstance(config['images'],dict) and set(config['images'])=={'nextcloud','postgres','redis'}
            and all(isinstance(v,str) and re.fullmatch('[^\\s]+@sha256:[a-f0-9]{64}',v)
                    for v in config['images'].values()),'RESTORE_HOST_IMAGE_PINS_REQUIRED')
    return RestoreHost(config.copy())


def validate_shared_preparation(host_config, guard):
    """Validate the unchanged PR7 lease binding before installing either config."""
    from scripts.backup.operational.runtime_config import validate as validate_guard
    host = validate(host_config).config
    validate_guard(guard)
    require(all(guard[key] == host[key] for key in
                ('account', 'region', 'instance', 'project', 'data_bucket', 'backups_bucket', 'state_bucket'))
            and guard['images'] == host['images']
            and guard['deployment_root'] == host['deployment_root'],
            'RESTORE_SHARED_GUARD_SCOPE_MISMATCH')
    return guard


def load():
    require(os.name=='posix' and os.geteuid()==0,'RESTORE_LINUX_ROOT_REQUIRED')
    directory=Path('/etc/nextcloud-restore')
    parent=directory.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid==parent.st_gid==0
            and stat.S_IMODE(parent.st_mode)==0o700,'RESTORE_HOST_CONFIG_UNSAFE')
    with SafeRoot(directory) as safe,safe.open('config.json') as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1
                and stat.S_IMODE(info.st_mode)==0o600,'RESTORE_HOST_CONFIG_UNSAFE')
        before=signature(stream);raw=stream.read(65537)
        require(before==signature(stream),'RESTORE_HOST_CONFIG_CHANGED')
        config=parse_json(raw,Limits(json_bytes=65536));safe.assert_unchanged()
    host=validate(config);host.identity();return host
