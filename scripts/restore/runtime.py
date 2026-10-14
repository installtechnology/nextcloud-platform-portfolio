"""Build a reviewed three-service restore recipe; never run Compose on import."""
import copy
import ipaddress
import json
import re
import yaml
from scripts.backup.sealing.common import require
from scripts.restore.parameters import SECRETS

ENV_KEYS={'COMPOSE_PROJECT_NAME','POSTGRES_DB','POSTGRES_USER','NEXTCLOUD_ADMIN_USER','NEXTCLOUD_TRUSTED_DOMAINS',
    'NEXTCLOUD_BIND_ADDRESS','NEXTCLOUD_HTTP_PORT','POSTGRES_ADMIN_PASSWORD_FILE_PATH','POSTGRES_PASSWORD_FILE_PATH',
    'NEXTCLOUD_ADMIN_PASSWORD_FILE_PATH','REDIS_PASSWORD_FILE_PATH','NEXTCLOUD_S3_BUCKET','NEXTCLOUD_S3_REGION',
    'GRAFANA_ADMIN_USER','GRAFANA_ADMIN_PASSWORD_FILE_PATH','GRAFANA_HTTP_PORT','GRAFANA_PUBLIC_HOST','DOCKER_LOG_DIRECTORY_PATH'}


def destination_runtime(parameters,config,public_ip,manifest):
    ip=str(ipaddress.IPv4Address(public_ip))
    require(ipaddress.IPv4Address(ip).is_global,'RESTORE_PUBLIC_HOST_REJECTED')
    try:
        text=parameters.compose_env.decode('utf-8');values={}
        for line in text.splitlines():
            if not line or line.startswith('#'):continue
            key,value=line.split('=',1)
            require(key in ENV_KEYS and key not in values and value and not any(ord(c)<32 for c in value),
                    'RESTORE_ENV_REVIEW_REQUIRED')
            values[key]=value
    except (ValueError,UnicodeError):raise ValueError('RESTORE_ENV_REVIEW_REQUIRED') from None
    require(set(values)==ENV_KEYS and values['COMPOSE_PROJECT_NAME']==config['project'] and
            values['POSTGRES_DB']==parameters.database['database'] and values['POSTGRES_USER']==parameters.database['owner']
            and values['NEXTCLOUD_S3_BUCKET']==config['source_data_bucket'] and values['NEXTCLOUD_S3_REGION']==config['region'],
            'RESTORE_ENV_SCOPE_MISMATCH')
    require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.@-]+',values['NEXTCLOUD_ADMIN_USER']) and
            values['NEXTCLOUD_HTTP_PORT']=='80','RESTORE_ENV_REVIEW_REQUIRED')
    for secret in SECRETS:
        env_key=secret.upper()+'_FILE_PATH'
        require(values[env_key]=='./secrets/'+secret,'RESTORE_SECRET_PATH_REJECTED')
    # Source environment is not sourced or evaluated by a shell.
    values.update(NEXTCLOUD_S3_BUCKET=config['data_bucket'],NEXTCLOUD_TRUSTED_DOMAINS=ip,
                  NEXTCLOUD_BIND_ADDRESS='0.0.0.0',GRAFANA_PUBLIC_HOST=ip)
    try:recipe=yaml.safe_load(parameters.runtime_code['compose.yml'])
    except yaml.YAMLError:raise ValueError('RESTORE_COMPOSE_REJECTED') from None
    require(isinstance(recipe,dict) and set(recipe)=={'x-platform-logging','services','networks','volumes','secrets'}
            and set(config['images'])=={'nextcloud','postgres','redis'},'RESTORE_COMPOSE_REJECTED')
    services={}
    for name in ('postgres','redis','nextcloud'):
        service=copy.deepcopy(recipe['services'].get(name))
        require(isinstance(service,dict) and service.get('image')==config['images'][name],
                'RESTORE_COMPOSE_IMAGE_MISMATCH')
        services[name]=service
    nextcloud=services['nextcloud']
    old='./configure-nextcloud.sh:/docker-entrypoint-hooks.d/before-starting/10-platform.sh:ro'
    require(nextcloud['volumes'].count(old)==1,'RESTORE_INSTALL_HOOK_MISMATCH')
    nextcloud['volumes']=[('./restore-start.sh:/docker-entrypoint-hooks.d/before-starting/10-platform.sh:ro'
                          if v==old else v) for v in nextcloud['volumes']]
    nextcloud['volumes'].append('./assert-restored.php:/opt/platform/assert-restored.php:ro')
    require(manifest['source']['data_bucket']==config['source_data_bucket'],'RESTORE_ENV_SCOPE_MISMATCH')
    nextcloud['environment']['RESTORE_EXPECTED_IDENTITY_SHA256']=manifest['source']['installation_identity_sha256']
    nextcloud['environment']['RESTORE_EXPECTED_MAINTENANCE']=str(manifest['consistency']['maintenance_initially_enabled']).lower()
    # Native auto-install inputs must remain absent; restored config is mandatory.
    require(not {'NEXTCLOUD_ADMIN_USER','NEXTCLOUD_ADMIN_PASSWORD','NEXTCLOUD_ADMIN_PASSWORD_FILE',
                 'POSTGRES_PASSWORD','POSTGRES_PASSWORD_FILE'}&set(nextcloud['environment']),
            'RESTORE_NATIVE_INSTALL_INPUT_REJECTED')
    # No observability startup or requirement for unbacked Grafana secrets.
    core={'services':services,'networks':{k:recipe['networks'][k] for k in ('backend','frontend')},
          'volumes':{k:recipe['volumes'][k] for k in ('postgres_data','nextcloud_html')},
          'secrets':{k:recipe['secrets'][k] for k in sorted(SECRETS)}}
    env='\n'.join(k+'='+v for k,v in sorted(values.items()) if not k.startswith('GRAFANA_') and k!='DOCKER_LOG_DIRECTORY_PATH')+'\n'
    return yaml.safe_dump(core,sort_keys=False).encode(),env.encode()


def remap_request(manifest,config,public_ip,mode='remap'):
    require(mode in ('remap','verify'),'RESTORE_REMAP_MODE_REJECTED')
    require(manifest['source']['data_bucket']==config['source_data_bucket'] and
            config['data_bucket'] not in {config['source_data_bucket'],config['backups_bucket'],config['state_bucket']},
            'RESTORE_REMAP_SCOPE_REJECTED')
    c=manifest['compatibility'];source=manifest['source']
    request={'mode':mode,'source_bucket':source['data_bucket'],'target_bucket':config['data_bucket'],
             'region':source['region'],'database':c['database_name'],'owner':c['database_owner'],
             'table_prefix':c['table_prefix'],'identity_sha256':source['installation_identity_sha256'],
             'maintenance':manifest['consistency']['maintenance_initially_enabled'],
             'trusted_domains':[str(ipaddress.IPv4Address(public_ip))]}
    return json.dumps(request,separators=(',',':')).encode()
