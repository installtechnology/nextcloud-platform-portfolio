"""Static contracts + optional native validators; never starts a service."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / 'monitoring'
parser = argparse.ArgumentParser()
parser.add_argument('--promtool', type=Path)
parser.add_argument('--loki', type=Path)
parser.add_argument('--alloy', type=Path)
args = parser.parse_args()

class UniqueLoader(yaml.SafeLoader):
    pass
def mapping(loader, node, deep=False):
    result = {}
    for key, value in loader.construct_pairs(node, deep=deep):
        if key in result: raise ValueError(f'Duplicate YAML key: {key}')
        result[key] = value
    return result
UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
def load(path): return yaml.load(path.read_text(encoding='utf-8'), Loader=UniqueLoader)
for path in MONITORING.rglob('*.yml'): load(path)
compose = load(ROOT / 'docker/compose.yml')
services = compose['services']
observability = {'prometheus','grafana','loki','alloy','node-exporter'}
assert compose['networks']['observability']['internal'] is True
assert services['nextcloud']['networks'] == ['backend','frontend']
for name in observability:
    service = services[name]
    assert not service.get('privileged') and service.get('network_mode') != 'host'
    assert 'frontend' not in service['networks']
    assert service['cap_drop'] == ['ALL'] and service['read_only'] is True
    assert not any(key.startswith(('AWS_', 'NEXTCLOUD_S3_')) for key in service.get('environment', {}))
    assert not any('docker.sock' in str(mount) for mount in service.get('volumes', []))
    if name != 'grafana': assert 'ports' not in service
assert services['grafana']['ports'] == ['0.0.0.0:${GRAFANA_HTTP_PORT:-3000}:3000']
assert services['grafana']['networks'] == ['observability', 'grafana_access']
assert compose['networks']['grafana_access'] == {'driver': 'bridge', 'internal': False, 'enable_ipv6': False}
assert sum('grafana_access' in s['networks'] for s in services.values()) == 1
assert services['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'] == 'false'
assert services['grafana']['environment']['GF_USERS_ALLOW_SIGN_UP'] == 'false'
assert '${GRAFANA_PUBLIC_HOST:?' in services['grafana']['environment']['GF_SERVER_ROOT_URL']
assert services['grafana']['secrets'] == ['grafana_admin_password']
assert services['grafana']['environment']['GF_SECURITY_ADMIN_PASSWORD__FILE'] == '/run/secrets/grafana_admin_password'
assert services['alloy']['user'] == '0:473'
mount = next(m for m in services['alloy']['volumes'] if isinstance(m,dict))
assert mount['read_only'] is True and mount['bind']['create_host_path'] is False
assert services['node-exporter']['user'] == '65534:65534'
assert '/:/host/root:ro,rslave' in services['node-exporter']['volumes']
for service in services.values():
    logging = service['logging']
    assert logging['driver'] == 'json-file'
    assert logging['options']['max-size'] == '5m' and logging['options']['max-file'] == '3'
    assert 'com.docker.compose.service' in logging['options']['labels']
assert '--storage.tsdb.retention.time=48h' in services['prometheus']['command']
assert '--storage.tsdb.retention.size=1GB' in services['prometheus']['command']
assert '--web.enable-remote-write-receiver' in services['prometheus']['command']
loki = load(MONITORING / 'loki/loki.yml')
assert loki['limits_config']['retention_period'] == '24h'
assert loki['compactor']['retention_enabled'] is True
assert loki['compactor']['delete_request_store'] == 'filesystem'
assert loki['schema_config']['configs'][0]['object_store'] == 'filesystem'
assert loki['schema_config']['configs'][0]['index']['period'] == '24h'
assert 's3' not in json.dumps(loki).lower()

environment = Environment(undefined=StrictUndefined)
environment.filters['to_json'] = json.dumps
template = environment.from_string((MONITORING / 'alloy/blackbox.yml.j2').read_text(encoding='utf-8'))
blackbox_text = template.render(nextcloud_stack_trusted_domains='fixture.invalid:8080 other.invalid')
blackbox = yaml.load(blackbox_text, Loader=UniqueLoader)
assert blackbox['modules']['nextcloud_status']['http']['headers']['Host'] == 'fixture.invalid:8080'
patterns = blackbox['modules']['nextcloud_status']['http']['fail_if_body_not_matches_regexp']
def healthy(body): return all(re.search(pattern,body) for pattern in patterns)
assert healthy('{"installed":true,"maintenance":false,"needsDbUpgrade":false}')
for fixture in ('{"installed":false,"maintenance":false,"needsDbUpgrade":false}',
                '{"installed":true,"maintenance":true,"needsDbUpgrade":false}',
                '{"installed":true,"maintenance":false,"needsDbUpgrade":true}', '<html>Error</html>'):
    assert not healthy(fixture)
alloy_text = (MONITORING / 'alloy/config.alloy').read_text(encoding='utf-8')
assert 'discovery.docker' not in alloy_text and 'loki.source.docker' not in alloy_text
assert 'stage.docker' in alloy_text and 'stage.label_drop' in alloy_text
assert 'tail_from_end' in alloy_text and 'sys.env("COMPOSE_PROJECT_NAME")' in alloy_text
assert 'prometheus.exporter.blackbox' in alloy_text
assert 'discovery.relabel.services.output' in alloy_text
assert '.*/(nextcloud|postgres|redis)' in alloy_text and 'replacement' in alloy_text
assert 'http://prometheus:9090/api/v1/write' in alloy_text
assert 'max_keepalive_time' in alloy_text and '30m' in alloy_text
assert 'com.docker.compose.service' in alloy_text and 'com.docker.compose.project' in alloy_text
datasources = load(MONITORING / 'grafana/provisioning/datasources/datasources.yml')['datasources']
uids = {item['uid'] for item in datasources}
assert uids == {'platform-prometheus','platform-loki'}
assert all(item['access']=='proxy' and item['editable'] is False for item in datasources)
dashboard = json.loads((MONITORING / 'grafana/dashboards/platform-overview.json').read_text(encoding='utf-8'))
assert dashboard['uid'] == 'platform-overview' and dashboard['editable'] is False
assert len({panel['id'] for panel in dashboard['panels']}) == len(dashboard['panels'])
expressions = [target['expr'] for panel in dashboard['panels'] for target in panel['targets']]
for metric in ('node_cpu_seconds_total','node_memory_MemAvailable_bytes','node_filesystem_avail_bytes','node_boot_time_seconds','probe_success','ALERTS'):
    assert any(metric in expr for expr in expressions)
for panel in dashboard['panels']: assert panel['datasource']['uid'] in uids
assert any(panel['type']=='logs' for panel in dashboard['panels'])
for path in MONITORING.rglob('*'):
    if path.is_file() and path.suffix in ('.alloy','.yml','.j2','.json'):
        assert b'\r' not in path.read_bytes(), f'Use LF: {path}'
print('PASS: YAML/JSON/Jinja, métricas/dashboard, probe funcional, retenção, IAM/rede/mounts/secrets e logs sem socket.')

def run(command, env=None):
    result = subprocess.run([str(part) for part in command], cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode: raise RuntimeError(f'{command[0]}: {result.stdout}\n{result.stderr}')
    if result.stdout.strip(): print(result.stdout.strip())

if args.promtool:
    executable = args.promtool.resolve()
    run([executable,'check','config','--syntax-only',MONITORING/'prometheus/prometheus.yml'])
    run([executable,'check','rules',MONITORING/'prometheus/rules/alerts.yml'])
    run([executable,'test','rules',MONITORING/'tests/alerts.test.yml'])
    for panel in dashboard['panels']:
        if panel['datasource']['type']=='prometheus':
            for target in panel['targets']:
                result = subprocess.run([str(executable),'--experimental','promql','format',target['expr']],capture_output=True)
                assert result.returncode==0, f'Invalid PromQL: {target["expr"]}'
    print('PASS: PromQL dos painéis e alertas testados no engine Prometheus offline.')
if args.loki:
    run([args.loki.resolve(),'-config.file='+str(MONITORING/'loki/loki.yml'),'-verify-config=true'])
    print('PASS: Loki -verify-config nativo; nenhum servidor iniciado.')
if args.alloy:
    with tempfile.TemporaryDirectory(prefix='alloy-config-validation-') as directory:
        local = Path(directory)
        blackbox_file = local/'blackbox.yml'
        blackbox_file.write_text(blackbox_text, encoding='utf-8')
        config_file = local/'config.alloy'
        config_file.write_text(alloy_text.replace('/etc/alloy/blackbox.yml',blackbox_file.as_posix()),encoding='utf-8')
        env = dict(os.environ, COMPOSE_PROJECT_NAME='fixture')
        run([args.alloy.resolve(),'validate',config_file],env=env)
    run([args.alloy.resolve(),'fmt','--test',MONITORING/'alloy/config.alloy'])
    print('PASS: Alloy validate/fmt nativos; paths temporários, nenhum coletor iniciado.')
print('VM/Docker daemon não executados; provisioning/render de dashboard, coleta e consumo reais continuam pendentes.')
