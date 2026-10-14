"""Offline regression tests: real PHP processes, temporary config, no AWS/Docker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PHP = str(Path(sys.argv.pop(1)).resolve())
DOCKER = Path(__file__).resolve().parents[1]


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.marker = self.directory / '.platform-storage.json'
        # Discard external overrides so these tests never depend on local credentials/config.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('NC_', 'NEXTCLOUD_', 'OBJECTSTORE_', 'REDIS_', 'SMTP_', 'SWIFT_'))}
        self.env.update(NEXTCLOUD_S3_BUCKET='fixture-nextcloud-data', NEXTCLOUD_S3_REGION='us-east-1')

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, php):
        (self.directory / name).write_text('<?php ' + php, encoding='utf-8')

    def run_guard(self, **overrides):
        return subprocess.run([PHP, '-n', str(DOCKER / 'storage-guard.php'), str(self.directory)],
                              env=dict(self.env, **overrides), capture_output=True)

    def expect_failure(self, reason, **overrides):
        before = self.marker.read_bytes() if self.marker.exists() else None
        result = self.run_guard(**overrides)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertIn(reason.encode(), result.stderr)
        self.assertEqual(self.marker.read_bytes() if self.marker.exists() else None, before)

    def installed_s3(self):
        self.assertEqual(self.run_guard().returncode, 0)
        self.write('config.php', "$CONFIG = ['installed' => true];")
        shutil.copyfile(DOCKER / 'platform.config.php', self.directory / 'zz-platform.config.php')

    def test_fresh_without_config_php(self):
        result = self.run_guard()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        self.assertFalse((self.directory / 'config.php').exists())
        identity = json.loads(self.marker.read_text())
        self.assertEqual(identity['arguments']['bucket'], 'fixture-nextcloud-data')
        self.assertNotIn('key', identity['arguments'])
        self.assertNotIn('secret', identity['arguments'])

    def test_apps_config_context_is_available_without_bootstrap(self):
        self.write('apps.config.php', """
