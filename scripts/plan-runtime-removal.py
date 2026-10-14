"""Prepare a private, saved removal plan; never apply it automatically.

Run from the repository root. Requires Terraform and AWS credentials already
configured. Backend and variable files must remain outside Git.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from scripts.restore.reprovision import PERSISTENT

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--root', choices=('runtime', 'restore'), required=True)
p.add_argument('--backend', type=Path, required=True)
p.add_argument('--variables', type=Path, required=True)
p.add_argument('--instance', required=True)
args = p.parse_args()
tf = shutil.which('terraform')
if not tf:
    p.error('Terraform indisponivel no PATH')
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
work = root / '.validation-tools' / ('removal-' + args.root + '-' + stamp)
work.mkdir(parents=True, exist_ok=False)
for f in (root / 'terraform/aws' / args.root).glob('*.tf'):
    shutil.copyfile(f, work / f.name)
shutil.copyfile(root / 'terraform/aws' / args.root / '.terraform.lock.hcl', work / '.terraform.lock.hcl')
(work / 'backend.tf').write_text('terraform {\n backend "s3" {}\n}\n')
shutil.copyfile(args.backend.resolve(), work / 'private.tfbackend')
shutil.copyfile(args.variables.resolve(), work / 'private.tfvars.json')
env = dict(os.environ, TF_INPUT='false', TF_IN_AUTOMATION='1')
for k in list(env):
    if k.startswith(('TF_LOG', 'TF_CLI_ARGS')):
        env.pop(k)

def terraform(*command):
    print('terraform ' + ' '.join(command), flush=True)
    result = subprocess.run([tf, '-chdir=' + str(work), *command], env=env, capture_output=True)
    (work / (command[0] + '.private.log')).write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit('Falha; consulte o log privado em ' + str(work))
    return result.stdout

terraform('init', '-input=false', '-lockfile=readonly', '-backend-config=private.tfbackend')
terraform('validate')
state = json.loads(terraform('state', 'pull'))
(work / 'before.private.tfstate').write_text(json.dumps(state))
resources = {}
for r in state['resources']:
    if r['mode'] != 'managed':
        continue
    if r.get('module'):
        raise SystemExit('Root com modulos exige revisao especifica')
    base = r['type'] + '.' + r['name']
    for i in r['instances']:
        if i.get('deposed'):
            raise SystemExit('State com instancia deposed exige revisao')
        address = base + ('[' + json.dumps(i['index_key']) + ']' if 'index_key' in i else '')
        resources[address] = (base, i['attributes'])
if resources.get('aws_instance.host', ('', {}))[1].get('id') != args.instance:
    raise SystemExit('Instancia informada diverge do state')
targets = sorted(a for a, (base, _) in resources.items() if args.root == 'runtime' or base not in PERSISTENT)
if any(resources[a][0].startswith('aws_s3') for a in targets):
    raise SystemExit('Plano incluindo S3 rejeitado')
if args.root == 'restore' and set(resources) - set(targets) != PERSISTENT:
    raise SystemExit('Conjunto persistente inesperado')
plan = work / 'removal.tfplan'
command = ['plan', '-destroy', '-input=false', '-lock-timeout=60s', '-var-file=private.tfvars.json']
if args.root == 'restore':
    command += ['-target=' + a for a in targets]
terraform(*command, '-out=' + str(plan))
document = json.loads(terraform('show', '-json', str(plan)))
changes = [r for r in document.get('resource_changes', []) if r.get('mode') == 'managed' and r['change']['actions'] != ['no-op']]
if {r['address'] for r in changes} != set(targets) or not all(r['change']['actions'] == ['delete'] for r in changes):
    raise SystemExit('Plano diverge do escopo esperado; nao aplicar')
report = {'instance': args.instance, 'deletions': targets, 'preserved_in_this_state': sorted(set(resources) - set(targets)), 'plan_sha256': hashlib.sha256(plan.read_bytes()).hexdigest(), 'applied': False}
(work / 'review.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
print('Nenhuma exclusao executada. Depois de conferir backup e ausencia de operacoes ativas:')
print('& terraform "-chdir=' + str(work) + '" apply -input=false "' + str(plan) + '"')
