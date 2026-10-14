import copy
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.backup.operational.capture_backend import CaptureTarget, HostProbe, OperationalCaptureBackend
from scripts.backup.operational.fencing import HeldWindow
from scripts.backup.operational.tests.test_fencing import Store, API, Authority, Barrier, Coordinator, SCOPE, OP, CAPTURE, ORIGINAL
from scripts.backup.capture.storage import CaptureFailure

IMAGES = {'nextcloud': 'nextcloud:34.0.4-apache@sha256:'+'a'*64,
          'postgres': 'postgres:17.11-bookworm@sha256:'+'b'*64,
          'redis': 'redis:7.4.8-bookworm@sha256:'+'c'*64}


class Probe(HostProbe):
    def __init__(self):
        self.host = {'account': SCOPE.account, 'region': 'us-east-1', 'instance': 'i-0123456789abcdef0'}
        self.state = {'installed': True, 'installation_identity_sha256': 'd'*64, 'maintenance': False,
            'config_fingerprint': 'e'*64, 'version': '34.0.4.1', 'objectstore_bucket': SCOPE.bucket,
            'objectstore_region': 'us-east-1', 'objectstore_class': '\\OC\\Files\\ObjectStore\\S3',
            'static_credentials': False}
        self.found = {s: {'Id': str(n)*64, 'Image': str(n)*64,
            'Config': {'Image': image, 'StopSignal': 'SIGWINCH', 'Cmd': ['apache2-foreground'],
                       'Labels': {'com.docker.compose.project': 'synthetic-platform'}},
            'State': {'Running': True, 'ExitCode': 0, 'Health': {'Status': 'healthy'}},
            'Mounts': [{'Type': 'volume', 'Destination': '/var/www/html', 'Source': '/synthetic/volume'}]}
            for n,(s,image) in enumerate(IMAGES.items(), 1)}
        self.writer_error = False
    def identity(self): return self.host
    def containers(self): return copy.deepcopy(self.found)
    def read_state(self): return self.state.copy()
    def baseline(self):
        return {'maintenance': False, 'config_fingerprint': 'e'*64, 'identity': 'd'*64,
            'writers': {'nextcloud': True}, 'dependencies_running': {'postgres': True, 'redis': True},
            'containers': {s: {'id': i['Id'], 'image_id': i['Image'], 'mounts': i['Mounts']} for s,i in self.found.items()},
            'database': 'nextcloud', 'owner': 'nextcloud', 'table_prefix': 'oc_',
            'capture_sources': ['/synthetic/volume', '/opt/synthetic/docker'], 'estimated_capture_bytes': 100}
    def assert_writers_stopped(self):
        if self.writer_error: raise CaptureFailure('EXTERNAL_WRITER_NOT_DRAINED')
    def assert_restored(self, original): return None


class Supervisor:
    def __init__(self, store): self.store = store; self.calls = []; self.orphan = False
    def initialize(self, operation): self.calls.append(('init', operation))
    def assert_idle(self, operation):
        if self.orphan: raise CaptureFailure('ORPHANED_CAPTURE_PRESENT')
    def assert_no_untracked_execs(self, operation, containers): self.assert_idle(operation)
    def execute(self, *args): self.calls.append(('exec', args))


