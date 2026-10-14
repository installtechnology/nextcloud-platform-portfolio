"""Exercise the PHP installation state machine with isolated adapters, never AWS or a real DB.

The official command/PDO/S3 adapters require integration validation after deployment authorization.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

PHP = str(Path(sys.argv.pop(1)).resolve())
DOCKER = Path(__file__).resolve().parents[1]


class InitialInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.state = dict(installed=False, storage_matches=True, install_config_matches=True)
        self.options = dict(database_empty=True, bucket_empty=True, local_data_safe=True,
                            rc=0, confirm=True, guard=True, exception=False, warning='', terminate=False)
        self.harness = self.path / 'test.php'
        self.harness.write_text('''<?php
require ''' + json.dumps(str(DOCKER / 'install-nextcloud.php')) + ''';
$path=__DIR__; $state=json_decode(file_get_contents($path.'/state.json'),true);
$options=json_decode(file_get_contents($path.'/options.json'),true); $calls=[];
$adapter=[
    'guard'=>function() use (&$calls,$options) { $calls[]='guard'; if (!$options['guard']) { throw new PlatformInstallFailure('STORAGE_GUARD_FAILED'); } },
    'state'=>function() use (&$calls,&$state) { $calls[]='state'; return $state; },
    'local_data_safe'=>function() use (&$calls,$options) { $calls[]='local_data'; return $options['local_data_safe']; },
    'database_empty'=>function() use (&$calls,$options) { $calls[]='database'; return $options['database_empty']; },
    'bucket_empty'=>function() use (&$calls,$options) { $calls[]='bucket'; return $options['bucket_empty']; },
    'install'=>function() use (&$calls,&$state,$options) {
        $calls[]='install'; echo 'fixture-sensitive-password';
        if ($options['terminate']) { exit(0); }
        if ($options['warning']==='suppressed') { @trigger_error('fixture-sensitive-password', E_USER_WARNING); }
        if ($options['warning']==='visible') { trigger_error('fixture-sensitive-password', E_USER_WARNING); }
        if ($options['exception']) { throw new RuntimeException('fixture-sensitive-password'); }
        if ($options['confirm']) { $state['installed']=true; }
        return $options['rc'];
    },
];
$rc=runPlatformInstallation($adapter,$path);
file_put_contents($path.'/state.json',json_encode($state));
file_put_contents($path.'/calls.json',json_encode($calls));
exit($rc);
''', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self):
        (self.path / 'state.json').write_text(json.dumps(self.state))
        (self.path / 'options.json').write_text(json.dumps(self.options))
        result = subprocess.run([PHP, '-n', str(self.harness)], capture_output=True)
        self.state = json.loads((self.path / 'state.json').read_text())
        calls = json.loads((self.path / 'calls.json').read_text())
        self.assertNotIn(b'fixture-sensitive-password', result.stdout + result.stderr)
        return result, calls

    def test_fresh_install_storage_and_read_only_checks_before_command(self):
        result, calls = self.run_install()
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'INSTALLED', result.stdout)
        self.assertEqual(calls, ['guard', 'state', 'local_data', 'database', 'bucket', 'install', 'state'])
        self.assertTrue(self.state['installed'])

    def test_interrupted_file_copy_with_empty_db_resumes(self):
        (self.path / 'version.php').write_text('<?php $OC_Version=[34,0,4,1];')
        (self.path / 'config.php').write_text("<?php $CONFIG=['installed'=>false];")
        self.test_fresh_install_storage_and_read_only_checks_before_command()

    def test_second_run_does_not_install_or_read_passwords(self):
        self.run_install()
        result, calls = self.run_install()
        self.assertEqual((result.returncode, result.stdout), (0, b'Platform install: ALREADY_INSTALLED\n'))
        self.assertEqual(calls, ['guard', 'state'])

    def test_partial_database_blocks_install_without_retry(self):
        self.options['database_empty'] = False
        for _ in range(2):
            result, calls = self.run_install()
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'DATABASE_NOT_EMPTY', result.stderr)
            self.assertNotIn('install', calls)
            self.assertNotIn('bucket', calls)

    def test_installed_config_change_aborts_without_reinstall(self):
        self.state.update(installed=True, install_config_matches=False)
        result, calls = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'INSTALL_CONFIG_MISMATCH', result.stderr)
        self.assertEqual(calls, ['guard', 'state'])

    def test_existing_s3_objects_block_install(self):
        self.options['bucket_empty'] = False
        result, calls = self.run_install()
        self.assertIn(b'DATA_BUCKET_NOT_EMPTY', result.stderr)
        self.assertNotIn('install', calls)

    def test_existing_local_data_blocks_install(self):
        self.options['local_data_safe'] = False
        result, calls = self.run_install()
        self.assertIn(b'LOCAL_DATA_NOT_EMPTY', result.stderr)
        self.assertNotIn('install', calls)

    def test_storage_and_db_config_incompatibility_fail_closed(self):
        for field, reason in [('storage_matches', b'STORAGE_CONFIG_MISMATCH'),
                              ('install_config_matches', b'INSTALL_CONFIG_MISMATCH')]:
            self.state[field] = False
            result, calls = self.run_install()
            self.assertIn(reason, result.stderr)
            self.assertNotIn('install', calls)
            self.state[field] = True

    def test_guard_is_never_bypassed_even_for_installed_volume(self):
        self.state['installed'] = True
        self.options['guard'] = False
        result, calls = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ['guard'])

    def test_failed_install_has_no_retry_and_sanitized_exception(self):
        self.options['exception'] = True
        result, calls = self.run_install()
        self.assertIn(b'INITIAL_INSTALL_ERROR', result.stderr)
        self.assertEqual(calls.count('install'), 1)
        self.assertFalse(self.state['installed'])

    def test_exit_code_and_post_install_confirmation(self):
        self.options.update(rc=1, confirm=False)
        result, _ = self.run_install()
        self.assertIn(b'INSTALL_FAILED', result.stderr)
        self.options['rc'] = 0
        result, _ = self.run_install()
        self.assertIn(b'INSTALL_NOT_CONFIRMED', result.stderr)

    def test_failed_command_cannot_announce_success_even_if_flag_was_written(self):
        self.options.update(rc=1, confirm=True)
        result, _ = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'Platform install: INSTALL_FAILED\n')
        # The wrapper does not fabricate an installation-success marker.
        self.assertEqual({p.name for p in self.path.glob('.platform-*')}, {'.platform-install.lock'})

    def test_concurrent_invocations_serialize_and_second_does_not_reinstall(self):
        (self.path / 'installed').write_text('false')
        harness = self.path / 'concurrent.php'
        harness.write_text('<?php require ' + json.dumps(str(DOCKER / 'install-nextcloud.php')) + ''';
$adapter=[
 'guard'=>function() {},
 'state'=>function() { return ['installed'=>file_get_contents(__DIR__.'/installed')==='true',
   'storage_matches'=>true,'install_config_matches'=>true]; },
 'local_data_safe'=>fn()=>true, 'database_empty'=>fn()=>true, 'bucket_empty'=>fn()=>true,
 'install'=>function() {
   file_put_contents(__DIR__.'/entered','1');
   usleep(700000);
   file_put_contents(__DIR__.'/attempts','1',FILE_APPEND);
   file_put_contents(__DIR__.'/installed','true'); return 0;
 },
];
exit(runPlatformInstallation($adapter,__DIR__));
''', encoding='utf-8')
        first = subprocess.Popen([PHP, '-n', str(harness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = None
        try:
            deadline = time.monotonic() + 20
            while not (self.path / 'entered').exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.path / 'entered').exists(), 'first installer did not acquire lock')
            second = subprocess.Popen([PHP, '-n', str(harness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out1, err1 = first.communicate(timeout=30)
            out2, err2 = second.communicate(timeout=30)
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            self.assertEqual((out1, out2), (b'Platform install: INSTALLED\n', b'Platform install: ALREADY_INSTALLED\n'))
            self.assertEqual(err1 + err2, b'')
            self.assertEqual((self.path / 'attempts').read_text(), '1')
        finally:
            for process in (first, second):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_vendor_suppression_and_unsuppressed_warning(self):
        self.options['warning'] = 'visible'
        result, _ = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'INITIAL_INSTALL_ERROR', result.stderr)
        self.options['warning'] = 'suppressed'
        result, _ = self.run_install()
        self.assertEqual(result.returncode, 0)

    def test_exit_zero_cannot_bypass_confirmation_or_leak_output(self):
        self.options['terminate'] = True
        (self.path / 'state.json').write_text(json.dumps(self.state))
        (self.path / 'options.json').write_text(json.dumps(self.options))
        result = subprocess.run([PHP, '-n', str(self.harness)], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'fixture-sensitive-password', result.stderr)
        self.assertFalse(json.loads((self.path / 'state.json').read_text())['installed'])

    def test_production_credentials_do_not_use_process_arguments(self):
        source = (DOCKER / 'install-nextcloud.php').read_text()
        self.assertIn('Input\\ArrayInput', source)
        self.assertIn('Output\\NullOutput', source)
        self.assertNotIn('shell_exec', source)
        self.assertNotIn('passthru', source)
        self.assertIn("proc_open([PHP_BINARY, __DIR__ . '/storage-guard.php']", source)
        self.assertIn("$db->exec('BEGIN READ ONLY')", source)
        self.assertNotIn('deleteObject', source)
        self.assertNotIn('DROP ', source)

    def test_healthcheck_before_after_and_incompatible_status(self):
        harness = self.path / 'health.php'
        harness.write_text('<?php require ' + json.dumps(str(DOCKER / 'nextcloud-healthcheck.php')) + ''';
$statuses=[['installed'=>false,'maintenance'=>false,'needsDbUpgrade'=>false],
 ['installed'=>true,'maintenance'=>false,'needsDbUpgrade'=>false],
 ['installed'=>true,'maintenance'=>true,'needsDbUpgrade'=>false],
 ['installed'=>true,'maintenance'=>false,'needsDbUpgrade'=>true],
 ['installed'=>'false','maintenance'=>false,'needsDbUpgrade'=>false],null];
echo json_encode(array_map('platformNextcloudReady',$statuses));
''', encoding='utf-8')
        result = subprocess.run([PHP, '-n', str(harness)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [False, True, False, False, False, False])

    def test_local_data_scaffolding_must_be_regular_files(self):
        data = self.path / 'data'
        data.mkdir()
        (data / 'index.html').write_text('fixture')
        check = self.path / 'data.php'
        check.write_text('<?php require ' + json.dumps(str(DOCKER / 'install-nextcloud.php'))
                         + '; exit(platformLocalDataSafe(__DIR__ . "/data") ? 0 : 1);')
        self.assertEqual(subprocess.run([PHP, '-n', str(check)], capture_output=True).returncode, 0)
        (data / 'index.html').unlink()
        (data / 'index.html').mkdir()
        self.assertEqual(subprocess.run([PHP, '-n', str(check)], capture_output=True).returncode, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
