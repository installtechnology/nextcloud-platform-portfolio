"""Fixed sandbox endpoints and image references; never discover AWS resources."""
from pathlib import Path
import re
from ..capture.storage import CaptureFailure

MOTO = 'ghcr.io/getmoto/motoserver:5.1.22@sha256:117238c6e7e3b566387c4c74e6fb0b6fdc34a094920c2154cf06f11dc72d9f37'
IMAGES = {
 'nextcloud': 'nextcloud:34.0.4-apache@sha256:72f5cc3347042dd5dcad72edc376b19d0e042194af344a549820affd04804c8f',
 'postgres': 'postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826',
 'redis': 'redis:7.4.8-bookworm@sha256:3e0669e42d4fe421c9dea0ba5fbc04d336b80b4f32a6c7d25bee3a1d089288a1',
 's3-emulator': MOTO, 'imds-emulator': MOTO,
}
NETWORKS = {'nextcloud': {'db_internal','storage_internal'}, 'postgres': {'db_internal'},
            'redis': {'db_internal'}, 's3-emulator': {'storage_internal'}, 'imds-emulator': {'storage_internal'}}
CONTROLS = {'AWS_EC2_METADATA_SERVICE_ENDPOINT': 'https://imds.sandbox.test:1338',
            'AWS_EC2_METADATA_V1_DISABLED': 'true'}
BUCKET = 'fixture-data'
HERE = Path(__file__).resolve().parent
PROJECT_TEMPLATE = 'nc-s3-imds-test-${SANDBOX_ID:?16 lowercase hexadecimal characters required}'

def require(condition, code):
    if not condition: raise CaptureFailure(code)

def project_id(value):
    require(isinstance(value,str) and re.fullmatch(r'nc-s3-imds-test-[a-f0-9]{16}',value), 'SANDBOX_PROJECT_REQUIRED')
    return value

def environment(service, values, *, template=False):
    require(service in IMAGES and isinstance(values,dict), 'SANDBOX_ENV_INVALID')
    for name,value in values.items():
        require(isinstance(name, str) and isinstance(value, str), 'SANDBOX_ENV_INVALID')
        if name.startswith('AWS_'):
            require(service=='nextcloud' and name in CONTROLS and value==CONTROLS[name], 'SANDBOX_CREDENTIAL_SOURCE_REJECTED')
        require(not name.startswith(('NC_', 'DOCKER_', 'TF_', 'ACTIONS_ID_TOKEN_'))
                and name.upper() not in {'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy'.upper()}
                and name not in {'PYTHONPATH','PYTHONHOME','LD_PRELOAD','LD_LIBRARY_PATH','BASH_ENV','ENV'}
                and (not name.startswith('MOTO_') or (service == 's3-emulator'
                     and name == 'MOTO_S3_CUSTOM_ENDPOINTS' and value == 'https://s3.sandbox.test:5000'))
                and not ('PASSWORD' in name and not name.endswith('_FILE')), 'SANDBOX_CREDENTIAL_SOURCE_REJECTED')
        require(not (service == 'nextcloud' and name == 'HOME' and value not in ('/root','/var/www')),
                'SANDBOX_CREDENTIAL_SOURCE_REJECTED')
    if service=='nextcloud':
        require(all(values.get(k)==v for k,v in CONTROLS.items()), 'SANDBOX_IMDS_REQUIRED')
    require(values.get('PLATFORM_SYNTHETIC_SANDBOX') == 's3-imds-v1', 'SANDBOX_CONTEXT_REQUIRED')
    identity = values.get('PLATFORM_SANDBOX_PROJECT')
    if not (template and identity == PROJECT_TEMPLATE):
        project_id(identity)
