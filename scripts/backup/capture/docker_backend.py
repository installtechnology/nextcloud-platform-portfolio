"""Docker adapter only for an explicitly isolated local fixture, never operational EC2."""
import base64
import json
import os
from pathlib import Path
import re
import shutil

from ..json_input import parse_json
from ..safe_files import SafeRoot
from .archive import archive_tree
from .process import Runner
from .storage import CaptureFailure


class DockerBackend:
    limit = 8 * 1024 ** 3

    def __init__(self, project, fixture_root, runner=None):
        if not re.fullmatch(r'nc-capture-test-[a-z0-9-]{1,40}', project):
            raise CaptureFailure('ISOLATED_PROJECT_REQUIRED')
        if os.name != 'posix' or os.geteuid() != 0:
            raise CaptureFailure('LINUX_ROOT_REQUIRED')
        self.project, self.fixture_root = project, Path(os.path.abspath(fixture_root))
        with SafeRoot(self.fixture_root):
            info = self.fixture_root.stat()
            if info.st_uid != 0 or info.st_mode & 0o077:
                raise CaptureFailure('PRIVATE_FIXTURE_REQUIRED')
        self.runner = runner or Runner()
        compose = (Path(__file__).resolve().parents[3] / 'docker/compose.yml').read_text()
        self.images = {service: re.search(r'image: (' + service + r':[^\s]+)', compose).group(1)
                       for service in ('nextcloud', 'postgres')}
        self.containers = {}

    def run(self, *args, **kwargs):
        return self.runner.run(['docker', *args], **kwargs)

    def inspect(self, identifier):
        return json.loads(self.run('inspect', identifier))[0]

    def isolated(self):
        ids = self.run('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + self.project).decode().split()
        if len(ids) != 3:
            raise CaptureFailure('ISOLATED_THREE_SERVICE_FIXTURE_REQUIRED')
        found = {}
        for identifier in ids:
            info = self.inspect(identifier)
            labels = info['Config'].get('Labels') or {}
            service = labels.get('com.docker.compose.service')
            if service not in ('nextcloud', 'postgres', 'redis') or service in found or labels.get('platform.capture.isolated') != 'true':
                raise CaptureFailure('UNCONTROLLED_WRITER_OR_FIXTURE')
            if info['HostConfig'].get('Privileged') or info['HostConfig'].get('NetworkMode') == 'host' or info['HostConfig'].get('PortBindings'):
                raise CaptureFailure('EXTERNAL_ACCESS_NOT_FENCED')
            networks = info['NetworkSettings']['Networks']
            if not networks:
                raise CaptureFailure('ISOLATION_NOT_PROVEN')
            for network in networks:
                if not json.loads(self.run('network', 'inspect', network))[0]['Internal']:
                    raise CaptureFailure('EXTERNAL_ACCESS_NOT_FENCED')
            for mount in info['Mounts']:
                if mount['Type'] == 'volume':
                    volume = json.loads(self.run('volume', 'inspect', mount['Name']))[0]
                    if volume['Driver'] != 'local' or volume.get('Options') or (volume.get('Labels') or {}).get('platform.capture.isolated') != 'true':
                        raise CaptureFailure('ISOLATED_VOLUME_REQUIRED')
                elif mount['Type'] == 'bind':
                    if not Path(mount['Source']).resolve().is_relative_to(self.fixture_root):
                        raise CaptureFailure('EXTERNAL_BIND_FORBIDDEN')
                else:
                    raise CaptureFailure('UNCLASSIFIED_MOUNT')
            if service in self.images and info['Config']['Image'] != self.images[service]:
                raise CaptureFailure('PINNED_IMAGE_MISMATCH')
            found[service] = info
        # No unrelated container may attach to the fixture's volumes or networks.
        volumes = {m['Source'] for i in found.values() for m in i['Mounts']}
        networks = {n for i in found.values() for n in i['NetworkSettings']['Networks']}
        for identifier in self.run('ps', '-aq').decode().split():
            info = self.inspect(identifier)
            if info['Id'] not in {i['Id'] for i in found.values()}:
                if any(m['Source'] in volumes for m in info['Mounts']) or networks & set(info['NetworkSettings']['Networks']):
                    raise CaptureFailure('EXTERNAL_WRITER_FOUND')
        self.containers = found
        return found

    def state(self):
        info = self.inspect(self.containers['nextcloud']['Id'])
        env = dict(value.split('=', 1) for value in info['Config']['Env'])
        if any(key.startswith(('AWS_', 'NC_')) or ('PASSWORD' in key and not key.endswith('_FILE')) for key in env):
            raise CaptureFailure('UNSAFE_CONTAINER_ENVIRONMENT')
        script = (Path(__file__).with_name('read-state.php')).read_bytes()
        encoded = base64.b64encode(json.dumps(env).encode()).decode()
        prefix = ('<?php foreach(json_decode(base64_decode("' + encoded + '"), true) as $k=>$v){putenv($k."=".$v);} ?>').encode()
        output = self.run('run', '--rm', '--pull=never', '--network', 'none', '--read-only', '--user', 'www-data',
                          '--label', 'platform.capture.isolated=true', '--volumes-from', info['Id'] + ':ro',
                          '--entrypoint', 'php', '-i', self.images['nextcloud'], input_bytes=prefix + script)
        return parse_json(output)

    def fingerprint(self):
        return self.state()['config_fingerprint']

    def maintenance(self):
        return self.state()['maintenance']

    def exec_pg(self, *args, **kwargs):
        # Explicit Unix socket and role prevent PGHOST/PGUSER container environment
        # from redirecting database tools to another server. Never supply passwords.
        return self.run('exec', '-i', '--user', 'postgres', self.containers['postgres']['Id'],
                        args[0], '--host=/var/run/postgresql', '--username=postgres', *args[1:], **kwargs)

    def database_metadata(self):
        query = """SELECT json_build_object('server_version_num',current_setting('server_version_num')::int,
          'database',d.datname,'owner',pg_get_userbyid(d.datdba),'encoding',pg_encoding_to_char(d.encoding),
          'collate',d.datcollate,'ctype',d.datctype,'locale_provider',d.datlocprovider,'collation_version',d.datcollversion,
          'extensions',(SELECT json_agg(json_build_object('name',extname,'version',extversion)) FROM pg_extension),
          'database_bytes',pg_database_size(current_database()),
          'custom_tablespaces',(SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')),
          'peer_sessions',(SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()))
          FROM pg_database d WHERE d.datname=current_database()"""
        try:
            return parse_json(self.exec_pg('psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '--dbname', self.db, '-c', query))
        except CaptureFailure as error:
            if error.code == 'COMMAND_TIMEOUT':
                raise
            raise CaptureFailure('POSTGRES_UNAVAILABLE') from None

    def preflight(self):
        found = self.isolated()
        if not all(found[name]['State']['Running'] for name in ('nextcloud', 'postgres', 'redis')):
            raise CaptureFailure('FIXTURE_NOT_RUNNING')
        state = self.state()
        if state['version'].split('.')[:3] != ['34', '0', '4']:
            raise CaptureFailure('NEXTCLOUD_VERSION_MISMATCH')
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,62}', state['database']) or not re.fullmatch(r'[a-z][a-z0-9_]{1,62}', state['owner']):
            raise CaptureFailure('DATABASE_IDENTIFIERS_INVALID')
        self.db = state['database']
        metadata = self.database_metadata()
        if metadata['server_version_num'] // 10000 != 17 or metadata['owner'] != state['owner'] or metadata['custom_tablespaces']:
            raise CaptureFailure('POSTGRES_COMPATIBILITY_FAILED')
        version = self.exec_pg('pg_dump', '--version').decode()
        if not re.search(r'\b17\.', version):
            raise CaptureFailure('DUMP_CLIENT_MAJOR_MISMATCH')
        signal = found['nextcloud']['Config'].get('StopSignal')
        if signal not in ('SIGWINCH', 'WINCH') or found['nextcloud']['Config'].get('Cmd') != ['apache2-foreground']:
            raise CaptureFailure('GRACEFUL_DRAIN_NOT_SUPPORTED')
        self.html = Path(next(m['Source'] for m in found['nextcloud']['Mounts'] if m['Destination'] == '/var/www/html'))
        html_size = sum(path.lstat().st_size for path in self.html.rglob('*') if path.is_file())
        if shutil.disk_usage(self.fixture_root).free < 1024 ** 3:
            raise CaptureFailure('INSUFFICIENT_DISK')
        return {'maintenance': state['maintenance'], 'writers': {'nextcloud': True},
                'config_fingerprint': state['config_fingerprint'],
                'capture_sources': [str(self.html), str(self.fixture_root)],
                'estimated_capture_bytes': 2 * (html_size + metadata['database_bytes']) + 64 * 1024 * 1024,
                'dependencies_running': {'postgres': True, 'redis': True},
                'identity': state['installation_identity_sha256'],
                'containers': {name: {'id': i['Id'], 'image_id': i['Image'], 'mounts': i['Mounts']} for name, i in found.items()},
                'database': self.db, 'owner': state['owner'], 'table_prefix': state['table_prefix']}

    def assert_identity(self, original):
        self.isolated()
        self.db = original['database']
        for name, expected in original['containers'].items():
            info = self.containers[name]
            if {'id': info['Id'], 'image_id': info['Image'], 'mounts': info['Mounts']} != expected:
                raise CaptureFailure('CAPTURE_TARGET_CHANGED')
        if self.state()['installation_identity_sha256'] != original['identity']:
            raise CaptureFailure('INSTALLATION_IDENTITY_CHANGED')

    def set_maintenance(self, enabled):
        self.run('exec', '--user', 'www-data', self.containers['nextcloud']['Id'], 'php', '/var/www/html/occ',
                 'maintenance:mode', '--on' if enabled else '--off')
        if self.maintenance() != enabled:
            raise CaptureFailure('MAINTENANCE_NOT_CONFIRMED')

    def stop_writer(self, service):
        identifier = self.containers[service]['Id']
        self.run('stop', '--timeout', '60', identifier, timeout=90)
        info = self.inspect(identifier)
        if info['State']['Running'] or info['State']['ExitCode'] != 0 or info['State'].get('OOMKilled'):
            raise CaptureFailure('WRITER_NOT_GRACEFULLY_DRAINED')

    def start_writer(self, service):
        identifier = self.containers[service]['Id']
        if not self.inspect(identifier)['State']['Running']:
            self.run('start', identifier)

    def assert_original_services(self, original):
        for service, running in original['writers'].items():
            if self.inspect(self.containers[service]['Id'])['State']['Running'] != running:
                raise CaptureFailure('WRITER_STATE_NOT_RESTORED')
        for service, running in original['dependencies_running'].items():
            info = self.inspect(self.containers[service]['Id'])
            if info['State']['Running'] != running or info['State'].get('Health', {}).get('Status') != 'healthy':
                raise CaptureFailure('DEPENDENCY_STATE_NOT_RESTORED')
        self.database_metadata()

    def assert_quiescent(self, original):
        self.assert_identity(original)
        if any(self.inspect(self.containers[s]['Id'])['State']['Running'] for s in original['writers']):
            raise CaptureFailure('WRITER_STILL_RUNNING')
        if self.database_metadata()['peer_sessions']:
            raise CaptureFailure('DATABASE_WRITER_NOT_FENCED')

    def dump(self, output):
        try:
            self.runner.stream(['docker', 'exec', '--user', 'postgres', self.containers['postgres']['Id'],
                                'pg_dump', '--host=/var/run/postgresql', '--username=postgres',
                                '--format=custom', '--create', '--dbname', self.db], output, timeout=600)
        except CaptureFailure as error:
            if error.code in ('COMMAND_TIMEOUT', 'CAPTURE_CANCELLED', 'ARTIFACT_LIMIT'):
                raise
            raise CaptureFailure('POSTGRES_DUMP_FAILED') from None

    def check_dump(self, path):
        with path.open('rb') as header:
            if header.read(5) != b'PGDMP':
                raise CaptureFailure('DUMP_FORMAT_INVALID')
        # Input is streamed from a private file, never a password or SQL in argv.
        with path.open('rb') as source:
            # Runner supports bytes only for small inputs; use a file descriptor for dump catalogs.
            try:
                self.runner.stream(['docker', 'exec', '-i', '--user', 'postgres', self.containers['postgres']['Id'],
                                    'pg_restore', '--list'], _Discard(), timeout=300, input_file=source)
            except CaptureFailure as error:
                if error.code in ('COMMAND_TIMEOUT', 'CAPTURE_CANCELLED'):
                    raise
                raise CaptureFailure('DUMP_CATALOG_INVALID') from None

    def capture(self, role, output, *, recovery_root=None):
        if role == 'configuration':
            archive_tree(self.html / 'config', output)
        elif role == 'local_files':
            archive_tree(self.html, output)
        elif role == 'recovery_parameters':
            globals_sql = self.exec_pg('pg_dumpall', '--globals-only', '--no-role-passwords').decode()
            secrets = {}
            def protected_read(root, name, maximum):
                with root.open(name) as stream:
                    value = stream.read(maximum + 1)
                    if len(value) > maximum:
                        raise CaptureFailure('RECOVERY_INPUT_LIMIT')
                    return base64.b64encode(value).decode()
            with SafeRoot(self.fixture_root if recovery_root is None else recovery_root) as private:
                for name in ('postgres_admin_password', 'postgres_password', 'redis_password', 'nextcloud_admin_password'):
                    secrets[name] = protected_read(private, 'secrets/' + name, 4096)
                env = protected_read(private, '.env', 65536)
                runtime_code = {name: protected_read(private, name, 1024 * 1024) for name in (
                    'compose.yml', 'postgres-init.sh', 'redis-entrypoint.sh', 'platform.config.php',
                    'storage-guard.php', 'install-nextcloud.php', 'configure-nextcloud.sh',
                    'nextcloud-healthcheck.php', 'nextcloud-integration.php')}
                private.assert_unchanged()
            parameters = {'internal_version': 1, 'kind': 'local-recovery-parameters', 'database': self.database_metadata(),
                          'globals_sql_without_passwords': globals_sql, 'images': self.images,
                          'capture_binding': self.context,
                          'secrets_base64': secrets, 'compose_env_base64': env, 'runtime_code_base64': runtime_code,
                          'recovery_requirement': 'review globals/roles/grants; set role credentials from protected secrets before DB restore',
                          'restore_proven': False}
            output.write(json.dumps(parameters).encode())
        else:
            raise CaptureFailure('UNKNOWN_CAPTURE_ROLE')


class _Discard:
    def write(self, value):
        return len(value)
