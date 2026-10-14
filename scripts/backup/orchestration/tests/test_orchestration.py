"""All observations are synthetic; no operational collector is imported."""
import copy
from datetime import datetime, timezone
import io
import json
import os
import unittest
from unittest.mock import patch

from scripts.backup.orchestration.__main__ import main
from scripts.backup.orchestration.gates import BLOCKERS, GateFailure, WORKFLOW_NAME
from scripts.backup.orchestration.preflight import evaluate, FLAGS
from scripts.backup.orchestration import posix_validation as posix

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def context():
    return {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'BACKUP_CONFIRMATION': 'BACKUP',
            'DEPLOY_BRANCH': 'main', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_WORKFLOW': WORKFLOW_NAME,
            'BACKUP_ENVIRONMENT': 'lab', 'AWS_ACCOUNT_ID': '000000000000', 'PROJECT_NAME': 'fixture'}


def observations():
    config = {'account': '000000000000', 'region': 'us-east-1', 'project': 'fixture', 'environment': 'lab',
              'instance_id': 'i-00000000000000000',
              'instance_profile': 'arn:aws:iam::000000000000:instance-profile/fixture-lab-ec2',
              'data_bucket': 'fixture-data', 'backups_bucket': 'fixture-backups',
              'installation_identity_sha256': 'a' * 64, 'repository_commit': 'b' * 40}
    observed = {k: v for k, v in config.items() if k not in ('project', 'environment')}
    observed.update({key: True for key in FLAGS})
    observed.update(synthetic=True, observed_at='2026-01-01T00:00:00Z', role_name='fixture-lab-backup',
                    instance_count=1, tags={'Project': 'fixture', 'Environment': 'lab'},
                    nextcloud_version='34.0.4', postgres_major=17, dump_client_major=17, maintenance=False,
                    free_bytes=20_000_000, required_bytes=10_000_000, session_seconds=3600, required_seconds=1800)
    return config, observed


