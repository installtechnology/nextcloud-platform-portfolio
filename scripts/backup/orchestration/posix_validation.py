"""Opt-in isolated Linux test harness; no installs, AWS, Docker or application."""
import importlib.util
import importlib.metadata
import io
import json
import os
from pathlib import Path
import sys
import unittest

TEST_IDS = (
    'scripts.backup.tests.test_validator.ValidatorTests.test_actual_concurrent_artifact_truncation',
    'scripts.backup.tests.test_validator.ValidatorTests.test_real_fifo_and_socket_rejected_without_read',
    'scripts.backup.capture.tests.test_process.TransportTests.test_actual_child_environment_has_no_inherited_credentials',
    'scripts.backup.capture.tests.test_process.TransportTests.test_actual_child_input_and_output',
    'scripts.backup.capture.tests.test_process.TransportTests.test_actual_child_timeout',
)
DEPENDENCIES = {'jsonschema': ('jsonschema', '4.23.0'),
                'rfc3339_validator': ('rfc3339-validator', '0.1.4')}


def dependencies_ready():
    for module, (distribution, expected) in DEPENDENCIES.items():
        if importlib.util.find_spec(module) is None:
            return 'DEPENDENCY_MISSING_NO_INSTALL'
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return 'DEPENDENCY_MISSING_NO_INSTALL'
        if actual != expected:
            return 'DEPENDENCY_VERSION_MISMATCH'
    return None


def platform_ready():
    if sys.platform != 'linux' or sys.version_info < (3, 11):
        return 'LINUX_PYTHON_REQUIRED'
    if not Path('/etc/os-release').is_file():
        return 'UBUNTU_2404_REQUIRED'
    fields = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if fields.get('ID', '').strip('"') != 'ubuntu' or fields.get('VERSION_ID', '').strip('"') != '24.04':
        return 'UBUNTU_2404_REQUIRED'
    forbidden = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE',
                 'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_DEFAULT_PROFILE',
                 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                 'ACTIONS_ID_TOKEN_REQUEST_URL', 'SSH_PRIVATE_KEY', 'NEXTCLOUD_ADMIN_PASSWORD',
                 'POSTGRES_ADMIN_PASSWORD', 'POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'GRAFANA_ADMIN_PASSWORD')
    if any(os.environ.get(key) for key in forbidden):
        return 'CREDENTIAL_FREE_RUNNER_REQUIRED'
    if any((Path.home() / '.aws' / name).exists() for name in ('credentials', 'config')):
        return 'CREDENTIAL_FREE_RUNNER_REQUIRED'
    return dependencies_ready()


def run_suite(loader=None, runner=None):
    loader = loader or unittest.TestLoader()
    suite = loader.loadTestsFromNames(TEST_IDS)
    # Never publish exception bodies or fixture contents, even if a test regresses.
    runner = runner or unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
    result = runner.run(suite)
    ok = (result.testsRun == 5 and result.wasSuccessful() and not result.skipped
          and not result.expectedFailures and not result.unexpectedSuccesses)
    return {'code': 'FIVE_POSIX_TESTS_PASSED' if ok else 'POSIX_VALIDATION_FAILED',
            'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skips': len(result.skipped), 'backup_complete': False,
            'expected_failures': len(result.expectedFailures), 'unexpected_successes': len(result.unexpectedSuccesses),
            'operationally_homologated': False, 'functional_restore': 'not_verified'}, 0 if ok else 2


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        error = 'INVALID_ARGUMENTS' if argv else platform_ready()
        if error:
            report, status = {'code': error, 'operationally_homologated': False}, 2
        else:
            # Isolate child process tests from unrelated runner configuration.
            permitted = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE'}
            previous = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update({k: v for k, v in previous.items() if k in permitted})
                report, status = run_suite()
            finally:
                os.environ.clear()
                os.environ.update(previous)
    except Exception:
        report, status = {'code': 'POSIX_HARNESS_ERROR', 'operationally_homologated': False}, 3
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
