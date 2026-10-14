"""Run the pinned, unmodified entrypoint with fixture volumes and a fake installer.

No application installation: network disabled, no production volumes, no Apache,
no database/S3 adapter. An argv interceptor also refuses native install/upgrade.
Requires a Linux Docker engine with the already cached Compose image.
"""
import json
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SERVICE = (ROOT / 'docker/compose.yml').read_text().split('  nextcloud:\n')[1].split('\n  prometheus:')[0]
IMAGE = re.search(r'^    image: (.+)$', SERVICE, re.M).group(1)
KEYS = re.findall(r'^      ([A-Z0-9_]+):', SERVICE.split('    environment:\n')[1].split('    secrets:')[0], re.M)
FORBIDDEN = {'NEXTCLOUD_ADMIN_USER', 'NEXTCLOUD_ADMIN_PASSWORD', 'NEXTCLOUD_ADMIN_PASSWORD_FILE',
             'POSTGRES_PASSWORD', 'POSTGRES_PASSWORD_FILE'}


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=120, check=True).stdout


class EntrypointSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        metadata = json.loads(docker('image', 'inspect', IMAGE))[0]
        assert 'nextcloud@' + IMAGE.split('@')[1] in metadata['RepoDigests']
        image_keys = {e.split('=', 1)[0] for e in metadata['Config']['Env']}
        assert not FORBIDDEN.intersection(image_keys | set(KEYS))
        assert 'env_file:' not in SERVICE
        cls.image_path = next(e.split('=', 1)[1] for e in metadata['Config']['Env'] if e.startswith('PATH='))
        cls.entrypoint = docker('run', '--rm', '--network', 'none', '--read-only',
                               '--entrypoint', 'cat', IMAGE, '/entrypoint.sh')
        assert cls.entrypoint.index('occ maintenance:install') < cls.entrypoint.index('run_path before-starting')
        assert cls.entrypoint.index(') 9> /var/www/html/nextcloud-init-sync.lock') < cls.entrypoint.index('run_path before-starting')
        print(json.dumps({'image': IMAGE, 'entrypoint_sha256': hashlib.sha256(cls.entrypoint.encode()).hexdigest(),
                          'native_auto_install_inputs_absent': True,
                          'native_install_line': cls.entrypoint[:cls.entrypoint.index('occ maintenance:install')].count('\n') + 1,
                          'before_starting_line': cls.entrypoint[:cls.entrypoint.index('run_path before-starting')].count('\n') + 1}), flush=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.html = self.path / 'html'
        self.html.mkdir(mode=0o777)
        self.html.chmod(0o777)
        self.platform = self.path / 'platform'
        self.platform.mkdir(mode=0o755)
        self.bin = self.path / 'bin'
        self.bin.mkdir(mode=0o755)
        for name in ('storage-guard.php', 'platform.config.php', 'configure-nextcloud.sh'):
            (self.platform / name).write_bytes((ROOT / 'docker' / name).read_bytes())
        (self.platform / 'configure-nextcloud.sh').chmod(0o755)
        (self.platform / 'install-library.php').write_bytes((ROOT / 'docker/install-nextcloud.php').read_bytes())
        (self.platform / 'install-nextcloud.php').write_text('''<?php
require '/opt/platform/install-library.php';
$cfg='/var/www/html/config';
function auditEvent($text) { file_put_contents('/var/www/html/audit-events', $text."\\n", FILE_APPEND); }
$adapter=[
 'guard'=>function() {
   // The official sync lock must be released before our own installer holds its lock.
   $sync=fopen('/var/www/html/nextcloud-init-sync.lock','r');
   if (!flock($sync,LOCK_EX|LOCK_NB)) { throw new RuntimeException('SYNC_LOCK_STILL_HELD'); }
   flock($sync,LOCK_UN); fclose($sync); auditEvent('SYNC_LOCK_RELEASED');
   $p=proc_open([PHP_BINARY,'/opt/platform/storage-guard.php'],
     [0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes);
   fclose($pipes[0]); stream_get_contents($pipes[1]); fclose($pipes[1]);
   stream_get_contents($pipes[2]); fclose($pipes[2]);
   if (proc_close($p)!==0) { throw new RuntimeException('GUARD_FAILED'); }
   auditEvent('GUARD');
 },
 'state'=>function() use ($cfg) {
   $CONFIG=[]; if (is_file($cfg.'/config.php')) { require $cfg.'/config.php'; }
   $installed=$CONFIG['installed']??false;
   $CONFIG=[]; require $cfg.'/zz-platform.config.php'; $actual=$CONFIG['objectstore'];
   require '/opt/platform/platform.config.php';
   return ['installed'=>$installed,'storage_matches'=>$actual===$CONFIG['objectstore'],'install_config_matches'=>true];
 },
 'local_data_safe'=>function() { auditEvent('LOCAL_READ_ONLY'); return true; },
 'database_empty'=>function() { auditEvent('DB_READ_ONLY'); return true; },
 'bucket_empty'=>function() { auditEvent('S3_READ_ONLY'); return true; },
 'install'=>function() use ($cfg) {
   auditEvent('FAKE_INSTALL_AFTER_S3');
   // Fixture state only; never calls the official command or connects to a database.
   file_put_contents($cfg.'/config.php',"<?php \\$CONFIG=['installed'=>true];");
   return 0;
 },
];
exit(runPlatformInstallation($adapter,$cfg));
''')
        (self.bin / 'php').write_text('''#!/bin/sh
case "$*" in *maintenance:install*|*"occ upgrade"*) echo NATIVE_INSTALL_FORBIDDEN >&2; exit 99;; esac
exec /usr/local/bin/php "$@"
''')
        (self.bin / 'apache2-foreground').write_text('#!/bin/sh\nprintf "APACHE_PHASE\\n" >> /var/www/html/audit-events\n')
        for script in self.bin.iterdir():
            script.chmod(0o755)
        secrets = self.path / 'secrets'
        secrets.mkdir(mode=0o755)
        for name in ('postgres_password', 'nextcloud_admin_password', 'redis_password'):
            (secrets / name).write_text('fixture-only-' + 'x' * 64)
        self.env = dict(POSTGRES_HOST='postgres', POSTGRES_DB='nextcloud', POSTGRES_USER='nextcloud',
                        PLATFORM_POSTGRES_PASSWORD_FILE='/run/secrets/postgres_password', PLATFORM_ADMIN_USER='fixtureadmin',
                        PLATFORM_ADMIN_PASSWORD_FILE='/run/secrets/nextcloud_admin_password', NEXTCLOUD_TRUSTED_DOMAINS='localhost',
                        NEXTCLOUD_S3_BUCKET='fixture-nextcloud-data', NEXTCLOUD_S3_REGION='us-east-1',
                        REDIS_HOST='redis', REDIS_HOST_PORT='6379', REDIS_HOST_PASSWORD_FILE='/run/secrets/redis_password')
        self.assertEqual(set(KEYS), set(self.env))

    def tearDown(self):
        # Container-created www-data files need removal by an isolated container.
        docker('run', '--rm', '--network', 'none', '--mount', f'type=bind,src={self.html},dst=/fixture',
               '--entrypoint', 'sh', IMAGE, '-c', 'find /fixture -mindepth 1 -delete')
        self.temp.cleanup()

    def start(self):
        args = ['run', '--rm', '--network', 'none', '--mount', f'type=bind,src={self.html},dst=/var/www/html',
                '--mount', f'type=bind,src={self.platform},dst=/opt/platform,readonly',
                '--mount', f'type=bind,src={self.platform / "configure-nextcloud.sh"},dst=/docker-entrypoint-hooks.d/before-starting/10-platform.sh,readonly',
                '--mount', f'type=bind,src={self.bin},dst=/audit-bin,readonly',
                '--mount', f'type=bind,src={self.path / "secrets"},dst=/run/secrets,readonly',
                '--env', 'PATH=/audit-bin:' + self.image_path]
        for name, value in self.env.items():
            args += ['--env', name + '=' + value]
        output = docker(*args, IMAGE, 'apache2-foreground')
        self.assertNotIn('NATIVE_INSTALL_FORBIDDEN', output)
        self.assertNotIn('Starting nextcloud installation', output)
        self.assertNotIn('fixture-only-', output)
        return output

    def test_fresh_then_installed_restart(self):
        output = self.start()
        self.assertIn('New nextcloud instance', output)
        self.assertIn('Platform install: INSTALLED', output)
        events = (self.html / 'audit-events').read_text().splitlines()
        self.assertEqual(events, ['SYNC_LOCK_RELEASED', 'GUARD', 'LOCAL_READ_ONLY', 'DB_READ_ONLY',
                                  'S3_READ_ONLY', 'FAKE_INSTALL_AFTER_S3', 'APACHE_PHASE'])
        marker = (self.html / 'config/.platform-storage.json').read_bytes()
        output = self.start()
        self.assertIn('Platform install: ALREADY_INSTALLED', output)
        self.assertEqual((self.html / 'audit-events').read_text().splitlines()[len(events):],
                         ['SYNC_LOCK_RELEASED', 'GUARD', 'APACHE_PHASE'])
        self.assertEqual(marker, (self.html / 'config/.platform-storage.json').read_bytes())
        print('FRESH: ' + ' -> '.join(events) + '; RESTART: GUARD -> ALREADY_INSTALLED -> APACHE_PHASE', flush=True)

    def test_interrupted_copy_without_application_tables(self):
        docker('run', '--rm', '--network', 'none', '--mount', f'type=bind,src={self.html},dst=/fixture',
               '--entrypoint', 'sh', IMAGE, '-c',
               'mkdir /fixture/config; cp /usr/src/nextcloud/version.php /fixture/; '
               'cp /usr/src/nextcloud/config/*.config.php /fixture/config/; chmod -R a+rwX /fixture')
        config = self.html / 'config/config.php'
        config.write_text("<?php $CONFIG=['installed'=>false];")
        config.chmod(0o666)
        output = self.start()
        self.assertNotIn('New nextcloud instance', output)
        self.assertIn('Platform install: INSTALLED', output)
        self.assertEqual((self.html / 'audit-events').read_text().splitlines(),
                         ['SYNC_LOCK_RELEASED', 'GUARD', 'LOCAL_READ_ONLY', 'DB_READ_ONLY',
                          'S3_READ_ONLY', 'FAKE_INSTALL_AFTER_S3', 'APACHE_PHASE'])
        print('INTERRUPTED: same safe installation sequence; official version-copy branch skipped', flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
