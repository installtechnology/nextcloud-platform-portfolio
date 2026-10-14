"""Contratos da stack e config Compose opcional; nunca inicia containers."""
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import tempfile
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / 'docker'
parser = argparse.ArgumentParser()
parser.add_argument('--docker-cli', type=Path)
parser.add_argument('--docker-config', type=Path)
args = parser.parse_args()


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key, value in loader.construct_pairs(node, deep=deep):
        if key in result:
            raise ValueError(f'Chave duplicada: {key}')
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
text = (DOCKER / 'compose.yml').read_text(encoding='utf-8')
model = yaml.load(text, Loader=UniqueLoader)
assert set(model['services']) == {'postgres', 'redis', 'nextcloud', 'prometheus', 'grafana', 'loki', 'alloy', 'node-exporter'}
assert model['networks']['backend']['internal'] is True
assert set(model['volumes']) == {'postgres_data', 'nextcloud_html', 'prometheus_data', 'grafana_data', 'loki_data', 'alloy_data'}
for name, service in model['services'].items():
    assert re.search(r':v?\d+(?:\.\d+){1,2}[^@]*@sha256:[a-f0-9]{64}$', service['image'])
    assert 'latest' not in service['image']
    if name not in ('loki', 'alloy'):
        assert 'healthcheck' in service
    assert not service.get('privileged', False)
    assert service.get('network_mode') != 'host'
    assert not any('docker.sock' in str(mount) for mount in service.get('volumes', []))
    if name in ('postgres', 'redis'):
        assert 'ports' not in service and service['networks'] == ['backend']
    elif name != 'nextcloud':
        assert 'observability' in service['networks']
        if name != 'grafana':
            assert 'ports' not in service
        else:
            assert service['ports'] == ['0.0.0.0:${GRAFANA_HTTP_PORT:-3000}:3000']
            assert service['networks'] == ['observability', 'grafana_access']
    assert not any(key.startswith(('AWS_', 'OBJECTSTORE_', 'S3_')) for key in service.get('environment', {}))
assert len(model['services']['nextcloud']['ports']) == 1
for dependency in ('postgres', 'redis'):
    assert model['services']['nextcloud']['depends_on'][dependency]['condition'] == 'service_healthy'
assert model['services']['postgres']['environment']['POSTGRES_USER'] == 'postgres'
assert model['services']['nextcloud']['environment']['POSTGRES_USER'].startswith('${POSTGRES_USER:')
for secret in model['secrets'].values():
    assert secret['file'].startswith('${')
for name, service in model['services'].items():
    for key, value in service.get('environment', {}).items():
        if 'PASSWORD' in key:
            assert key.endswith('_FILE') and value.startswith('/run/secrets/')

example = (DOCKER / '.env.example').read_text(encoding='utf-8')
example_vars = dict(line.split('=', 1) for line in example.splitlines() if line and not line.startswith('#'))
assert all(value.startswith('REPLACE_') for value in example_vars.values())
assert set(re.findall(r'\$\{([A-Z_0-9]+)', text)) <= set(example_vars)
assert not (DOCKER / '.env').exists(), 'Não criar env real no repo'

env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
template = env.from_string((ROOT / 'ansible/roles/nextcloud_stack/templates/env.j2').read_text(encoding='utf-8'))
fixture = dict(ansible_host='192.0.2.10', nextcloud_stack_project='phase3-test', nextcloud_stack_postgres_db='fixture_db',
               nextcloud_stack_postgres_user='fixture_user', nextcloud_stack_admin_user='fixture_admin',
               nextcloud_stack_trusted_domains='example.invalid', nextcloud_stack_bind_address='127.0.0.1',
               nextcloud_stack_http_port=8080, nextcloud_stack_s3_bucket='fixture-data', nextcloud_stack_s3_region='us-east-1',
               nextcloud_stack_grafana_admin_user='fixture_monitoring', nextcloud_stack_grafana_http_port=3000,
               nextcloud_stack_docker_log_directory='/var/lib/docker/containers')
rendered = dict(line.split('=', 1) for line in template.render(**fixture).splitlines() if line)
assert set(rendered) == set(example_vars)
assert not any('PASSWORD' in key and not key.endswith('_FILE_PATH') for key in rendered)
assert json.loads(rendered['NEXTCLOUD_TRUSTED_DOMAINS']) == 'example.invalid'
for current_ip in ('192.0.2.10', '198.51.100.42'):
    dynamic = dict(line.split('=', 1) for line in template.render(**dict(fixture, ansible_host=current_ip)).splitlines() if line)
    assert dynamic['GRAFANA_PUBLIC_HOST'] == current_ip
assert '${GRAFANA_PUBLIC_HOST:?' in model['services']['grafana']['environment']['GF_SERVER_ROOT_URL']
assert model['services']['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'] == 'false'
for source in ('postgres-init.sh', 'redis-entrypoint.sh', 'configure-nextcloud.sh',
               'platform.config.php', 'storage-guard.php', 'install-nextcloud.php', 'nextcloud-healthcheck.php', 'nextcloud-integration.php'):
    content = (DOCKER / source).read_bytes()
    assert b'\r' not in content, f'Usar LF em {source}'