if (OC::$SERVERROOT !== '/var/www/html' || OC::$configDir !== __DIR__ . '/') {
    throw new RuntimeException('Incorrect path context');
}
$CONFIG = ['apps_paths' => [
    ['path' => OC::$SERVERROOT . '/apps', 'url' => '/apps', 'writable' => false],
    ['path' => OC::$SERVERROOT . '/custom_apps', 'url' => '/custom_apps', 'writable' => true]
]];
""")
        self.assertEqual(self.run_guard().returncode, 0)

    def test_installed_s3_is_idempotent(self):
        self.installed_s3()
        before = (self.marker.read_bytes(), self.marker.stat().st_mtime_ns)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual((self.marker.read_bytes(), self.marker.stat().st_mtime_ns), before)

    def test_local_installation_without_marker_is_refused(self):
        self.write('config.php', "$CONFIG = ['installed' => true];")
        self.expect_failure('INSTALLED_WITHOUT_STORAGE_MARKER')

    def test_local_installation_with_marker_is_refused(self):
        self.assertEqual(self.run_guard().returncode, 0)
        self.write('config.php', "$CONFIG = ['installed' => true];")
        self.expect_failure('OBJECTSTORE_CONFIG_MISMATCH')

    def test_bucket_and_region_changes_are_refused(self):
        self.installed_s3()
        self.expect_failure('MARKER_STORAGE_MISMATCH', NEXTCLOUD_S3_BUCKET='other-fixture-data')
        self.expect_failure('MARKER_STORAGE_MISMATCH', NEXTCLOUD_S3_REGION='eu-west-1')

    def test_invalid_expected_config_is_refused(self):
        self.expect_failure('EXPECTED_STORAGE_CONFIG_ERROR', NEXTCLOUD_S3_BUCKET='')
        self.expect_failure('EXPECTED_STORAGE_CONFIG_ERROR', NEXTCLOUD_S3_REGION='invalid')

    def test_invalid_marker_is_refused(self):
        self.marker.write_text('invalid-json', encoding='utf-8')
        self.expect_failure('MARKER_READ_ERROR')

    def test_partial_install_with_conflicting_storage_is_refused(self):
        self.write('config.php', "$CONFIG = ['objectstore' => ['class' => 'OtherStorage']];")
        self.expect_failure('OBJECTSTORE_CONFIG_MISMATCH')

    def test_natural_order_last_file_wins(self):
        self.installed_s3()
        self.write('zz2.config.php', "$CONFIG = ['objectstore' => ['arguments' => ['bucket' => 'wrong-fixture-data']]];")
        self.write('zz10.config.php', "$CONFIG = ['objectstore' => ['arguments' => ['bucket' => 'fixture-nextcloud-data']]];")
        self.assertEqual(self.run_guard().returncode, 0)
        self.write('zz10.config.php', "$CONFIG = ['objectstore' => ['arguments' => ['bucket' => 'wrong-fixture-data']]];")
        self.expect_failure('OBJECTSTORE_CONFIG_MISMATCH')

    def test_recursive_merge_cannot_erase_static_credentials(self):
        self.installed_s3()
        self.write('config.php', "$CONFIG = ['installed' => true, 'objectstore' => ['arguments' => ['key' => 'fixture-sensitive-value']]];")
        self.expect_failure('OBJECTSTORE_CONFIG_MISMATCH')
        self.assertNotIn(b'fixture-sensitive-value', self.run_guard().stderr)

    def test_environment_cannot_hide_existing_installation(self):
        self.write('config.php', "$CONFIG = ['installed' => true];")
        self.expect_failure('INSTALLED_WITHOUT_STORAGE_MARKER', NC_installed='0')
        (self.directory / 'config.php').unlink()
        self.expect_failure('OBJECTSTORE_CONFIG_MISMATCH', NC_objectstore='{}')

    def test_config_exception_and_output_are_sanitized(self):
        for body in ("throw new RuntimeException('fixture-sensitive-value');",
                     "echo 'fixture-sensitive-value'; $CONFIG = [];",
                     "trigger_error('fixture-sensitive-value', E_USER_WARNING);"):
            self.write('broken.config.php', body)
            self.expect_failure('CONFIG_LOAD_ERROR')
            self.assertNotIn(b'fixture-sensitive-value', self.run_guard().stderr)

    def test_unsupported_context_is_not_silently_skipped(self):
        self.write('other.config.php', "$CONFIG = ['x' => OC::unsupportedMethod()];")
        self.expect_failure('CONFIG_LOAD_ERROR')

    def test_config_exit_cannot_bypass_guard(self):
        self.write('exit.config.php', "echo 'fixture-sensitive-value'; exit(0);")
        self.expect_failure('CONFIG_LOAD_ERROR')

    def test_missing_directory_cannot_create_marker(self):
        self.directory = self.directory / 'missing'
        self.marker = self.directory / '.platform-storage.json'
        self.expect_failure('MARKER_WRITE_ERROR')

    @unittest.skipUnless(os.environ.get('STORAGE_GUARD_OFFICIAL_CONFIGS'),
                         'Optional public config fixtures from the pinned Docker image')
    def test_all_official_image_configs_and_installed_rerun(self):
        source = Path(os.environ['STORAGE_GUARD_OFFICIAL_CONFIGS'])
        configs = sorted(source.glob('*.config.php'))
        self.assertEqual(len(configs), 9)
        self.assertTrue((source / 'apps.config.php').is_file())
        for config in configs:
            shutil.copyfile(config, self.directory / config.name)
        first = self.run_guard()
        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, b'', b''))
        before = self.marker.read_bytes(), self.marker.stat().st_mtime_ns
        self.write('config.php', "$CONFIG = ['installed' => true];")
        shutil.copyfile(DOCKER / 'platform.config.php', self.directory / 'zz-platform.config.php')
        for _ in range(2):
            result = self.run_guard()
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        self.assertEqual((self.marker.read_bytes(), self.marker.stat().st_mtime_ns), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
