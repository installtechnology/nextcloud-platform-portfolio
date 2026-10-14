"""Local negative tests for the qualification gate; no platform installation."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from types import SimpleNamespace

from scripts.backup.qualification import pr7_linux as harness


class QualificationTests(unittest.TestCase):
    def test_operational_store_rejects_nonroot_before_filesystem_access(self):
        import os
        from scripts.backup.capture import storage
        with patch.object(storage, 'os', SimpleNamespace(name='posix', geteuid=lambda: 1001, path=os.path)):
            with self.assertRaisesRegex(storage.CaptureFailure, '^LINUX_ROOT_REQUIRED$'):
                storage.Store('synthetic-unused-path')

    def test_harness_root_gate_is_explicit(self):
        output = io.StringIO()
        with patch.object(harness, 'check_platform'), patch.object(harness.os, 'geteuid', return_value=1001, create=True), redirect_stdout(output):
            self.assertEqual(harness.main([]), 3)
        self.assertIn('LINUX_ROOT_REQUIRED', output.getvalue())

    def test_exception_diagnostics_do_not_print_values(self):
        from scripts.backup.capture.storage import CaptureFailure
        class FailingSetup(unittest.TestCase):
            def setUp(self):
                raise CaptureFailure('LINUX_ROOT_REQUIRED')
            def runTest(self):
                pass
        class PrivateFailure(unittest.TestCase):
            def runTest(self):
                raise PermissionError('synthetic-private-text')
        class UnknownCode(unittest.TestCase):
            def runTest(self):
                raise CaptureFailure('synthetic-private-text')
        runner = unittest.TextTestRunner(stream=io.StringIO(), resultclass=harness.SanitizedResult)
        first = runner.run(FailingSetup()).diagnostics
        self.assertEqual(first, [{'phase': 'setup', 'exception_type': 'CaptureFailure', 'reason': 'LINUX_ROOT_REQUIRED'}])
        self.assertNotIn('synthetic-private-text', str(runner.run(PrivateFailure()).diagnostics))
        self.assertEqual(runner.run(UnknownCode()).diagnostics[0]['reason'], 'TEST_EXCEPTION')

    def result(self, **changes):
        data = dict(testsRun=1, skipped=[], failures=[], errors=[], expectedFailures=[], unexpectedSuccesses=[])
        data.update(changes)
        return Mock(**data, wasSuccessful=Mock(return_value=True))

    def run_result(self, result):
        return harness.run_tests(Mock(), Mock(run=Mock(return_value=result)))

    def test_exact_three_successes(self):
        report, status = self.run_result(self.result())
        self.assertEqual(status, 0)
        self.assertEqual(len(report['tests']), 3)
        self.assertFalse(report['backup_complete'])

    def test_each_ambiguous_outcome_rejected(self):
        for changes in ({'skipped': [1]}, {'expectedFailures': [1]}, {'unexpectedSuccesses': [1]},
                        {'testsRun': 0}, {'testsRun': 2}):
            with self.subTest(changes=changes):
                report, status = self.run_result(self.result(**changes))
                self.assertEqual(status, 2)
                self.assertFalse(report['backup_complete'])

    def test_failed_result_rejected(self):
        result = self.result(failures=[('test', 'synthetic-private-text')])
        result.wasSuccessful.return_value = False
        report, status = self.run_result(result)
        self.assertEqual(status, 2)
        self.assertNotIn('synthetic-private-text', str(report))

    def test_arguments_rejected_before_platform(self):
        with patch.object(harness, 'check_platform') as check, redirect_stdout(io.StringIO()):
            self.assertEqual(harness.main(['enable-operational']), 3)
        check.assert_not_called()

    def test_platform_error_sanitized(self):
        output = io.StringIO()
        with patch.object(harness, 'check_platform', side_effect=RuntimeError('synthetic-private-text')), redirect_stdout(output):
            self.assertEqual(harness.main([]), 3)
        self.assertNotIn('synthetic-private-text', output.getvalue())


if __name__ == '__main__':
    unittest.main()
