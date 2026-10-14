"""Offline harness tests. No qualification platform, packages or remote access."""
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from scripts.restore import qualification as q


class Harness(unittest.TestCase):
    def execute(self,result):
        suite=SimpleNamespace(run=lambda received:received.__dict__.update(result))
        output=io.StringIO()
        with redirect_stdout(output):q.run_tests(suite)
        return output.getvalue()
    def result(self,**changes):
        result=dict(testsRun=523,failures=[],errors=[],skipped=[],expectedFailures=[],unexpectedSuccesses=[])
        result.update(changes);return result
    def test_exact_success(self):
        output=self.execute(self.result())
        self.assertIn('RESTORE_OFFLINE_LINUX_TESTS_PASSED; RESTORE_PROVEN=false',output)
        self.assertIn('RESTORE_TESTS_RUN=523',output)
    def test_wrong_count_rejected(self):
        for count in (0,407,425,446,448,503,507,508,512,522,524):
            with self.subTest(count=count),self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_FAILED$'):
                self.execute(self.result(testsRun=count))
    def test_all_non_success_outcomes_rejected(self):
        for field in ('failures','errors','skipped','expectedFailures','unexpectedSuccesses'):
            with self.subTest(field=field),self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_FAILED$'):
                self.execute(self.result(**{field:[1]}))
    def test_private_exception_values_never_printed(self):
        class Private(unittest.TestCase):
            def runTest(self):raise ValueError('synthetic-private-credential')
        output=io.StringIO()
        with redirect_stdout(output),self.assertRaises(SystemExit):q.run_tests(unittest.TestSuite([Private()]))
        self.assertNotIn('synthetic-private-credential',output.getvalue())
        self.assertIn('ValueError',output.getvalue())
    def test_arguments_rejected_before_platform(self):
        with patch('scripts.backup.orchestration.linux.prepare_posix.check_platform') as check:
            with self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_ARGUMENTS_REJECTED$'):q.main(['activate'])
        check.assert_not_called()
    def test_nonroot_rejected_before_discovery(self):
        with patch('scripts.backup.orchestration.linux.prepare_posix.check_platform'),\
             patch.object(q,'sys',SimpleNamespace(version_info=(3,12))),\
             patch.object(q,'os',SimpleNamespace(name='posix',geteuid=lambda:1000)),\
             patch.object(q.unittest.defaultTestLoader,'discover') as discovery:
            with self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_ROOT_REQUIRED$'):q.main([])
        discovery.assert_not_called()
    def test_bad_python_rejected(self):
        with patch('scripts.backup.orchestration.linux.prepare_posix.check_platform'),\
             patch.object(q,'sys',SimpleNamespace(version_info=(3,11))):
            with self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_PYTHON_REJECTED$'):q.main([])
    def test_platform_failure_sanitized(self):
        with patch('scripts.backup.orchestration.linux.prepare_posix.check_platform',side_effect=ValueError('private')):
            with self.assertRaisesRegex(SystemExit,'^RESTORE_QUALIFICATION_PLATFORM_REJECTED$'):q.main([])
    def test_workflow_stays_manual_and_credential_free(self):
        import yaml
        root=Path(__file__).resolve().parents[4]
        workflow=yaml.safe_load((root/'.github/workflows/backup-pr7-linux-validation.yml').read_text())
        self.assertEqual(set(workflow[True]),{'workflow_dispatch'})
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        job=workflow['jobs']['qualification']
        self.assertNotIn('environment',job)
        self.assertEqual(job['runs-on'],'ubuntu-24.04')
        self.assertIn("github.ref == 'refs/heads/main'",job['if'])
        self.assertIn("inputs.confirmation == 'VALIDATE_PR7_LINUX'",job['if'])
        text=(root/'.github/workflows/backup-pr7-linux-validation.yml').read_text()
        self.assertNotIn('${{ secrets.',text);self.assertNotIn('id-token:',text)
    def test_root_step_preserves_venv_and_syntax_check_only(self):
        import yaml
        root=Path(__file__).resolve().parents[4]
        workflow=yaml.safe_load((root/'.github/workflows/backup-pr7-linux-validation.yml').read_text())
        steps=workflow['jobs']['qualification']['steps']
        restore=next(s for s in steps if 'scripts.restore.qualification' in s.get('run',''))
        self.assertIn('sudo -n env -i',restore['run'])
        self.assertIn('"$RUNNER_TEMP/nextcloud-operational-qualification/venv/bin/python" -m scripts.restore.qualification',restore['run'])
        syntax=next(s for s in steps if 'creation-handoff-prepare.yml' in s.get('run',''))
        line=next(l for l in syntax['run'].splitlines() if 'creation-handoff-prepare.yml' in l)
        self.assertTrue(line.endswith('--syntax-check'))


if __name__=='__main__':unittest.main()
