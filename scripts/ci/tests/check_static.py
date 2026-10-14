from pathlib import Path
import re
import yaml

ROOT=Path(__file__).resolve().parents[3]
# BaseLoader preserves GitHub's "on" key and rejects no field because YAML 1.1 treats it as bool.
workflow=yaml.load((ROOT/'.github/workflows/deploy.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
assert set(workflow['on'])=={'workflow_dispatch'}
assert workflow['on']['workflow_dispatch']['inputs']['mode']['default']=='plan'
assert workflow['permissions']=={'contents':'read','id-token':'write'}
job=workflow['jobs']['deploy']
assert job['concurrency']['cancel-in-progress']=='false'
assert job['environment']=='${{ inputs.environment }}'
steps=job['steps']
for step in steps:
    if 'uses' in step: assert re.search(r'@[a-f0-9]{40}$',step['uses'])
    assert 'upload-artifact' not in step.get('uses','')
assert steps[-1]['if']=='always()' and 'cleanup' in steps[-1]['run']
assert all(step['if']=="inputs.mode == 'apply'" for step in steps if any(word in step.get('run','') for word in ('deploy.py connect','ansible-playbook -i "$RUNNER_TEMP','deploy.py summary')))
assert steps.index(next(step for step in steps if 'syntax-check' in step.get('run',''))) < steps.index(next(step for step in steps if 'deploy.py terraform' in step.get('run','')))
text=(ROOT/'scripts/ci/deploy.py').read_text(encoding='utf-8')
assert 'StrictHostKeyChecking yes' in text and 'StrictHostKeyChecking=no' not in text
assert '0o600' in text and '0o700' in text
assert 'verify_scan(scan.stdout, ip, fingerprints)' in text
assert 'context()  # Check the explicit gate again immediately before apply.' in text
assert '["persistent", "runtime"]' in (ROOT/'terraform/aws/bootstrap/operational-iam.tf').read_text()
iam=(ROOT/'terraform/aws/bootstrap/operational-iam.tf').read_text()
for forbidden in ('AdministratorAccess','iam:*','ec2:*','s3:DeleteBucket','s3:DeleteObjectVersion','iam:DeleteRolePermissionsBoundary','iam:AttachRolePolicy'):
    assert forbidden not in iam,forbidden
assert '"${local.state_bucket_arn}/${key}.tflock"' in iam
assert 'iam:PermissionsBoundary' in iam and 'iam:PassedToService' in iam
assert 'github_read' not in (ROOT/'terraform/aws/bootstrap/iam.tf').read_text()
for root in ('bootstrap','persistent','runtime'):
    path=ROOT/f'terraform/aws/{root}'
    assert (path/'backend.tf.example').is_file() and not (path/'backend.tf').exists()
    assert 'allowed_account_ids = [var.aws_account_id]' in (path/'versions.tf').read_text()
state=(ROOT/'terraform/aws/bootstrap/state.tf').read_text(encoding='utf-8')
for term in ('prevent_destroy = true','force_destroy = false','"AES256"','"Enabled"','aws:SecureTransport','BucketOwnerEnforced'):
    assert term in state,term
assert 'lifecycle_configuration' not in state
assert set(p.name for p in (ROOT/'.github/workflows').glob('*.yml'))=={'deploy.yml'}
print('PASS: workflow manual, SHA pins, mode gates, backend/IAM boundaries, SSH verification/cleanup and no forbidden phases.')
