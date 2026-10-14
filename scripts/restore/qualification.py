"""Manual Linux qualification of synthetic restore/reprovision tests only."""
import os
from pathlib import Path
import sys
import unittest

EXPECTED_TESTS=523
SAFE_TYPES=frozenset({'AssertionError','CaptureFailure','SealFailure','ValidationFailure',
                     'PermissionError','FileNotFoundError','TimeoutExpired','OSError',
                     'ImportError','ModuleNotFoundError','SyntaxError','ValueError','TypeError'})


class SanitizedResult(unittest.TestResult):
    def __init__(self):
        super().__init__();self.diagnostics=[]

    def record(self,test,error):
        kind=error[0].__name__
        self.diagnostics.append({'test':test.id(),
            'exception_type':kind if kind in SAFE_TYPES else 'OTHER_EXCEPTION'})

    def addError(self,test,error):
        self.record(test,error);super().addError(test,error)

    def addFailure(self,test,error):
        self.record(test,error);super().addFailure(test,error)


def run_tests(suite):
    result=SanitizedResult();suite.run(result)
    for d in result.diagnostics:
        print('RESTORE_TEST_REJECTED '+d['test']+' '+d['exception_type'])
    print('RESTORE_TESTS_RUN='+str(result.testsRun))
    print('RESTORE_TESTS_FAILURES='+str(len(result.failures)))
    print('RESTORE_TESTS_ERRORS='+str(len(result.errors)))
    print('RESTORE_TESTS_SKIPS='+str(len(result.skipped)))
    print('RESTORE_TESTS_EXPECTED_FAILURES='+str(len(result.expectedFailures)))
    print('RESTORE_TESTS_UNEXPECTED_SUCCESSES='+str(len(result.unexpectedSuccesses)))
    if (result.testsRun!=EXPECTED_TESTS or result.failures or result.errors or result.skipped
            or result.expectedFailures or result.unexpectedSuccesses):
        raise SystemExit('RESTORE_QUALIFICATION_FAILED')
    print('RESTORE_OFFLINE_LINUX_TESTS_PASSED; RESTORE_PROVEN=false')


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if argv:raise SystemExit('RESTORE_QUALIFICATION_ARGUMENTS_REJECTED')
    from scripts.backup.orchestration.linux.prepare_posix import check_platform
    try:check_platform()
    except Exception:raise SystemExit('RESTORE_QUALIFICATION_PLATFORM_REJECTED') from None
    if sys.version_info[:2] != (3,12):raise SystemExit('RESTORE_QUALIFICATION_PYTHON_REJECTED')
    if os.name!='posix' or os.geteuid()!=0:raise SystemExit('RESTORE_QUALIFICATION_ROOT_REQUIRED')
    root=Path(__file__).resolve().parents[2]
    suite=unittest.defaultTestLoader.discover(str(root/'scripts/restore/tests'),top_level_dir=str(root))
    run_tests(suite)


if __name__=='__main__':main()
