"""Checks locais de YAML/Jinja e contratos; não substituem Ansible syntax-check."""
from pathlib import Path
import configparser
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Chave YAML duplicada: {key}")
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
jinja = Environment()
ALLOWED = {
    "assert", "getent", "apt", "group", "user", "file", "package_facts",
    "get_url", "deb822_repository", "systemd_service", "meta", "import_tasks",
    "service_facts", "command", "copy", "template", "stat", "uri",
    "docker_compose_v2", "docker_compose_v2_pull",
    "set_fact", "docker_container_info",
}
files = sorted(ROOT.rglob("*.yml")) + [ROOT / "inventory/hosts.yml.example", ROOT / ".ansible-lint"]
documents = {}
task_count = 0


def inspect_strings(value):
    if isinstance(value, str) and any(marker in value for marker in ("{{", "{%")):
        jinja.parse(value)
    elif isinstance(value, list):
        for item in value:
            inspect_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            inspect_strings(item)


for path in files:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    documents[path.relative_to(ROOT).as_posix()] = document
    inspect_strings(document)
    if "tasks" in path.parts:
        assert isinstance(document, list), f"Tasks devem ser lista: {path}"
        for task in document:
            assert "name" in task, f"Task sem nome: {path}"
            modules = [key for key in task if key.startswith(("ansible.builtin.", "community.docker."))]
            assert len(modules) == 1, f"Task deve ter exatamente um builtin FQCN: {task}"
            module = modules[0].split('.')[-1]
            assert module in ALLOWED, f"Módulo fora do escopo: {module}"
            if module == "command":
                assert task["changed_when"] is False, "Probe deve ser somente leitura"
                argv = task[modules[0]]["argv"]
                if "nextcloud_stack" in path.parts:
                    assert argv[:3] == ["docker", "compose", "--project-name"]
                    assert argv[4:10] == ["exec", "-T", "--user", "www-data", "nextcloud", "php"]
                    assert argv[10:] in (["occ", "status", "--output=json"], ["/opt/platform/integration.php"])
                    assert task["no_log"] is True
                else:
                    assert argv in (["docker", "info"], ["docker", "compose", "version", "--short"])
            if module == "user":
                assert task[modules[0]]["append"] is True, "Preservar grupos existentes"
            if module == "file":
                assert isinstance(task[modules[0]]["mode"], str), "Permissão deve ser string"
            if module == "import_tasks":
                assert (path.parent / task[modules[0]]).is_file(), "Import inexistente"
            if module in ("docker_compose_v2", "docker_compose_v2_pull"):
                if module == "docker_compose_v2":
                    assert task["no_log"] is True
                    args = task[modules[0]]
                    if args['state'] == 'restarted':
                        assert not args['dependencies'] and 'nextcloud_stack_monitoring_restart' in args['services']
                        assert task['when'] == 'nextcloud_stack_monitoring_restart | length > 0'
                    else:
                        assert args["state"] == "present" and args["recreate"] == "auto"
                        assert args["wait"] and not args["remove_volumes"]
                else:
                    # Public pinned-image pull diagnostics must remain visible.
                    assert module == "docker_compose_v2_pull"
                    assert task.get("no_log", False) is False
                    assert set(task[modules[0]]) == {"project_src", "project_name", "policy"}
                    assert task[modules[0]]["policy"] == "missing"
            if "when" in task:
                conditions = task["when"] if isinstance(task["when"], list) else [task["when"]]
                for condition in conditions:
                    jinja.parse("{{ " + condition + " }}")
            if module == "assert":
                for condition in task[modules[0]]["that"]:
                    jinja.parse("{{ " + condition + " }}")
            task_count += 1

play = documents["playbooks/bootstrap.yml"][0]
assert play["hosts"] == "platform" and play["become"] is True
assert [role["role"] for role in play["roles"]] == ["common", "docker"]
deploy = documents["playbooks/deploy.yml"]
assert deploy[0]["ansible.builtin.import_playbook"] == "bootstrap.yml"
assert [role["role"] for role in deploy[1]["roles"]] == ["common", "nextcloud_stack"]
for task in play["pre_tasks"]:
    for condition in task["ansible.builtin.assert"]["that"]:
        jinja.parse("{{ " + condition + " }}")
for role in play["roles"]:
    assert (ROOT / "roles" / role["role"] / "tasks/main.yml").is_file()

host = documents["inventory/hosts.yml.example"]["all"]["children"]["platform"]["hosts"]["application_host"]
assert "PLATFORM_HOST" in host["ansible_host"] and "lookup" in host["ansible_host"]
assert "ansible_ssh_private_key_file" not in host
docker = documents["roles/docker/defaults/main.yml"]
assert docker["docker_allow_operational_user"] is False
assert "docker-compose-plugin" in docker["docker_packages"]
assert "docker-compose" not in docker["docker_packages"]
assert docker["docker_apt_repository_url"].startswith("https://download.docker.com/")
common = documents["roles/common/defaults/main.yml"]
stack = documents["roles/nextcloud_stack/defaults/main.yml"]
assert 'NEXTCLOUD_S3_BUCKET' in stack['nextcloud_stack_s3_bucket']
assert 'NEXTCLOUD_S3_REGION' in stack['nextcloud_stack_s3_region']
assert 'GRAFANA_ADMIN_USER' in stack['nextcloud_stack_grafana_admin_user']
assert 'GRAFANA_ADMIN_PASSWORD' in stack['nextcloud_stack_grafana_admin_password']
assert stack['nextcloud_stack_grafana_http_port'] == 3000
stack_tasks = documents['roles/nextcloud_stack/tasks/main.yml']
assert 'storage-guard.php' in next(t['loop'] for t in stack_tasks if t['name'] == 'Copiar configuração única da stack e probes')
assert 'install-nextcloud.php' in next(t['loop'] for t in stack_tasks if t['name'] == 'Copiar configuração única da stack e probes')
monitoring_tasks = documents['roles/nextcloud_stack/tasks/monitoring.yml']
selection = next(t['ansible.builtin.set_fact']['nextcloud_stack_monitoring_restart'] for t in monitoring_tasks if 'ansible.builtin.set_fact' in t)
from jinja2.nativetypes import NativeEnvironment
native = NativeEnvironment()
native.filters['split'] = lambda value, separator: value.split(separator)
selection_template = native.from_string(selection)
def selected(files, probe=False):
    return selection_template.render(nextcloud_stack_monitoring_files={'results': files},nextcloud_stack_monitoring_probe={'changed':probe})
assert selected([{'item':'prometheus/prometheus.yml','changed':False}]) == []
assert set(selected([{'item':'prometheus/prometheus.yml','changed':True},{'item':'prometheus/rules/alerts.yml','changed':True}], True)) == {'prometheus','alloy'}
assert selected([{'item':'grafana/dashboards/platform-overview.json','changed':True}]) == ['grafana']
assert common["common_application_directories"] == ["docker", "monitoring", "data", "backups"]
config = configparser.ConfigParser()
config.read(ROOT / "ansible.cfg")
assert config.getboolean("defaults", "host_key_checking")
for template in ROOT.glob('roles/*/templates/*.j2'):
    jinja.parse(template.read_text(encoding='utf-8'))
print(f"PASS: {len(files)} arquivos YAML/Jinja, {task_count} tasks e contratos estáticos.")
print("NÃO executados: ansible-playbook --syntax-check, lint ou teste em VM.")