class Controls:
    def __init__(self, probe): self.probe = probe; self.calls = []
    def stop_nextcloud(self, identifier):
        self.calls.append(('stop', identifier)); self.probe.found['nextcloud']['State']['Running'] = False
    def start_nextcloud(self, identifier):
        self.calls.append(('start', identifier)); self.probe.found['nextcloud']['State']['Running'] = True


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.authority = Authority(); self.authority.held = True
        self.coordinator = Coordinator(self.store, API(self.store), SCOPE, self.authority, Barrier())
        # Synthetic already-observed window; not a live administrative receipt.
        from scripts.backup.operational.fence_policy import build_policy, digest
        import hashlib, json
        raw = json.dumps(ORIGINAL)
        fenced = build_policy(raw, SCOPE, OP)
        self.store.write(OP+'/fence-journal.json', {'kind': 'administrative-s3-fence', 'version': 1,
            'operation_id': OP, 'scope': SCOPE.__dict__.copy(), 'identity': self.coordinator.identity(),
            'before_raw': raw, 'before_hash': digest(ORIGINAL),
            'before_bytes_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'fenced_policy': fenced, 'fenced_hash': digest(fenced),
            'capture_id': None, 'phase': 'observed', 'observations': [], 'backup_complete': False})
        import threading
        self.window = HeldWindow(self.coordinator, OP, threading.get_ident())
        self.probe = Probe(); self.controls = Controls(self.probe); self.supervisor = Supervisor(self.store)
        self.target = CaptureTarget(SCOPE.account, 'us-east-1', 'i-0123456789abcdef0',
            'synthetic-platform', SCOPE.bucket, 'd'*64, '/opt/synthetic/docker', IMAGES)
        self.backend = OperationalCaptureBackend(self.target, self.probe, self.controls, self.supervisor, self.window)
    def code(self, expected, action):
        with self.assertRaises(CaptureFailure) as error: action()
        self.assertEqual(error.exception.code, expected)
    def ready(self):
        baseline = self.backend.preflight()
        self.backend.context = {'operation_id': CAPTURE, 'consistency_id': 'capture-synthetic'}
        return baseline
    def test_explicit_target_preflight(self):
        original = self.ready(); self.assertEqual(original['identity'], self.target.installation_identity)
        self.assertEqual(self.supervisor.calls[0], ('init', CAPTURE))
    def test_recovery_files_use_ansible_docker_subdirectory(self):
        self.ready()
        output = io.BytesIO()
        with patch('scripts.backup.operational.capture_backend.DockerBackend.capture') as capture:
            self.backend.capture('recovery_parameters', output)
        capture.assert_called_once_with(self.backend, 'recovery_parameters', output,
            recovery_root=Path(self.target.deployment_root) / 'docker')
    def test_wrong_ec2_blocks_before_maintenance(self):
        self.probe.host['instance'] = 'i-aaaaaaaaaaaaaaaaa'
        self.code('CAPTURE_HOST_IDENTITY_MISMATCH', self.backend.preflight); self.assertFalse(self.controls.calls)
    def test_wrong_installation(self):
        self.probe.state['installation_identity_sha256'] = 'f'*64
        self.code('CAPTURE_STORAGE_OR_IDENTITY_MISMATCH', self.backend.preflight)
    def test_wrong_bucket(self):
        self.probe.state['objectstore_bucket'] = 'other-data'
        self.code('CAPTURE_STORAGE_OR_IDENTITY_MISMATCH', self.backend.preflight)
    def test_static_credentials_rejected(self):
        self.probe.state['static_credentials'] = True
        self.code('CAPTURE_STORAGE_OR_IDENTITY_MISMATCH', self.backend.preflight)
    def test_wrong_image(self):
        self.probe.found['redis']['Config']['Image'] = 'redis:latest'
        self.code('CAPTURE_CONTAINER_OR_IMAGE_MISMATCH', self.backend.preflight)
    def test_wrong_compose_project(self):
        self.probe.found['nextcloud']['Config']['Labels']['com.docker.compose.project'] = 'nc-capture-test-fixture'
        self.code('CAPTURE_PROJECT_MISMATCH', self.backend.preflight)
    def test_pg_never_stopped_by_writer_api(self):
        self.ready(); self.code('WRITER_SERVICE_REJECTED', lambda: self.backend.stop_writer('postgres'))
        self.assertFalse(self.controls.calls)
    def test_nextcloud_graceful_stop(self):
        self.ready(); self.backend.stop_writer('nextcloud')
        self.assertEqual(self.controls.calls, [('stop', '1'*64)])
    def test_orphan_blocks_restart(self):
        self.ready(); self.supervisor.orphan = True
        self.code('ORPHANED_CAPTURE_PRESENT', lambda: self.backend.start_writer('nextcloud'))
        self.assertFalse(self.controls.calls)
    def test_changed_mount_blocks_recovery(self):
        baseline = self.ready(); self.probe.found['nextcloud']['Mounts'][0]['Source'] = '/other/volume'
        self.code('CAPTURE_TARGET_CHANGED', lambda: self.backend.assert_identity(baseline))
    def test_context_cannot_rebind_another_capture(self):
        self.ready()
        self.code('CAPTURE_CONTEXT_INVALID', lambda: setattr(self.backend, 'context', {'operation_id': OP, 'consistency_id': 'other'}))
    def test_unimplemented_probe_is_not_ready(self):
        backend = OperationalCaptureBackend(self.target, HostProbe(), self.controls, self.supervisor, self.window)
        self.code('HOST_COLLECTOR_UNIMPLEMENTED', backend.preflight)
    def test_archive_reuses_guarded_algorithm_only(self):
        self.ready()
        with patch('scripts.backup.operational.capture_backend.DockerBackend.capture', return_value=None) as routine:
            self.backend.capture('configuration', io.BytesIO())
        self.assertEqual(routine.call_args.args[1], 'configuration')
    def test_unknown_archive_rejected(self):
        self.ready(); self.code('UNKNOWN_CAPTURE_ROLE', lambda: self.backend.capture('other', io.BytesIO()))
    def test_closed_fence_cannot_read_state(self):
        self.window.released = True
        self.code('FENCE_WINDOW_NOT_HELD', self.backend.state)
    def test_deployment_path_traversal(self):
        self.code('DEPLOYMENT_ROOT_INVALID', lambda: CaptureTarget(SCOPE.account, 'us-east-1', self.target.instance,
            self.target.project, SCOPE.bucket, 'd'*64, '/opt/../private', IMAGES))
