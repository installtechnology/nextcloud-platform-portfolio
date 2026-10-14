"""Bounded parsing of the protected PR7 parameters artifact. Never execute SQL/PHP."""
import base64
from dataclasses import dataclass,field
import re
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import SealFailure,require

SECRETS={'postgres_admin_password','postgres_password','redis_password','nextcloud_admin_password'}
RUNTIME={'compose.yml','postgres-init.sh','redis-entrypoint.sh','platform.config.php','storage-guard.php',
         'install-nextcloud.php','configure-nextcloud.sh','nextcloud-healthcheck.php','nextcloud-integration.php'}


@dataclass(repr=False)
class ProtectedParameters:
    database: dict=field(repr=False)
    globals_sql: str=field(repr=False)
    secrets: dict=field(repr=False)
    compose_env: bytes=field(repr=False)
    runtime_code: dict=field(repr=False)


def decode(value,limit):
    require(isinstance(value,str) and len(value)<=4*((limit+2)//3),'RESTORE_PARAMETER_LIMIT')
    try:raw=base64.b64decode(value,validate=True)
    except (ValueError,TypeError):raise SealFailure('RESTORE_PARAMETER_ENCODING') from None
    require(0<len(raw)<=limit and base64.b64encode(raw).decode()==value,'RESTORE_PARAMETER_ENCODING')
    return raw


def load_parameters(raw,manifest):
    # Runtime code is <=1 MiB per file and base64 encoded in the authenticated
    # artifact. The default 4096-char journal limit is intentionally inapplicable.
    p=parse_json(raw,Limits(json_bytes=8*1024**2,string_chars=4*((1024**2+2)//3)))
    expected={'internal_version','kind','database','globals_sql_without_passwords','images','capture_binding',
              'secrets_base64','compose_env_base64','runtime_code_base64','recovery_requirement','restore_proven'}
    require(isinstance(p,dict) and set(p)==expected and type(p['internal_version']) is int
            and p['internal_version']==1 and p['kind']=='local-recovery-parameters'
            and p['restore_proven'] is False,'RESTORE_PARAMETERS_INVALID')
    c=manifest['compatibility'];db=p['database']
    require(isinstance(db,dict) and db.get('database')==c['database_name'] and db.get('owner')==c['database_owner']
            and isinstance(db.get('extensions'),list),'RESTORE_DATABASE_PARAMETERS_MISMATCH')
    binding=p['capture_binding']
    require(binding=={'operation_id':manifest['backup_id'],'consistency_id':manifest['consistency']['id']},
            'RESTORE_CAPTURE_BINDING_MISMATCH')
    require(isinstance(p['images'],dict) and p['images'].get('nextcloud')==c['nextcloud_image']
            and p['images'].get('postgres')==c['postgres_image'],'RESTORE_IMAGE_MISMATCH')
    require(isinstance(p['secrets_base64'],dict) and set(p['secrets_base64'])==SECRETS
            and isinstance(p['runtime_code_base64'],dict) and set(p['runtime_code_base64'])==RUNTIME,
            'RESTORE_RECOVERY_INPUTS_INCOMPLETE')
    require(isinstance(p['globals_sql_without_passwords'],str) and
            0<len(p['globals_sql_without_passwords'])<=65536,'RESTORE_GLOBALS_INVALID')
    require(all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',v) for v in (db['database'],db['owner'])),
            'RESTORE_DATABASE_IDENTIFIER_REJECTED')
    return ProtectedParameters(db,p['globals_sql_without_passwords'],
        {k:decode(v,4096) for k,v in p['secrets_base64'].items()},decode(p['compose_env_base64'],65536),
        {k:decode(v,1024**2) for k,v in p['runtime_code_base64'].items()})