class GateTests(unittest.TestCase):
    def invoke(self, values=None, argv=None):
        output = io.StringIO()
        with patch('sys.stdout', output):
            status = main([] if argv is None else argv, context() if values is None else values)
        return status, json.loads(output.getvalue())

    def test_valid_dispatch_always_disabled(self):
        status, report = self.invoke()
        self.assertEqual((status, report['code']), (78, 'OPERATIONAL_BACKUP_DISABLED'))
        self.assertFalse(report['operational_allowed'])
        self.assertFalse(report['backup_complete'])
        self.assertEqual(report['functional_restore'], 'not_verified')
        self.assertEqual(report['blockers'], list(BLOCKERS))

    def test_variables_cannot_activate(self):
        values = context()
        values.update(BACKUP_ENABLED='true', OPERATIONAL_ALLOWED='true', ALL_TESTS_PASSED='true',
                      AWS_ROLE_ARN='synthetic', BACKUP_COMPLETE='true')
        self.assertEqual(self.invoke(values)[0], 78)

    def test_arguments_cannot_activate(self):
        status, report = self.invoke(argv=['--enable-operational'])
        self.assertEqual((status, report['code']), (2, 'INVALID_ARGUMENTS'))

    def test_internal_error_sanitized(self):
        with patch('scripts.backup.orchestration.__main__.dispatch_context', side_effect=RuntimeError('private-content')):
            status, report = self.invoke()
        self.assertEqual((status, report['code']), (3, 'INTERNAL_GATE_ERROR'))
        self.assertNotIn('private-content', json.dumps(report))

    def test_unknown_failure_code_is_not_echoed(self):
        with patch('scripts.backup.orchestration.__main__.dispatch_context', side_effect=GateFailure('private-content')):
            status, report = self.invoke()
        self.assertEqual((status, report['code']), (3, 'INTERNAL_GATE_ERROR'))
        self.assertNotIn('private-content', json.dumps(report))

    def test_import_has_no_process_or_network_effect(self):
        from pathlib import Path
        from scripts.backup.orchestration import preflight, gates, __main__, posix_validation
        sources = [(module, compile(Path(module.__file__).read_text(), module.__file__, 'exec'))
                   for module in (preflight, gates, __main__, posix_validation)]
        with patch('subprocess.Popen', side_effect=AssertionError('process forbidden')), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('pathlib.Path.write_text', side_effect=AssertionError('write forbidden')), \
             patch('pathlib.Path.mkdir', side_effect=AssertionError('mkdir forbidden')), \
             patch.object(posix_validation, 'run_suite', side_effect=AssertionError('test execution forbidden')):
            for module, source in sources:
                exec(source, {'__name__': module.__name__, '__package__': module.__package__})
            self.assertEqual(self.invoke()[0], 78)

    def test_all_plausible_enable_flags_are_ignored_for_every_environment(self):
        for environment in ('lab', 'other', 'lab-other'):
            for flag in ('true', '1', 'APPLY', 'BACKUP'):
                values = context()
                values.update(BACKUP_ENVIRONMENT=environment, BACKUP_ENABLED=flag, GITHUB_ENV=flag,
                              OPERATIONAL_ALLOWED=flag, BACKUP_COMPLETE=flag, AWS_ROLE_ARN=flag)
                status, report = self.invoke(values)
                self.assertEqual(status, 78)
                self.assertIs(report['operational_allowed'], False)
                self.assertIs(report['backup_complete'], False)

    def test_capitalization_rejected_without_operational_result(self):
        for field, value, reason in (
            ('GITHUB_REF', 'refs/heads/Main', 'BRANCH_MISMATCH'),
            ('BACKUP_ENVIRONMENT', 'Lab', 'ENVIRONMENT_INVALID'),
            ('PROJECT_NAME', 'Fixture', 'CONCURRENCY_CONTEXT_MISSING'),
            ('BACKUP_CONFIRMATION', 'backup', 'CONFIRMATION_REQUIRED'),
        ):
            values = context()
            values[field] = value
            status, report = self.invoke(values)
            self.assertEqual((status, report['code']), (2, reason))
            self.assertIs(report['operational_allowed'], False)
            self.assertIs(report['backup_complete'], False)

    def test_context_diagnostics_never_echo_inputs(self):
        values = context()
        values['BACKUP_ENVIRONMENT'] = 'private-sensitive-input\n::error::injected'
        status, report = self.invoke(values)
        self.assertEqual((status, report['code']), (2, 'ENVIRONMENT_INVALID'))
        self.assertNotIn('private-sensitive-input', json.dumps(report))


def gate_case(field, value, code):
    def test(self):
        values = context()
        values[field] = value
        status, report = self.invoke(values)
        self.assertEqual((status, report['code']), (2, code))
    return test


for field, value, code in (
    ('GITHUB_EVENT_NAME', 'push', 'MANUAL_EVENT_REQUIRED'),
    ('GITHUB_EVENT_NAME', 'pull_request', 'MANUAL_EVENT_REQUIRED'),
    ('BACKUP_CONFIRMATION', '', 'CONFIRMATION_REQUIRED'),
    ('BACKUP_CONFIRMATION', 'APPLY', 'CONFIRMATION_REQUIRED'),
    ('GITHUB_REF', 'refs/heads/other', 'BRANCH_MISMATCH'),
    ('GITHUB_REF', 'refs/tags/main', 'BRANCH_MISMATCH'),
    ('DEPLOY_BRANCH', 'main..other', 'BRANCH_CONFIGURATION_INVALID'),
    ('DEPLOY_BRANCH', '.hidden', 'BRANCH_CONFIGURATION_INVALID'),
    ('DEPLOY_BRANCH', '', 'BRANCH_CONFIGURATION_INVALID'),
    ('GITHUB_WORKFLOW', 'Deploy platform (manual)', 'WORKFLOW_MISMATCH'),
    ('BACKUP_ENVIRONMENT', '', 'ENVIRONMENT_INVALID'),
    ('AWS_ACCOUNT_ID', '', 'CONCURRENCY_CONTEXT_MISSING'),
    ('PROJECT_NAME', '', 'CONCURRENCY_CONTEXT_MISSING'),
):
    setattr(GateTests, 'test_reject_' + field.lower() + '_' + str(len(GateTests.__dict__)), gate_case(field, value, code))