init = (DOCKER / 'postgres-init.sh').read_text(encoding='utf-8')
assert 'NOSUPERUSER NOCREATEDB NOCREATEROLE' in init
config = (DOCKER / 'platform.config.php').read_text(encoding='utf-8')
assert 'memcache.local' in config and 'memcache.locking' in config
assert "'objectstore'" in config and "'trusted_proxies'" not in config
assert not any('/var/www/html/config/' in str(value) for value in model['services']['nextcloud']['volumes'])
print('PASS: YAML Compose, images/digests, rede/volumes/secrets, template env e contratos Fase 4.')

if args.docker_cli:
    with tempfile.TemporaryDirectory(prefix='nextcloud-compose-validation-') as directory:
        dummy_secret = Path(directory) / 'fixture-secret'
        dummy_secret.write_text('VALIDATION_ONLY_NOT_A_RUNTIME_PASSWORD', encoding='utf-8')
        process_env = dict(os.environ)
        process_env.update({key: value.strip('"') for key, value in rendered.items()})
        for key in process_env.copy():
            if key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY'):
                process_env.pop(key)
        for key in rendered:
            if key.endswith('_FILE_PATH'):
                process_env[key] = str(dummy_secret)
        if args.docker_config:
            process_env['DOCKER_CONFIG'] = str(args.docker_config.resolve())
        command = [str(args.docker_cli.resolve()), 'compose', '--env-file', str(DOCKER / '.env.example'),
                   '-f', str(DOCKER / 'compose.yml'), 'config', '--quiet']
        result = subprocess.run(command, env=process_env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        for key in ('POSTGRES_DB', 'NEXTCLOUD_ADMIN_USER', 'NEXTCLOUD_S3_BUCKET', 'NEXTCLOUD_S3_REGION', 'GRAFANA_ADMIN_USER', 'GRAFANA_PUBLIC_HOST', 'GRAFANA_ADMIN_PASSWORD_FILE_PATH', 'DOCKER_LOG_DIRECTORY_PATH'):
            invalid_env = dict(process_env, **{key: ''})
            invalid = subprocess.run(command, env=invalid_env, capture_output=True, text=True)
            assert invalid.returncode != 0, f'Compose deve rejeitar {key} vazio'
        print('PASS: docker compose config --quiet real e rejeição de parâmetros obrigatórios vazios.')
else:
    print('Compose CLI não executado; fornecer --docker-cli quando disponível.')
print('Nenhum container/volume criado. Teste operacional e idempotência em VM continuam pendentes.')

assert 'before-starting/10-platform.sh' in text
assert "'verify_bucket_exists' => false" in config
assert "'key' =>" not in config and "'secret' =>" not in config
iam = (ROOT / 'terraform/aws/runtime/iam.tf').read_text(encoding='utf-8')
assert 'backups' not in iam and 's3:DeleteObjectVersion' not in iam and 's3:CreateBucket' not in iam
assert '/nextcloud-data/' not in iam
assert 's3:PutObjectAcl' in iam and 's3:x-amz-acl' in iam and '["private"]' in iam
assert 'php /opt/platform/storage-guard.php' in (DOCKER / 'configure-nextcloud.sh').read_text(encoding='utf-8')
nextcloud = model['services']['nextcloud']
assert not {'NEXTCLOUD_ADMIN_USER', 'NEXTCLOUD_ADMIN_PASSWORD', 'NEXTCLOUD_ADMIN_PASSWORD_FILE', 'POSTGRES_PASSWORD_FILE'} & set(nextcloud['environment'])
assert nextcloud['environment']['PLATFORM_ADMIN_PASSWORD_FILE'] == '/run/secrets/nextcloud_admin_password'
assert nextcloud['environment']['PLATFORM_POSTGRES_PASSWORD_FILE'] == '/run/secrets/postgres_password'
assert './install-nextcloud.php:/opt/platform/install-nextcloud.php:ro' in nextcloud['volumes']
assert not any('/pre-installation/' in mount for mount in nextcloud['volumes'])
hook = (DOCKER / 'configure-nextcloud.sh').read_text(encoding='utf-8')
assert hook.index('php /opt/platform/storage-guard.php') < hook.index('cp /opt/platform/platform.config.php') < hook.index('php /opt/platform/install-nextcloud.php')
print('PASS: S3 antes da instalação, credenciais temporárias, isolamento IAM e ausência de migração.')
storage = (ROOT / 'terraform/aws/persistent/main.tf').read_text(encoding='utf-8')
assert storage.count('prevent_destroy = true') == 2
assert storage.count('force_destroy = false') == 2
assert 'aws_s3_bucket_lifecycle_configuration' not in storage
assert 'expiration' not in storage
assert storage.count('aws:SecureTransport') == 2
assert storage.count('DenyInsecureTransport') == 2
backup_iam = (ROOT / 'terraform/aws/bootstrap/iam.tf').read_text(encoding='utf-8')
assert 'local.data_bucket_arn' not in backup_iam
assert 's3:DeleteObject' not in backup_iam
assert 'aws_iam_policy_attachment' not in backup_iam
print('PASS: controles TLS/retenção dos dois buckets e política separada de backup sem exclusão/attachment.')
