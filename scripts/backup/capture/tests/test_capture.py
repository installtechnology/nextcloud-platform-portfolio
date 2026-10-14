"""Synthetic state-machine and local filesystem tests, not operational consistency proof."""
import copy
import io
import json
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.backup.capture.__main__ import main
from scripts.backup.capture.archive import archive_tree
from scripts.backup.capture.control import cleanup_signals
from scripts.backup.capture.docker_backend import DockerBackend
from scripts.backup.capture.engine import Capture
from scripts.backup.capture.storage import CaptureFailure, Store, atomic_json
from scripts.backup.errors import ValidationFailure


class FakeBackend:
    limit = 1024 * 1024

    def __init__(self, maintenance=False, running=True, failure=None):
        self.enabled, self.running, self.failure = maintenance, running, failure
        self.calls = []
        self.extra = ''
        self.identity = 'synthetic-identity'

    def step(self, name):
        self.calls.append(name)
        if self.failure == name:
            raise CaptureFailure('SYNTHETIC_' + name.upper())

    def preflight(self):
        self.step('preflight')
        return {'maintenance': self.enabled, 'writers': {'nextcloud': self.running}, 'identity': self.identity,
                'config_fingerprint': self.fingerprint()}

    def fingerprint(self):
        return ('enabled' if self.enabled else 'disabled') + self.extra

    def maintenance(self):
        return self.enabled

    def set_maintenance(self, value):
        self.step('maintenance_on' if value else 'maintenance_off')
        self.enabled = value

    def stop_writer(self, service):
        self.step('stop')
        self.running = False

    def start_writer(self, service):
        self.step('start')
        self.running = True

    def assert_identity(self, original):
        if original['identity'] != self.identity:
            raise CaptureFailure('INSTALLATION_IDENTITY_CHANGED')

    def assert_quiescent(self, original):
        self.step('quiescent')
        if self.running:
            raise CaptureFailure('WRITER_STILL_RUNNING')

    def assert_original_services(self, original):
        if self.running != original['writers']['nextcloud']:
            raise CaptureFailure('WRITER_STATE_NOT_RESTORED')

    def dump(self, stream):
        self.step('dump')
        stream.write(b'PGDMP synthetic NOT a real PostgreSQL dump')

    def check_dump(self, path):
        self.step('catalog')
        if not path.read_bytes().startswith(b'PGDMP'):
            raise CaptureFailure('DUMP_FORMAT_INVALID')

    def capture(self, role, stream):
        self.step(role)
        stream.write(b'synthetic protected local artifact')


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.store = Store(self.root, synthetic=True)

    def assert_code(self, expected, operation):
        with self.assertRaises(CaptureFailure) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)

    def journal(self):
        active = self.store.read('active.json')
        return self.store.read(active['operation_id'] + '/journal.json')

    def test_candidate_no_manifest_completion_or_restore_claim(self):
        backend = FakeBackend()
        result = Capture(self.store, backend).start()
        self.assertFalse(result['backup_complete'])
        self.assertEqual(result['functional_restore'], 'not_verified')
        journal = self.journal()
        self.assertEqual(journal['status'], 'local_candidate_only')
        self.assertTrue(journal['service_state_restored'])
        self.assertEqual(set(journal['artifacts']), {'database', 'configuration', 'local_files', 'recovery_parameters'})
        self.assertFalse(list(self.root.rglob('completion.json')))
        self.assertFalse(list(self.root.rglob('manifest.json')))
        self.assertFalse(backend.enabled)
        self.assertTrue(backend.running)

    def test_original_maintenance_preserved(self):
        backend = FakeBackend(maintenance=True)
        Capture(self.store, backend).start()
        self.assertTrue(backend.enabled)
        self.assertNotIn('maintenance_off', backend.calls)
        self.assertNotIn('maintenance_on', backend.calls)

    def test_dependencies_fail_before_maintenance_is_disabled(self):
        backend = FakeBackend()
        def unavailable(original):
            raise CaptureFailure('DEPENDENCY_STATE_NOT_RESTORED')
        backend.assert_original_services = unavailable
        self.assert_code('RECOVERY_REQUIRED', lambda: Capture(self.store, backend).start())
        self.assertTrue(backend.enabled)
        self.assertNotIn('maintenance_off', backend.calls)
        self.assertEqual(self.journal()['status'], 'incomplete')

    def test_original_stopped_writer_not_started(self):
        backend = FakeBackend(running=False)
        Capture(self.store, backend).start()
        self.assertFalse(backend.running)
        self.assertNotIn('start', backend.calls)

    def test_unique_runs_never_overwrite_or_mix(self):
        first = Capture(self.store, FakeBackend()).start()
        second = Capture(self.store, FakeBackend()).start()
        self.assertNotEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(len(list(self.root.glob('*/database.dump'))), 2)

    def test_staging_overlap_rejected_before_maintenance(self):
        backend = FakeBackend()
        original = backend.preflight
        def overlap():
            value = original()
            value['capture_sources'] = [str(self.root)]
            return value
        backend.preflight = overlap
        self.assert_code('STAGING_SOURCE_OVERLAP', lambda: Capture(self.store, backend).start())
        self.assertNotIn('maintenance_on', backend.calls)

    def test_low_staging_disk_before_maintenance(self):
        backend = FakeBackend()
        from types import SimpleNamespace
        with patch('scripts.backup.capture.engine.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            self.assert_code('INSUFFICIENT_STAGING_DISK', lambda: Capture(self.store, backend).start())
        self.assertNotIn('maintenance_on', backend.calls)

    def test_timeout_aborts_candidate_and_restores(self):
        backend = FakeBackend()
        def timeout(stream):
            raise CaptureFailure('COMMAND_TIMEOUT')
        backend.dump = timeout
        self.assert_code('COMMAND_TIMEOUT', lambda: Capture(self.store, backend).start())
        self.assertEqual(self.journal()['status'], 'incomplete')
        self.assertFalse(backend.enabled)

    def test_sequence_requires_quiescence_before_dump(self):
        backend = FakeBackend()
        Capture(self.store, backend).start()
        self.assertLess(backend.calls.index('stop'), backend.calls.index('quiescent'))
        self.assertLess(backend.calls.index('quiescent'), backend.calls.index('dump'))
        self.assertLess(backend.calls.index('recovery_parameters'), backend.calls.index('start'))

    def test_intent_durable_before_side_effect(self):
        backend = FakeBackend()
        original_stop = backend.stop_writer
        def stop(service):
            self.assertEqual(self.journal()['phase'], 'stop_intent')
            self.assertIn(service, self.journal()['stop_intents'])
            original_stop(service)
        backend.stop_writer = stop
        Capture(self.store, backend).start()

    def test_lock_exclusion_and_release(self):
        other = Store(self.root, synthetic=True)
        with self.store.lock():
            self.assert_code('CAPTURE_BUSY', lambda: Capture(other, FakeBackend()).start())
        self.assertEqual(Capture(other, FakeBackend()).start()['code'], 'LOCAL_CANDIDATE_ONLY')

    def test_cancellation_restores_state_and_preserves_partial(self):
        backend = FakeBackend()
        def cancelled(stream):
            stream.write(b'PGDMP partial')
            raise CaptureFailure('CAPTURE_CANCELLED')
        backend.dump = cancelled
        self.assert_code('CAPTURE_CANCELLED', lambda: Capture(self.store, backend).start())
        self.assertTrue(backend.running)
        self.assertFalse(backend.enabled)
        self.assertTrue(list(self.root.glob('*/database.dump.partial')))
        self.assertEqual(self.journal()['status'], 'incomplete')

    def test_duplicate_cancel_ignored_during_failure_journal_and_cleanup(self):
        backend = FakeBackend(failure='dump')
        release = self.store.release_reserve
        def guarded(directory):
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            release(directory)
        self.store.release_reserve = guarded
        self.assert_code('SYNTHETIC_DUMP', lambda: Capture(self.store, backend).start())
        self.assertFalse(backend.enabled)

    def test_disk_error_restores_from_reserved_space(self):
        backend = FakeBackend()
        def no_space(stream):
            stream.write(b'partial')
            raise OSError(28, 'synthetic-sensitive-path')
        backend.dump = no_space
        self.assert_code('INSUFFICIENT_DISK', lambda: Capture(self.store, backend).start())
        self.assertTrue(backend.running)
        self.assertFalse(backend.enabled)
        self.assertEqual(next(self.root.glob('*/journal.reserve')).stat().st_size, 0)
        self.assertNotIn('synthetic-sensitive-path', json.dumps(self.journal()))

    def test_foreign_maintenance_change_not_reverted(self):
        backend = FakeBackend()
        def changed(stream):
            backend.extra = '-operator-change'
            raise CaptureFailure('DUMP_FAILED')
        backend.dump = changed
        self.assert_code('RECOVERY_REQUIRED', lambda: Capture(self.store, backend).start())
        self.assertTrue(backend.enabled)
        self.assertNotIn('maintenance_off', backend.calls)
        self.assertFalse(self.store.read('active.json')['terminal'])

    def test_foreign_maintenance_between_preflight_and_intent_not_owned(self):
        backend = FakeBackend()
        original = backend.preflight
        def changed():
            snapshot = original()
            backend.enabled = True
            return snapshot
        backend.preflight = changed
        self.assert_code('RECOVERY_REQUIRED', lambda: Capture(self.store, backend).start())
        self.assertNotIn('maintenance_on', backend.calls)
        self.assertNotIn('maintenance_off', backend.calls)

    def test_identity_change_not_recovered(self):
        backend = FakeBackend()
        def changed(stream):
            backend.identity = 'different'
            raise CaptureFailure('DUMP_FAILED')
        backend.dump = changed
        self.assert_code('RECOVERY_REQUIRED', lambda: Capture(self.store, backend).start())
        self.assertNotIn('start', backend.calls)

    def interrupted(self, backend, phase='quiesced', fingerprint='enabled'):
        operation_id = '20261009T120000Z-11111111-2222-4333-8444-555555555555'
        directory = self.store.operation(operation_id)
        directory.mkdir(mode=0o700)
        journal = {'internal_version': 1, 'kind': 'local-capture-journal', 'operation_id': operation_id,
                   'consistency_id': 'capture-synthetic', 'original': {'maintenance': False,
                   'writers': {'nextcloud': True}, 'identity': backend.identity}, 'events': [],
                   'phase': phase, 'maintenance_fingerprint': fingerprint, 'stop_intents': ['nextcloud'],
                   'status': 'incomplete', 'artifacts': {}}
        self.store.write(operation_id + '/journal.json', journal)
        self.store.write('active.json', {'operation_id': operation_id, 'terminal': False})

    def test_recovery_abandons_interrupted_capture_without_dump(self):
        backend = FakeBackend(maintenance=True, running=False)
        self.interrupted(backend)
        result = Capture(self.store, backend).recover()
        self.assertEqual(result['code'], 'SERVICE_STATE_RECOVERED_CAPTURE_ABANDONED')
        self.assertNotIn('dump', backend.calls)
        self.assertFalse(backend.enabled)
        self.assertTrue(backend.running)
        self.assertEqual(Capture(self.store, backend).recover()['code'], 'ALREADY_TERMINAL')

    def test_new_capture_refuses_unresolved_active_journal(self):
        backend = FakeBackend(maintenance=True, running=False)
        self.interrupted(backend)
        self.assert_code('RECOVERY_REQUIRED', lambda: Capture(self.store, backend).start())
        self.assertNotIn('preflight', backend.calls)

    def test_uncertain_maintenance_intent_requires_intervention(self):
        backend = FakeBackend(maintenance=True, running=False)
        self.interrupted(backend, 'maintenance_intent', None)
        self.assert_code('MAINTENANCE_OWNERSHIP_UNCERTAIN', lambda: Capture(self.store, backend).recover())
        self.assertTrue(backend.enabled)

    def test_recovery_mixed_journal_rejected(self):
        backend = FakeBackend(maintenance=True, running=False)
        self.interrupted(backend)
        journal = self.journal()
        journal['operation_id'] = 'different'
        active = self.store.read('active.json')
        self.store.write(active['operation_id'] + '/journal.json', journal)
        self.assert_code('JOURNAL_MISMATCH', lambda: Capture(self.store, backend).recover())

    def test_malformed_recovery_controls_rejected_before_backend_effects(self):
        backend = FakeBackend(maintenance=True, running=False)
        self.interrupted(backend)
        baseline = self.journal()
        changes = [{'stop_intents': ['postgres']}, {'stop_intents': ['nextcloud', 'nextcloud']},
                   {'internal_version': True}, {'kind': 'completion'}, {'events': {}},
                   {'maintenance_fingerprint': 123}, {'status': 'complete'},
                   {'original': {'maintenance': 'false', 'writers': {'nextcloud': True}, 'identity': backend.identity}}]
        active = self.store.read('active.json')
        for changeset in changes:
            with self.subTest(changeset=changeset):
                value = dict(baseline, **changeset)
                self.store.write(active['operation_id'] + '/journal.json', value)
                self.assert_code('JOURNAL_MISMATCH', lambda: Capture(self.store, backend).recover())
                self.assertFalse(backend.calls)

    def test_interrupted_stage_matrix_abandons_without_repeating_capture(self):
        for phase in ('maintenance_owned', 'stop_intent', 'quiesced', 'dump_intent',
                      'dump_catalog_checked', 'configuration_intent', 'local_files_intent',
                      'recovery_parameters_intent', 'local_capture_finished', 'restore_intent'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                os.chmod(path, 0o700)
                store = Store(path, synthetic=True)
                old = self.store
                self.store = store
                try:
                    backend = FakeBackend(maintenance=True, running=False)
                    self.interrupted(backend, phase)
                    result = Capture(store, backend).recover()
                    self.assertFalse(result['backup_complete'])
                    self.assertNotIn('dump', backend.calls)
                    self.assertNotIn('configuration', backend.calls)
                    self.assertFalse(backend.enabled)
                finally:
                    self.store = old

    def test_crash_after_maintenance_off_without_fingerprint_is_ambiguous(self):
        backend = FakeBackend(maintenance=False, running=True)
        self.interrupted(backend, 'maintenance_restore_intent', 'enabled')
        self.assert_code('MAINTENANCE_OWNERSHIP_CHANGED', lambda: Capture(self.store, backend).recover())
        self.assertFalse(backend.calls)

    def test_abrupt_child_exit_releases_os_lock(self):
        code = ('import os,sys; from scripts.backup.capture.storage import Store; '
                's=Store(sys.argv[1],synthetic=True); c=s.lock(); c.__enter__(); '
                'print("LOCKED",flush=True); sys.stdin.readline(); os._exit(99)')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            out, _ = child.communicate(input=b'finish\n', timeout=10)
            self.assertEqual((child.returncode, out.strip()), (99, b'LOCKED'))
            with self.store.lock():
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_artifact_limit_checked_before_write(self):
        directory = self.root / 'synthetic'
        directory.mkdir()
        self.assert_code('ARTIFACT_LIMIT', lambda: self.store.artifact(directory, 'file', lambda s: s.write(b'12345'), 4))
        self.assertEqual((directory / 'file.partial').stat().st_size, 0)

    def test_no_overwrite_existing_artifact(self):
        directory = self.root / 'synthetic'
        directory.mkdir()
        (directory / 'file').write_bytes(b'original')
        self.assert_code('ARTIFACT_ALREADY_EXISTS', lambda: self.store.artifact(directory, 'file', lambda s: s.write(b'new'), 4))
        self.assertEqual((directory / 'file').read_bytes(), b'original')

    def test_pending_journal_is_not_silently_overwritten(self):
        path = self.root / 'journal.json'
        path.with_name('journal.json.pending').write_bytes(b'interrupted')
        with self.assertRaises(FileExistsError):
            atomic_json(path, {'example': True})
        self.assertEqual(path.with_name('journal.json.pending').read_bytes(), b'interrupted')

    def test_journal_traversal_rejected(self):
        with self.assertRaises(ValidationFailure) as caught:
            self.store.write('../escape.json', {})
        self.assertEqual(caught.exception.code, 'UNSAFE_PATH')

    def test_journal_parent_symlink_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (self.root / 'link').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('synthetic symlink creation unavailable')
        with self.assertRaises(ValidationFailure) as caught:
            self.store.write('link/journal.json', {})
        self.assertEqual(caught.exception.code, 'UNSAFE_ROOT')
        self.assertFalse((outside / 'journal.json').exists())

    def test_reserve_hardlink_never_truncates_foreign_file(self):
        directory = self.root / 'synthetic'
        directory.mkdir()
        original = self.root / 'protected'
        original.write_bytes(b'preserve')
        os.link(original, directory / 'journal.reserve')
        with self.assertRaises(ValidationFailure) as caught:
            self.store.release_reserve(directory)
        self.assertEqual(caught.exception.code, 'UNSAFE_FILE')
        self.assertEqual(original.read_bytes(), b'preserve')

    def test_artifact_modification_during_hash_fails(self):
        directory = self.root / 'synthetic'
        directory.mkdir()
        from scripts.backup.capture.storage import signature
        calls = 0
        def changed(stream):
            nonlocal calls
            calls += 1
            value = signature(stream)
            return value if calls < 3 else (*value[:-1], value[-1] + 1)
        with patch('scripts.backup.capture.storage.signature', side_effect=changed):
            self.assert_code('ARTIFACT_CHANGED', lambda: self.store.artifact(directory, 'file', lambda s: s.write(b'contents'), 100))
        self.assertFalse((directory / 'file').exists())
        self.assertTrue((directory / 'file.partial').exists())

    def test_archive_includes_identity_apps_themes_data_and_permissions(self):
        source = self.root / 'html'
        for directory in ('config', 'custom_apps', 'themes', 'data'):
            (source / directory).mkdir(parents=True)
            (source / directory / 'synthetic').write_bytes(b'synthetic-sensitive-content')
        (source / 'config/.platform-storage.json').write_bytes(b'{}')
        output = io.BytesIO()
        archive_tree(source, output)
        with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
            names = archive.getnames()
            for expected in ('config/synthetic', 'config/.platform-storage.json', 'custom_apps/synthetic', 'themes/synthetic', 'data/synthetic'):
                self.assertIn(expected, names)

    def test_archive_links_require_review(self):
        source = self.root / 'html'
        source.mkdir()
        try:
            (source / 'external').symlink_to(self.root / 'outside')
        except OSError:
            self.skipTest('synthetic symlink creation unavailable')
        self.assert_code('LOCAL_LINK_REQUIRES_REVIEW', lambda: archive_tree(source, io.BytesIO()))

    def test_archive_new_file_during_traversal_rejected(self):
        source = self.root / 'html'
        source.mkdir()
        (source / 'initial').write_bytes(b'initial')
        real_walk = os.walk
        def changed_walk(*args, **kwargs):
            for current, directories, files in real_walk(*args, **kwargs):
                (source / 'late-file').write_bytes(b'late')
                yield current, directories, files
        with patch('scripts.backup.capture.archive.os.walk', side_effect=changed_walk):
            self.assert_code('LOCAL_TREE_CHANGED', lambda: archive_tree(source, io.BytesIO()))

    def test_cli_rejects_operational_project_before_docker(self):
        with patch('subprocess.Popen', side_effect=AssertionError('must not contact Docker')), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['start', '--isolated-project', 'nextcloud-platform', '--fixture-root', str(self.root), '--staging', str(self.root)]), 2)
        self.assertNotIn(str(self.root), out.getvalue())
        self.assertIn('ISOLATED_PROJECT_REQUIRED', out.getvalue())

    def test_cli_argument_errors_sanitized(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['--unknown', 'synthetic-sensitive-content']), 2)
        self.assertNotIn('synthetic-sensitive-content', out.getvalue())

    def test_cleanup_restores_signal_handlers(self):
        before = signal.getsignal(signal.SIGTERM)
        with cleanup_signals():
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


for stage in ('preflight', 'maintenance_on', 'stop', 'quiescent', 'dump', 'catalog',
              'configuration', 'local_files', 'recovery_parameters'):
    def test(self, stage=stage):
        backend = FakeBackend(failure=stage)
        self.assert_code('SYNTHETIC_' + stage.upper(), lambda: Capture(self.store, backend).start())
        self.assertTrue(backend.running)
        self.assertFalse(backend.enabled)
    setattr(CaptureTests, 'test_failure_' + stage, test)


if __name__ == '__main__':
    unittest.main()