class PreflightTests(unittest.TestCase):
    def test_conformant_synthetic_observation_still_blocked(self):
        config, observed = observations()
        original = copy.deepcopy((config, observed))
        report = evaluate(config, observed, NOW)
        self.assertEqual(report['code'], 'SYNTHETIC_PREFLIGHT_CONFORMANT')
        self.assertFalse(report['authenticated_evidence'])
        self.assertFalse(report['operational_allowed'])
        self.assertFalse(report['backup_complete'])
        self.assertEqual((config, observed), original)

    def test_missing_field_rejected(self):
        config, observed = observations()
        observed.pop('age_verified')
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, 'PREFLIGHT_OBSERVATION_SHAPE')

    def test_secret_field_rejected_not_printed(self):
        config, observed = observations()
        observed['password'] = 'private-input'
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(str(caught.exception), 'PREFLIGHT_OBSERVATION_SHAPE')

    def test_equal_bucket_names_rejected(self):
        config, observed = observations()
        config['backups_bucket'] = config['data_bucket']
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, 'STORAGE_SEPARATION')

    def test_wrong_profile_account_rejected(self):
        config, observed = observations()
        config['instance_profile'] = config['instance_profile'].replace('000000000000', '111111111111')
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, 'INSTANCE_PROFILE_CONFIGURATION')

    def test_auxiliary_ec2_tags_are_compatible(self):
        config, observed = observations()
        observed['tags']['Name'] = 'fixture-lab'
        self.assertFalse(evaluate(config, observed, NOW)['operational_allowed'])

    def test_config_shape_is_closed(self):
        config, observed = observations()
        config['enabled'] = True
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, 'PREFLIGHT_CONFIGURATION_SHAPE')

    def test_observation_time_checks_do_not_mutate_maintenance(self):
        config, observed = observations()
        with patch('subprocess.Popen', side_effect=AssertionError('process forbidden')), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            report = evaluate(config, observed, NOW)
        self.assertEqual(report['synthetic_checks'], 'passed')
        self.assertIs(observed['maintenance'], False)

    def test_naive_check_time_rejected(self):
        config, observed = observations()
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW.replace(tzinfo=None))
        self.assertEqual(caught.exception.code, 'CHECK_TIME_INVALID')

    def test_multiple_identity_mismatches_have_deterministic_reason(self):
        config, observed = observations()
        observed.update(region='eu-west-1', account='111111111111', instance_id='i-11111111111111111')
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, 'TARGET_ACCOUNT_MISMATCH')


def preflight_case(field, value, code):
    def test(self):
        config, observed = observations()
        observed[field] = value
        with self.assertRaises(GateFailure) as caught:
            evaluate(config, observed, NOW)
        self.assertEqual(caught.exception.code, code)
    return test


for key in FLAGS:
    setattr(PreflightTests, 'test_missing_' + key, preflight_case(key, False, 'PREFLIGHT_' + key.upper()))
    setattr(PreflightTests, 'test_nonboolean_' + key, preflight_case(key, 1, 'PREFLIGHT_' + key.upper()))
