"""Separate PR3-compatible backend with explicit EC2/installation binding.

All execs use the durable supervisor. No fixture guard is overridden. Complete
host/state/writer collectors remain mandatory injected interfaces; no default
discovery, operational launcher or ready flags. Tests use synthetic inspectors.
"""
from dataclasses import dataclass
import copy
import re
from pathlib import Path, PurePosixPath
from .fencing import HeldWindow, require
from .postgres import PostgreSQL, Limited
from ..capture.docker_backend import DockerBackend
from ..capture.storage import CaptureFailure


@dataclass(frozen=True, repr=False)
class CaptureTarget:
    account: str
    region: str
    instance: str
    project: str
    data_bucket: str
    installation_identity: str
    deployment_root: str
    images: dict

    def __post_init__(self):
        require(isinstance(self.account, str) and re.fullmatch(r'[0-9]{12}', self.account)
                and isinstance(self.region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', self.region)
                and isinstance(self.instance, str) and re.fullmatch(r'i-[a-f0-9]{17}', self.instance), 'CAPTURE_TARGET_INVALID')
        require(isinstance(self.project, str) and re.fullmatch(r'[a-z][a-z0-9-]{1,62}', self.project)
                and isinstance(self.data_bucket, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', self.data_bucket), 'CAPTURE_TARGET_INVALID')
        require(isinstance(self.installation_identity, str) and re.fullmatch(r'[a-f0-9]{64}', self.installation_identity), 'INSTALLATION_IDENTITY_REQUIRED')
        require(isinstance(self.deployment_root, str) and PurePosixPath(self.deployment_root).is_absolute()
                and self.deployment_root != '/' and not any(p in ('.', '..', '') for p in self.deployment_root.split('/')[1:]),
                'DEPLOYMENT_ROOT_INVALID')
        require(isinstance(self.images, dict) and set(self.images) == {'nextcloud', 'postgres', 'redis'}
                and all(isinstance(v, str) and re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', v) for v in self.images.values()), 'PINNED_IMAGES_REQUIRED')


class HostProbe:
    """No fake evidence fallback. Must inspect the actual host in future bridge.

    read_state must work with Nextcloud stopped using a supervised read-only
    pinned helper, without Nextcloud bootstrap or writable mounts. before_capture
    must inspect the complete writer/cron/network/process boundary and PG peers.
    """
    def identity(self): raise CaptureFailure('HOST_COLLECTOR_UNIMPLEMENTED')
    def containers(self): raise CaptureFailure('HOST_COLLECTOR_UNIMPLEMENTED')
    def read_state(self): raise CaptureFailure('HOST_COLLECTOR_UNIMPLEMENTED')
    def baseline(self): raise CaptureFailure('HOST_COLLECTOR_UNIMPLEMENTED')
    def assert_writers_stopped(self): raise CaptureFailure('WRITER_COLLECTOR_UNIMPLEMENTED')
    def assert_restored(self, original): raise CaptureFailure('HOST_COLLECTOR_UNIMPLEMENTED')


class DockerControls:
    """Specific SDK calls only; never stop PostgreSQL/Redis or generic commands."""
    def __init__(self, api): self.api = api
    def stop_nextcloud(self, identifier):
        try: self.api.stop(identifier, timeout=60)
        except Exception: raise CaptureFailure('WRITER_STOP_OUTCOME_UNKNOWN') from None
    def start_nextcloud(self, identifier):
        try: self.api.start(identifier)
        except Exception: raise CaptureFailure('WRITER_START_OUTCOME_UNKNOWN') from None


class OperationalCaptureBackend:
    limit = 8 * 1024 ** 3

    def __init__(self, target, probe, controls, supervisor, window):
        require(type(target) is CaptureTarget and isinstance(probe, HostProbe)
                and type(window) is HeldWindow, 'CAPTURE_INTERFACES_REQUIRED')
        require(window.coordinator.scope.account == target.account
                and window.coordinator.scope.bucket == target.data_bucket, 'CAPTURE_FENCE_SCOPE_MISMATCH')
        self.target, self.probe, self.controls, self.supervisor, self.window = target, probe, controls, supervisor, window
        self.store = window.coordinator.store
        self.images = target.images.copy()
        self._context = None

    def state(self):
        self.window.assert_held()
        value = self.probe.read_state()
        require(isinstance(value, dict) and value.get('installed') is True
                and value.get('installation_identity_sha256') == self.target.installation_identity
                and value.get('objectstore_bucket') == self.target.data_bucket
                and value.get('objectstore_region') == self.target.region
                and value.get('objectstore_class') == '\\OC\\Files\\ObjectStore\\S3'
                and value.get('static_credentials') is False, 'CAPTURE_STORAGE_OR_IDENTITY_MISMATCH')
        require(isinstance(value.get('version'), str) and value['version'].split('.')[:3] == ['34', '0', '4']
                and type(value.get('maintenance')) is bool
                and isinstance(value.get('config_fingerprint'), str)
                and re.fullmatch(r'[a-f0-9]{64}', value['config_fingerprint']), 'CAPTURE_STATE_INVALID')
        return value

    def preflight(self):
        self.window.assert_held()
        require(self.probe.identity() == {'account': self.target.account, 'region': self.target.region,
                                         'instance': self.target.instance}, 'CAPTURE_HOST_IDENTITY_MISMATCH')
        containers = self.probe.containers()
        state = self.state()
        require(isinstance(containers, dict) and set(containers) == {'nextcloud', 'postgres', 'redis'}, 'CAPTURE_CONTAINERS_INVALID')
        for service, info in containers.items():
            require(isinstance(info, dict) and isinstance(info.get('Id'), str) and re.fullmatch(r'[a-f0-9]{64}', info['Id'])
                    and info.get('Config', {}).get('Image') == self.images[service]
                    and info.get('State', {}).get('Running') is True
                    and self.container_ready(service, info, state)
                    and isinstance(info.get('Mounts'), list), 'CAPTURE_CONTAINER_OR_IMAGE_MISMATCH')
            require((info['Config'].get('Labels') or {}).get('com.docker.compose.project') == self.target.project,
                    'CAPTURE_PROJECT_MISMATCH')
        require(containers['nextcloud']['Config'].get('StopSignal') in ('SIGWINCH', 'WINCH')
                and containers['nextcloud']['Config'].get('Cmd') == ['apache2-foreground'], 'GRACEFUL_DRAIN_NOT_SUPPORTED')
        self.containers = copy.deepcopy(containers)
        baseline = self.probe.baseline()
        # Baseline collector must use supervised PG commands, paths confined to
        # the actual inspected volumes, compatibility/ownership and free-space.
        require(isinstance(baseline, dict) and baseline.get('identity') == self.target.installation_identity
                and baseline.get('maintenance') == state['maintenance']
                and baseline.get('config_fingerprint') == state['config_fingerprint']
                and baseline.get('writers') == {'nextcloud': True}
                and baseline.get('dependencies_running') == {'postgres': True, 'redis': True}
                and isinstance(baseline.get('containers'), dict), 'CAPTURE_BASELINE_INVALID')
        expected = {s: {'id': i['Id'], 'image_id': i['Image'], 'mounts': i['Mounts']} for s, i in containers.items()}
        require(baseline['containers'] == expected, 'CAPTURE_BASELINE_INVALID')
        self.db = baseline['database']
        volumes = [m for m in containers['nextcloud']['Mounts'] if m.get('Destination') == '/var/www/html']
        require(len(volumes) == 1 and volumes[0].get('Type') == 'volume'
                and isinstance(volumes[0].get('Source'), str) and PurePosixPath(volumes[0]['Source']).is_absolute(),
                'CAPTURE_VOLUME_INVALID')
        self.html = Path(volumes[0]['Source'])
        # PR3's pure archive routine calls this attribute fixture_root; here its
        # value comes only from the explicit deployment target, not fixture config.
        self.fixture_root = Path(self.target.deployment_root)
        require(baseline.get('capture_sources') == [self.html.as_posix(), self.fixture_root.as_posix()], 'CAPTURE_SOURCES_MISMATCH')
        return copy.deepcopy(baseline)

    def container_ready(self, service, info, state):
        return info['State'].get('Health', {}).get('Status') == 'healthy'

    @property
    def context(self): return self._context

    @context.setter
    def context(self, value):
        require(isinstance(value, dict) and set(value) == {'operation_id', 'consistency_id'}
                and self._context is None, 'CAPTURE_CONTEXT_INVALID')
        self.window.coordinator.bind(self.window, value['operation_id'])
        self.supervisor.initialize(value['operation_id'])
        self.pg = PostgreSQL(self.supervisor, value['operation_id'], self.containers['postgres']['Id'], self.db)
        self._context = value.copy()

    def fingerprint(self): return self.state()['config_fingerprint']
    def maintenance(self): return self.state()['maintenance']

    def assert_identity(self, original):
        self.window.assert_held()
        if self.context is not None:
            self.supervisor.assert_idle(self.context['operation_id'])
            self.supervisor.assert_no_untracked_execs(self.context['operation_id'],
                [i['Id'] for i in self.containers.values()])
        require(self.probe.identity() == {'account': self.target.account, 'region': self.target.region,
                                         'instance': self.target.instance}, 'CAPTURE_HOST_IDENTITY_MISMATCH')
        found = self.probe.containers()
        require(set(found) == set(original['containers']), 'CAPTURE_TARGET_CHANGED')
        for name, expected in original['containers'].items():
            info = found[name]
            require({'id': info['Id'], 'image_id': info['Image'], 'mounts': info['Mounts']} == expected,
                    'CAPTURE_TARGET_CHANGED')
        self.state()

    def set_maintenance(self, enabled):
        require(type(enabled) is bool and self.context is not None, 'CAPTURE_CONTEXT_INVALID')
        self.supervisor.execute(self.context['operation_id'], self.containers['nextcloud']['Id'],
            ['php', '/var/www/html/occ', 'maintenance:mode', '--on' if enabled else '--off'],
            'www-data', Limited(), 60)
        require(self.maintenance() == enabled, 'MAINTENANCE_NOT_CONFIRMED')

    def stop_writer(self, service):
        require(service == 'nextcloud', 'WRITER_SERVICE_REJECTED')
        self.controls.stop_nextcloud(self.containers[service]['Id'])
        info = self.probe.containers()['nextcloud']['State']
        require(info.get('Running') is False and info.get('ExitCode') == 0 and info.get('OOMKilled') is not True,
                'WRITER_NOT_GRACEFULLY_DRAINED')

    def start_writer(self, service):
        require(service == 'nextcloud', 'WRITER_SERVICE_REJECTED')
        self.supervisor.assert_idle(self.context['operation_id'])
        self.controls.start_nextcloud(self.containers[service]['Id'])

    def assert_quiescent(self, original):
        self.assert_identity(original)
        require(self.probe.containers()['nextcloud']['State'].get('Running') is False, 'WRITER_STILL_RUNNING')
        require(self.probe.assert_writers_stopped() is None, 'EXTERNAL_WRITER_NOT_DRAINED')
        require(self.pg.sessions() == [], 'DATABASE_WRITER_NOT_FENCED')

    def assert_original_services(self, original):
        require(self.probe.assert_restored(original) is None, 'WRITER_STATE_NOT_RESTORED')

    def exec_pg(self, executable, *args):
        output = Limited()
        self.pg.command(executable, list(args), output, 120)
        return output.getvalue()

    def database_metadata(self):
        return DockerBackend.database_metadata(self)

    def dump(self, output):
        self.pg.compatibility()
        self.pg.dump(output)

    def check_dump(self, path):
        from ..safe_files import SafeRoot
        with SafeRoot(self.store.operation(self.context['operation_id'])) as safe:
            require(path.name == 'database.dump' and path.parent == self.store.operation(self.context['operation_id']), 'UNSAFE_DUMP_PATH')
            with safe.open(path.name) as stream: self.pg.check_dump(stream)

    def capture(self, role, output):
        require(role in ('configuration', 'local_files', 'recovery_parameters'), 'UNKNOWN_CAPTURE_ROLE')
        self.supervisor.assert_idle(self.context['operation_id'])
        self.window.assert_held()
        # Reuse only the confined archive/recovery-parameters algorithm. Never
        # call/override DockerBackend.isolated or its fixture-only constructor.
        # Ansible installs runtime files beneath <platform>/docker. The isolated
        # PR3 fixture keeps its files directly at fixture_root; do not conflate
        # that fixture layout with the production platform root.
        return DockerBackend.capture(self, role, output,
                                     recovery_root=Path(self.target.deployment_root) / 'docker')
