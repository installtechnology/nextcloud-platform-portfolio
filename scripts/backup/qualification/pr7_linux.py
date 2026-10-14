"""Exactly three synthetic kernel tests, never a capture or host deployment."""
import importlib.metadata
import io
import json
import os
import sys
import unittest

from ..operational.dependencies import audit
from ..orchestration.linux.prepare_posix import check_platform

PREFIX = 'scripts.backup.operational.tests.test_integration.LinuxKernelQualification.'
TEST_IDS = tuple(PREFIX + name for name in (
    'test_kernel_flock_owner_is_actual_process',
    'test_independent_process_cannot_enter_held_lock',
    'test_sigkill_of_disposable_child_leaves_durable_deploy_block',
))

SAFE_CODES = frozenset({'LINUX_ROOT_REQUIRED', 'PRIVATE_STAGING_REQUIRED', 'UNSAFE_LOCK',
                       'UNSAFE_LOCK_PERMISSIONS', 'CAPTURE_BUSY', 'DEPLOY_LEASE_RECOVERY_REQUIRED',
                       'ADMIN_KERNEL_LOCK_REQUIRED', 'UNSAFE_PATH'})
SAFE_TYPES = frozenset({'AssertionError', 'ImportError', 'ModuleNotFoundError', 'PermissionError',
                       'FileNotFoundError', 'TimeoutExpired', 'OSError', 'SyntaxError', 'CaptureFailure'})


class SanitizedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = []

    def diagnostic(self, error):
        kind, exception, traceback = error
        phase = 'test'
        while traceback:
            if traceback.tb_frame.f_code.co_name in ('setUp', 'setUpClass'):
                phase = 'setup'
            traceback = traceback.tb_next
        code = getattr(exception, 'code', None)
        self.diagnostics.append({'phase': phase,
                                 'exception_type': kind.__name__ if kind.__name__ in SAFE_TYPES else 'OTHER_EXCEPTION',
                                 'reason': code if isinstance(code, str) and code in SAFE_CODES else 'TEST_EXCEPTION'})

    def addError(self, test, error):
        self.diagnostic(error)
        super().addError(test, error)

    def addFailure(self, test, error):
        self.diagnostic(error)
        super().addFailure(test, error)


def run_tests(loader=None, runner=None):
    loader = loader or unittest.TestLoader()
    runner = runner or unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, resultclass=SanitizedResult)
    reports = []
    for test_id in TEST_IDS:
        result = runner.run(loader.loadTestsFromName(test_id))
        ok = (result.testsRun == 1 and result.wasSuccessful() and not result.skipped
              and not result.expectedFailures and not result.unexpectedSuccesses)
        reports.append({'test': test_id, 'passed': bool(ok), 'tests_run': result.testsRun,
                        'failures': len(result.failures), 'errors': len(result.errors),
                        'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
                        'unexpected_successes': len(result.unexpectedSuccesses),
                        'diagnostics': result.diagnostics if isinstance(getattr(result, 'diagnostics', None), list) else []})
    passed = len(reports) == 3 and all(r['passed'] for r in reports)
    return {'code': 'THREE_PR7_LINUX_TESTS_PASSED' if passed else 'PR7_LINUX_TESTS_FAILED',
            'tests': reports, 'backup_complete': False, 'operationally_homologated': False}, 0 if passed else 2


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        if argv:
            raise ValueError('arguments rejected')
        check_platform()
        if os.geteuid() != 0:
            print(json.dumps({'code': 'LINUX_ROOT_REQUIRED', 'backup_complete': False,
                              'operationally_homologated': False}))
            return 3
        records = audit('qualification')
        versions = {r['name']: importlib.metadata.version(r['name']) for r in records}
        if any(versions[r['name']] != r['version'] for r in records):
            raise ValueError('dependency version rejected')
        print(json.dumps({'code': 'QUALIFICATION_ENVIRONMENT_VERIFIED', 'ubuntu': '24.04',
                          'python': sys.version.split()[0], 'euid': os.geteuid(), 'dependencies': versions}, sort_keys=True))
        report, status = run_tests()
    except Exception:
        report, status = {'code': 'PR7_QUALIFICATION_PREFLIGHT_FAILED', 'backup_complete': False,
                          'operationally_homologated': False}, 3
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