for field, value, code in (
    ('synthetic', False, 'SYNTHETIC_OBSERVATION_REQUIRED'),
    ('instance_count', 0, 'INSTANCE_NOT_UNIQUE'), ('instance_count', 2, 'INSTANCE_NOT_UNIQUE'),
    ('instance_count', True, 'INSTANCE_NOT_UNIQUE'),
    ('account', '111111111111', 'TARGET_ACCOUNT_MISMATCH'),
    ('region', 'eu-west-1', 'TARGET_REGION_MISMATCH'),
    ('instance_id', 'i-11111111111111111', 'TARGET_INSTANCE_ID_MISMATCH'),
    ('data_bucket', 'other-data', 'TARGET_DATA_BUCKET_MISMATCH'),
    ('backups_bucket', 'other-backups', 'TARGET_BACKUPS_BUCKET_MISMATCH'),
    ('installation_identity_sha256', 'c' * 64, 'TARGET_INSTALLATION_IDENTITY_SHA256_MISMATCH'),
    ('repository_commit', 'd' * 40, 'TARGET_REPOSITORY_COMMIT_MISMATCH'),
    ('role_name', 'fixture-lab-github', 'BACKUP_ROLE_MISMATCH'),
    ('tags', {'Project': 'other', 'Environment': 'lab'}, 'TARGET_TAGS_MISMATCH'),
    ('maintenance', True, 'MAINTENANCE_ALREADY_ENABLED'),
    ('maintenance', 0, 'MAINTENANCE_ALREADY_ENABLED'),
    ('nextcloud_version', '35.0.0', 'PINNED_VERSION_MISMATCH'),
    ('postgres_major', 16, 'PINNED_VERSION_MISMATCH'),
    ('dump_client_major', 16, 'PINNED_VERSION_MISMATCH'),
    ('observed_at', '2026-02-30T00:00:00Z', 'OBSERVATION_TIME_INVALID'),
    ('observed_at', '2025-12-31T23:00:00Z', 'OBSERVATION_STALE_OR_FUTURE'),
    ('observed_at', '2026-01-01T00:00:01Z', 'OBSERVATION_STALE_OR_FUTURE'),
    ('observed_at', '2026-01-01T00:00:00+00:00', 'OBSERVATION_TIME_INVALID'),
    ('free_bytes', 1, 'INSUFFICIENT_STAGING_DISK'),
    ('free_bytes', True, 'RESOURCE_BUDGET_INVALID'),
    ('required_bytes', -1, 'RESOURCE_BUDGET_INVALID'),
    ('required_bytes', 2 ** 51, 'RESOURCE_BUDGET_INVALID'),
    ('session_seconds', 4000, 'SESSION_BUDGET_INSUFFICIENT'),
    ('session_seconds', 60, 'SESSION_BUDGET_INSUFFICIENT'),
):
    setattr(PreflightTests, 'test_reject_' + field + '_' + str(len(PreflightTests.__dict__)), preflight_case(field, value, code))


