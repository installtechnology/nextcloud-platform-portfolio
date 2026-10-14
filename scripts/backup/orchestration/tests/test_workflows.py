"""Offline YAML/HCL bindings and absence of operational capabilities, not AWS proof."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import unittest

import hcl2
import yaml

ROOT = Path(__file__).resolve().parents[4]


def workflow(name):
    # BaseLoader retains YAML 'on' instead of interpreting it as a YAML 1.1 boolean.
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


class WorkflowTests(unittest.TestCase):
    def test_backup_dispatch_only(self):
        self.assertEqual(set(workflow('backup.yml')['on']), {'workflow_dispatch'})

    def test_no_oidc_or_secrets_in_either_workflow(self):
        for name in ('backup.yml', 'backup-posix-validation.yml'):
            document = workflow(name)
            self.assertEqual(document['permissions'], {'contents': 'read'})
            source = (ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('secrets.', source)
            self.assertNotIn('id-token:', source)
            self.assertNotIn('configure-aws-credentials', source)

    def test_shared_concurrency_matches_deploy_exactly(self):
        backup = workflow('backup.yml')['jobs']['blocked-backup']['concurrency']
        deploy = workflow('deploy.yml')['jobs']['deploy']['concurrency']
        self.assertEqual(backup, deploy)
        self.assertEqual(backup['cancel-in-progress'], 'false')

    def test_environment_confirmation_and_guard(self):
        document = workflow('backup.yml')
        self.assertEqual(document['name'], 'Backup platform (manual)')
        self.assertEqual(document['on']['workflow_dispatch']['inputs']['confirmation']['required'], 'true')
        job = document['jobs']['blocked-backup']
        self.assertEqual(job['environment'], '${{ inputs.environment }}')
        self.assertEqual(job['env']['DEPLOY_BRANCH'], '${{ vars.DEPLOY_BRANCH }}')
        self.assertEqual(job['steps'][-1]['run'], 'python3 -m scripts.backup.orchestration')

    def test_allowlisted_steps_only_no_hidden_operational_path(self):
        for name, expected in (
            ('backup.yml', 'python3 -m scripts.backup.orchestration'),
            ('backup-posix-validation.yml', None),
        ):
            jobs = workflow(name)['jobs']
            self.assertEqual(len(jobs), 1)
            job = next(iter(jobs.values()))
            self.assertEqual(len(job['steps']), 3 if expected else 4)
            guard, checkout, step = job['steps'][0], job['steps'][1], job['steps'][-1]
            self.assertEqual(guard['shell'], 'bash')
            self.assertIn('"$GITHUB_REF" != \'refs/heads/main\'', guard['run'])
            self.assertIn('"$GITHUB_EVENT_NAME" != \'workflow_dispatch\'', guard['run'])
            self.assertNotIn('${{', guard['run'])
            self.assertIn('exit 2', guard['run'])
            self.assertEqual(checkout['uses'], 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1')
            self.assertEqual(checkout['with']['persist-credentials'], 'false')
            self.assertEqual(checkout['with']['ref'], '${{ github.sha }}')
            if expected:
                self.assertEqual(set(step), {'name', 'run'})
                self.assertEqual(step['run'], expected)
            else:
                preparation = job['steps'][2]
                self.assertEqual(set(preparation), {'name', 'shell', 'run'})
                self.assertEqual(set(step), {'name', 'shell', 'run'})
                self.assertIn('/usr/bin/python3.12 -I scripts/backup/orchestration/linux/prepare_posix.py', preparation['run'])
                self.assertIn('set -euo pipefail', preparation['run'])
                self.assertIn('/venv/bin/python" -m scripts.backup.orchestration.posix_validation', step['run'])
                self.assertNotIn('python3 -m', step['run'])
            self.assertNotIn('permissions', job)

    def test_posix_no_environment_and_main_only(self):
        document = workflow('backup-posix-validation.yml')
        self.assertEqual(set(document['on']), {'workflow_dispatch'})
        job = document['jobs']['posix']
        self.assertNotIn('environment', job)
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main' && inputs.confirmation == 'VALIDATE_POSIX'")

    def test_pr4_oidc_claim_binding_stays_exact(self):
        source = hcl2.loads((ROOT / 'terraform/aws/bootstrap/backup-iam.tf').read_text())
        trust = source['locals'][0]['backup_trust_json']
        for key, expression in (
            ('aud', 'sts.amazonaws.com'), ('sub', '${local.github_subject}'),
            ('repository_id', '${var.github_oidc.repository_id}'),
            ('repository_owner_id', '${var.github_oidc.owner_id}'),
        ):
            self.assertIn('token.actions.githubusercontent.com:' + key, trust)
            self.assertIn(expression, trust)
        self.assertIn('refs/heads/${var.github_backup.branch}', trust)
        self.assertIn('${var.github_backup.workflow_name}', trust)
        self.assertIn('StringEquals', trust)
        self.assertIn('${local.oidc_provider_arn}', trust)

    def test_opt_in_default_remains_null(self):
        source = hcl2.loads((ROOT / 'terraform/aws/bootstrap/variables.tf').read_text())
        backup = next(item['github_backup'] for item in source['variable'] if 'github_backup' in item)
        self.assertIsNone(backup['default'])

    def test_documented_workflow_name_matches_prepared_example(self):
        name = workflow('backup.yml')['name']
        self.assertIn('workflow_name = "' + name + '"', (ROOT / 'docs/phase-7-pr4.md').read_text())

    def test_environment_input_can_match_pr4_mock_configuration(self):
        source = hcl2.loads((ROOT / 'terraform/aws/bootstrap/tests/backup.tftest.hcl').read_text())
        oidc = source['variables'][0]['github_oidc']
        environment = workflow('backup.yml')['on']['workflow_dispatch']['inputs']['environment']
        self.assertEqual(environment['type'], 'string')
        self.assertEqual(environment['required'], 'true')
        # Exact synthetic immutable subject used by PR4, without operational IDs.
        subject = f"repo:{oidc['owner']}@{oidc['owner_id']}/{oidc['repository']}@{oidc['repository_id']}:environment:{oidc['environment']}"
        self.assertEqual(subject, 'repo:fixture@123/platform@456:environment:test')
        mock_run = next(run['backup_security_contract'] for run in source['run'] if 'backup_security_contract' in run)
        config = mock_run['variables'][0]['github_backup']
        self.assertEqual(config['workflow_name'], workflow('backup.yml')['name'])
        self.assertEqual(config['branch'], 'main')
        # No accidental requirement of branch inside the Environment subject.
        self.assertNotIn('refs/heads/', subject)

    def test_permissions_not_overridden_and_inputs_never_enter_run_text(self):
        for name in ('backup.yml', 'backup-posix-validation.yml'):
            document = workflow(name)
            self.assertEqual(document['permissions'], {'contents': 'read'})
            self.assertNotIn('env', document)
            for job in document['jobs'].values():
                self.assertNotIn('permissions', job)
                self.assertNotIn('container', job)
                self.assertNotIn('services', job)
                for step in job['steps']:
                    self.assertNotIn('${{', step.get('run', ''))
                    self.assertNotIn('continue-on-error', step)

    def test_literal_group_expression_and_case_insensitive_group_identity(self):
        expression = 'aws-${{ vars.AWS_ACCOUNT_ID }}-${{ vars.PROJECT_NAME }}-${{ inputs.environment }}'
        backup = workflow('backup.yml')['jobs']['blocked-backup']['concurrency']['group']
        deploy = workflow('deploy.yml')['jobs']['deploy']['concurrency']['group']
        self.assertEqual(backup, expression)
        self.assertEqual(deploy, expression)
        def resolved(template, project='fixture', environment='lab', account='000000000000'):
            return template.replace('${{ vars.AWS_ACCOUNT_ID }}', account).replace('${{ vars.PROJECT_NAME }}', project).replace('${{ inputs.environment }}', environment).casefold()
        # Models documented string identity only, not the GitHub scheduler.
        self.assertEqual(resolved(backup), resolved(deploy, project='FIXTURE', environment='LAB'))
        self.assertNotEqual(resolved(backup), resolved(deploy, environment='other'))
        self.assertNotEqual(resolved(backup), resolved(deploy, account=''))

    def test_posix_dependency_pins_match_existing_pr2_requirements(self):
        from scripts.backup.orchestration.posix_validation import DEPENDENCIES
        requirements = (ROOT / 'scripts/backup/requirements.txt').read_text().splitlines()
        self.assertEqual({distribution + '==' + version for distribution, version in DEPENDENCIES.values()},
                         {line for line in requirements if line and not line.startswith('#')})

    def test_precheckout_guards_execute_without_input_interpolation(self):
        # Git Bash on Windows is not proof of any POSIX capture/file primitive.
        bash = os.environ.get('PR6_TEST_BASH') or (shutil.which('bash') if os.name == 'posix' else None)
        if not bash:
            self.skipTest('Approved existing Bash unavailable; precheckout shell execution pending')
        for name, confirmation, field in (
            ('backup.yml', 'BACKUP', 'BACKUP_CONFIRMATION'),
            ('backup-posix-validation.yml', 'VALIDATE_POSIX', 'POSIX_CONFIRMATION'),
        ):
            script = next(iter(workflow(name)['jobs'].values()))['steps'][0]['run']
            for ref, event, supplied, expected in (
                ('refs/heads/main', 'workflow_dispatch', confirmation, 0),
                ('refs/heads/Main', 'workflow_dispatch', confirmation, 2),
                ('refs/heads/other', 'workflow_dispatch', confirmation, 2),
                ('refs/heads/main', 'push', confirmation, 2),
                ('refs/heads/main', 'workflow_dispatch', '', 2),
                ('refs/heads/main', 'workflow_dispatch', '$(printf UNSAFE_INPUT_EXECUTED)\n; echo UNSAFE_INPUT_EXECUTED', 2),
            ):
                env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
                env.update(GITHUB_REF=ref, GITHUB_EVENT_NAME=event, **{field: supplied})
                result = subprocess.run([bash, '--noprofile', '--norc', '-c', script], env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn('UNSAFE_INPUT_EXECUTED', result.stdout + result.stderr)
                if expected:
                    report = json.loads(result.stdout)
                    self.assertEqual(report['code'], 'DISPATCH_CONTEXT_REJECTED')
                    if name == 'backup.yml':
                        self.assertIs(report['operational_allowed'], False)
                        self.assertIs(report['backup_complete'], False)


if __name__ == '__main__':
    unittest.main()
