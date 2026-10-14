"""Actual local PHP with synthetic configs and an externally supplied pinned Config.php."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PHP = os.environ.get('CAPTURE_TEST_PHP')
CONFIG = os.environ.get('CAPTURE_TEST_OC_CONFIG')
HELPER = Path(__file__).resolve().parents[1] / 'read-state.php'


@unittest.skipUnless(PHP and CONFIG, 'cached PHP/official Config.php not supplied; no download performed')
class StateReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'lib/private').mkdir(parents=True)
        shutil.copyfile(CONFIG, self.root / 'lib/private/Config.php')
        self.helper = self.root / 'reader.php'
        self.helper.write_text(HELPER.read_text().replace('/var/www/html', self.root.as_posix()), encoding='utf-8')
        self.objectstore = {'class': '\\OC\\Files\\ObjectStore\\S3', 'arguments': {'bucket': 'fixture-data', 'region': 'us-east-1'}}
        self.write_config()
        (self.root / 'config/.platform-storage.json').write_text(json.dumps(self.objectstore))

    def write_config(self, extra=''):
        config = """<?php $CONFIG=['installed'=>true,'dbtype'=>'pgsql',
          'datadirectory'=>__DIR__.'/../data','maintenance'=>false,
          'instanceid'=>'synthetic-id','passwordsalt'=>'synthetic-salt','secret'=>'synthetic-sensitive-value',
          'version'=>'34.0.4.0','dbname'=>'fixture','dbuser'=>'fixture','dbtableprefix'=>'oc_',
          'objectstore'=>json_decode(base64_decode('%s'),true)]; %s""" % (base64.b64encode(json.dumps(self.objectstore).encode()).decode(), extra)
        config = config.replace("__DIR__.'/../data'", "'" + self.root.as_posix() + "/data'")
        (self.root / 'config/config.php').write_text(config)

    def run_reader(self, **overrides):
        env = {key: value for key, value in os.environ.items() if not key.startswith(('NC_', 'AWS_', 'NEXTCLOUD_', 'REDIS_'))}
        return subprocess.run([PHP, '-n', str(self.helper)], capture_output=True, env=dict(env, **overrides))

    def test_actual_config_reader_no_secret_output_or_mutation(self):
        before = {p.name: p.read_bytes() for p in (self.root / 'config').iterdir()}
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value['installed'])
        self.assertFalse(value['maintenance'])
        self.assertEqual(len(value['installation_identity_sha256']), 64)
        self.assertNotIn(b'synthetic-sensitive-value', result.stdout + result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / 'config').iterdir()})

    def test_apps_config_context_and_extra_config_precedence(self):
        (self.root / 'config/apps.config.php').write_text('<?php $CONFIG=["apps_paths"=>[["path"=>OC::$SERVERROOT."/custom_apps"]]];')
        (self.root / 'config/zz-maintenance.config.php').write_text('<?php $CONFIG=["maintenance"=>true];')
        result = self.run_reader()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)['maintenance'])

    def test_static_credentials_rejected_sanitized(self):
        self.objectstore['arguments']['key'] = 'synthetic-sensitive-value'
        self.write_config()
        (self.root / 'config/.platform-storage.json').write_text(json.dumps(self.objectstore))
        result = self.run_reader()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout + result.stderr, b'')

    def test_env_maintenance_override_rejected(self):
        result = self.run_reader(NC_maintenance='true')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, b'', b''))

    def test_not_installed_rejected_without_installation(self):
        self.write_config('$CONFIG["installed"]=false;')
        result = self.run_reader()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout + result.stderr, b'')

    def test_marker_mismatch_rejected(self):
        (self.root / 'config/.platform-storage.json').write_text('{}')
        self.assertEqual(self.run_reader().returncode, 1)

    def test_malicious_config_output_and_exit_not_success(self):
        self.write_config('echo "synthetic-sensitive-value"; exit(0);')
        result = self.run_reader()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, b'', b''))