class PosixHarnessTests(unittest.TestCase):
    def test_windows_does_not_claim_posix_validation(self):
        with patch.object(posix.sys, 'platform', 'win32'):
            self.assertEqual(posix.platform_ready(), 'LINUX_PYTHON_REQUIRED')

    def test_exact_five_test_ids_exist(self):
        suite = unittest.TestLoader().loadTestsFromNames(posix.TEST_IDS)
        self.assertEqual(suite.countTestCases(), 5)
        for group in suite:
            for test in group:
                self.assertFalse(isinstance(test, unittest.loader._FailedTest))

    def fake_run(self, count=5, skips=None, failures=None, expected=None):
        result = unittest.TestResult()
        result.testsRun = count
        result.skipped = skips or []
        result.failures = failures or []
        result.expectedFailures = expected or []
        runner = unittest.TextTestRunner(stream=io.StringIO())
        with patch.object(runner, 'run', return_value=result):
            return posix.run_suite(runner=runner)

    def test_skips_block_validation(self):
        report, status = self.fake_run(skips=[('synthetic', 'not supported')])
        self.assertEqual((status, report['code']), (2, 'POSIX_VALIDATION_FAILED'))

    def test_missing_test_blocks_validation(self):
        self.assertEqual(self.fake_run(count=4)[1], 2)

    def test_expected_failure_cannot_pass_validation(self):
        report, status = self.fake_run(expected=[('fixture', 'private-error')])
        self.assertEqual((status, report['code']), (2, 'POSIX_VALIDATION_FAILED'))
        self.assertEqual(report['expected_failures'], 1)
        self.assertNotIn('private-error', json.dumps(report))

    def test_wrong_dependency_version_is_rejected(self):
        with patch.object(posix.importlib.util, 'find_spec', return_value=object()), \
             patch.object(posix.importlib.metadata, 'version', return_value='999.0.0'):
            self.assertEqual(posix.dependencies_ready(), 'DEPENDENCY_VERSION_MISMATCH')

    def test_dependency_metadata_missing_is_rejected(self):
        with patch.object(posix.importlib.util, 'find_spec', return_value=object()), \
             patch.object(posix.importlib.metadata, 'version', side_effect=posix.importlib.metadata.PackageNotFoundError):
            self.assertEqual(posix.dependencies_ready(), 'DEPENDENCY_MISSING_NO_INSTALL')

    def test_existing_exact_dependencies_pass_only_environment_check(self):
        with patch.object(posix.importlib.util, 'find_spec', return_value=object()), \
             patch.object(posix.importlib.metadata, 'version', side_effect=['4.23.0', '0.1.4']):
            self.assertIsNone(posix.dependencies_ready())

    def test_failure_sanitized(self):
        report, status = self.fake_run(failures=[('fixture', 'private-value')])
        self.assertEqual(status, 2)
        self.assertNotIn('private-value', json.dumps(report))

    def test_success_does_not_homologate_backup(self):
        report, status = self.fake_run()
        self.assertEqual(status, 0)
        self.assertFalse(report['operationally_homologated'])
        self.assertFalse(report['backup_complete'])

    def test_credentials_rejected_before_tests(self):
        with patch.object(posix.sys, 'platform', 'linux'), \
             patch.object(posix.Path, 'is_file', return_value=True), \
             patch.object(posix.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'), \
             patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'synthetic'}):
            self.assertEqual(posix.platform_ready(), 'CREDENTIAL_FREE_RUNNER_REQUIRED')

    def test_no_auto_dependency_install(self):
        with patch.object(posix.sys, 'platform', 'linux'), \
             patch.object(posix.Path, 'is_file', return_value=True), \
             patch.object(posix.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'), \
             patch.object(posix.Path, 'home', return_value=posix.Path('synthetic-home')), \
             patch.object(posix.Path, 'exists', return_value=False), \
             patch.dict(os.environ, {}, clear=True), patch.object(posix.importlib.util, 'find_spec', return_value=None):
            self.assertEqual(posix.platform_ready(), 'DEPENDENCY_MISSING_NO_INSTALL')

    def test_existing_aws_files_rejected_without_reading(self):
        with patch.object(posix.sys, 'platform', 'linux'), \
             patch.object(posix.Path, 'is_file', return_value=True), \
             patch.object(posix.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"\n'), \
             patch.object(posix.Path, 'home', return_value=posix.Path('synthetic-home')), \
             patch.object(posix.Path, 'exists', return_value=True), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(posix.platform_ready(), 'CREDENTIAL_FREE_RUNNER_REQUIRED')

    def test_invalid_arguments_do_not_echo(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            status = posix.main(['private-input'])
        self.assertEqual(status, 2)
        self.assertNotIn('private-input', output.getvalue())

    def test_environment_removed_and_restored_on_harness_failure(self):
        def fail_safely():
            self.assertNotIn('ACTIONS_RUNTIME_TOKEN', os.environ)
            raise RuntimeError('private-diagnostic')
        output = io.StringIO()
        with patch.dict(os.environ, {'ACTIONS_RUNTIME_TOKEN': 'synthetic-runtime-value'}), \
             patch.object(posix, 'platform_ready', return_value=None), \
             patch.object(posix, 'run_suite', side_effect=fail_safely), \
             patch('sys.stdout', output):
            status = posix.main([])
            self.assertEqual(os.environ['ACTIONS_RUNTIME_TOKEN'], 'synthetic-runtime-value')
        self.assertEqual(status, 3)
        self.assertNotIn('private-diagnostic', output.getvalue())


if __name__ == '__main__':
    unittest.main()
