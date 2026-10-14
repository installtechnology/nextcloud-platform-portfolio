"""Offline synthetic wheels only: never execute preparation, pip, or downloads."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.backup.orchestration.linux import prepare_posix as preparation


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # packaging evaluates its default host environment before merging the
        # explicit target. Avoid Windows version-shell probing in these mocks.
        parser_environment = patch('pip._vendor.packaging.markers.default_environment', return_value={})
        parser_environment.start()
        self.addCleanup(parser_environment.stop)

    def spec(self, change=None, lock_change=None):
        data = json.loads((preparation.HERE / 'dependencies.provenance.json').read_text())
        if change:
            change(data)
        provenance = self.root / 'provenance.json'
        provenance.write_text(json.dumps(data))
        lock = self.root / 'lock'
        lines = [f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in data['records']]
        lock.write_text('\n'.join(lines if not lock_change else lock_change(lines)))
        return lock, provenance

    def reject(self, function, code):
        with self.assertRaises(preparation.PreparationFailure) as error:
            function()
        self.assertEqual(str(error.exception), code)

    def wheel(self, metadata=None, extra=None):
        filename = self.root / 'fixture.whl'
        metadata = metadata or b'Metadata-Version: 2.1\nName: fixture\nVersion: 1.0\nRequires-Python: >=3.9\n\n'
        with zipfile.ZipFile(filename, 'w') as archive:
            archive.writestr('fixture-1.0.dist-info/METADATA', metadata)
            archive.writestr('fixture/__init__.py', '# synthetic fixture, never imported\n')
            if extra:
                archive.writestr(extra, 'synthetic')
        data = filename.read_bytes()
        record = {'name': 'fixture', 'version': '1.0', 'sha256': hashlib.sha256(data).hexdigest(),
                  'size_bytes': len(data), 'requires_python': '>=3.9', 'requires_dist': []}
        return filename, record

    def test_existing_lock_closure_without_download(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('process forbidden')):
            self.assertEqual(len(preparation.load_spec()), 8)

    def test_import_has_no_effects(self):
        source = compile(Path(preparation.__file__).read_text(), preparation.__file__, 'exec')
        with patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('process forbidden')), \
             patch('pathlib.Path.mkdir', side_effect=AssertionError('write forbidden')):
            exec(source, {'__name__': 'synthetic_import', '__file__': preparation.__file__})

    def test_target_mismatch(self):
        pair = self.spec(lambda data: data.update(target='windows'))
        self.reject(lambda: preparation.load_spec(*pair), 'TARGET_MISMATCH')

    def test_lock_mismatch(self):
        pair = self.spec(lock_change=lambda lines: lines[:-1])
        self.reject(lambda: preparation.load_spec(*pair), 'LOCK_PROVENANCE_MISMATCH')

    def test_duplicate_package(self):
        pair = self.spec(lambda data: data['records'].__setitem__(1, copy.deepcopy(data['records'][0])))
        self.reject(lambda: preparation.load_spec(*pair), 'DUPLICATE_PACKAGE')

    def test_wrong_dependency_version(self):
        pair = self.spec(lambda data: data['records'][0].update(version='1.0', filename='attrs-1.0-py3-none-any.whl',
                           metadata_url='https://pypi.org/pypi/attrs/1.0/json',
                           url='https://files.pythonhosted.org/packages/attrs-1.0-py3-none-any.whl'))
        self.reject(lambda: preparation.load_spec(*pair), 'DEPENDENCY_CLOSURE_MISMATCH')

    def test_untrusted_download_host(self):
        pair = self.spec(lambda data: data['records'][0].update(url='https://example.invalid/wheel'))
        self.reject(lambda: preparation.load_spec(*pair), 'UNTRUSTED_WHEEL_URL')

    def test_incompatible_wheel_abi(self):
        def change(data):
            record = data['records'][5]
            record['filename'] = record['filename'].replace('cp312-cp312', 'cp311-cp311')
            record['url'] = 'https://files.pythonhosted.org/packages/' + record['filename']
        self.reject(lambda: preparation.load_spec(*self.spec(change)), 'WHEEL_TARGET_MISMATCH')

    def test_duplicate_json_key(self):
        pair = self.spec()
        pair[1].write_text('{"target":"first","target":"second"}')
        self.reject(lambda: preparation.load_spec(*pair), 'DUPLICATE_JSON_KEY')

    def test_document_size_limit(self):
        path = self.root / 'large'
        path.write_bytes(b'x' * 100)
        self.reject(lambda: preparation.safe_bytes(path, 10), 'UNSAFE_INPUT_FILE')

    def test_valid_synthetic_wheel(self):
        preparation.verify_wheel(*self.wheel())

    def test_wrong_hash(self):
        path, record = self.wheel()
        record['sha256'] = '0' * 64
        self.reject(lambda: preparation.verify_wheel(path, record), 'WHEEL_HASH_MISMATCH')

    def test_truncated_bytes(self):
        path, record = self.wheel()
        path.write_bytes(path.read_bytes()[:-1])
        self.reject(lambda: preparation.verify_wheel(path, record), 'WHEEL_FILE_INVALID')

    def test_wrong_metadata_identity(self):
        path, record = self.wheel(b'Name: wrong\nVersion: 1.0\nRequires-Python: >=3.9\n\n')
        self.reject(lambda: preparation.verify_wheel(path, record), 'WHEEL_METADATA_MISMATCH')

    def test_wrong_metadata_dependencies(self):
        path, record = self.wheel()
        record['requires_dist'] = ['six>=1']
        self.reject(lambda: preparation.verify_wheel(path, record), 'WHEEL_METADATA_MISMATCH')

    def test_archive_traversal(self):
        path, record = self.wheel(extra='../outside')
        self.reject(lambda: preparation.verify_wheel(path, record), 'WHEEL_UNSAFE_ARCHIVE')

    def test_redirect_rejected_before_following(self):
        self.reject(lambda: preparation.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://169.254.169.254/'),
                    'WHEEL_REDIRECT_REJECTED')

    def test_windows_platform_rejected_before_effects(self):
        with patch.object(preparation.sys, 'platform', 'win32'), \
             patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('process forbidden')):
            self.reject(lambda: preparation.prepare(self.root), 'CPYTHON312_LINUX_AMD64_REQUIRED')

    def test_unknown_error_sanitized(self):
        stream = io.StringIO()
        with patch.object(preparation, 'prepare', side_effect=preparation.PreparationFailure('private-content')), \
             patch('sys.stdout', stream):
            self.assertEqual(preparation.main(['fixture']), 3)
        self.assertNotIn('private-content', stream.getvalue())

    def test_unexpected_exception_sanitized(self):
        stream = io.StringIO()
        with patch.object(preparation, 'prepare', side_effect=RuntimeError('private-content')), patch('sys.stdout', stream):
            self.assertEqual(preparation.main(['fixture']), 3)
        self.assertNotIn('private-content', stream.getvalue())

    def test_prepare_offline_command_sequence_with_no_installation(self):
        records = preparation.load_spec()
        root = self.root / 'nextcloud-posix-fixture'
        def fake_download(directory, record):
            (directory / record['filename']).write_bytes(b'synthetic-not-a-real-wheel')
        with patch.object(preparation, 'check_platform'), \
             patch.object(preparation, 'download_wheel', side_effect=fake_download), \
             patch.dict(preparation.os.environ, {'RUNNER_TEMP': str(self.root.resolve())}), \
             patch.object(preparation.subprocess, 'run') as run:
            run.return_value.returncode = 0
            report = preparation.prepare(root)
        self.assertEqual(report['packages_verified'], 8)
        self.assertEqual(run.call_count, 4)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:4], ['-I', '-m', 'venv'])
        install = commands[1]
        self.assertEqual(install[0], str(root / 'venv/bin/python'))
        for flag in ('--isolated', '--no-index', '--require-hashes', '--only-binary=:all:', '--no-deps', '--no-cache-dir'):
            self.assertIn(flag, install)
        self.assertEqual(install[-1], str(root / 'requirements.lock'))
        self.assertEqual(commands[2][-1], 'check')
        self.assertNotIn('PYTHONPATH', run.call_args.kwargs['env'])
        self.assertNotIn('AWS_ACCESS_KEY_ID', run.call_args.kwargs['env'])
        self.assertEqual((root / 'requirements.lock').read_text().splitlines(),
                         [f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in records])

    def test_existing_directory_not_reused(self):
        root = self.root / 'nextcloud-posix-existing'
        root.mkdir()
        with patch.object(preparation, 'check_platform'), patch.dict(preparation.os.environ, {'RUNNER_TEMP': str(self.root.resolve())}), \
             patch.object(preparation, 'download_wheel', side_effect=AssertionError('download forbidden')):
            self.reject(lambda: preparation.prepare(root), 'PRIVATE_RUNNER_DIRECTORY_REQUIRED')

    def test_download_failure_never_installs(self):
        with patch.object(preparation, 'check_platform'), patch.dict(preparation.os.environ, {'RUNNER_TEMP': str(self.root.resolve())}), \
             patch.object(preparation, 'download_wheel', side_effect=preparation.PreparationFailure('WHEEL_HASH_MISMATCH')), \
             patch.object(preparation.subprocess, 'run', side_effect=AssertionError('install forbidden')):
            self.reject(lambda: preparation.prepare(self.root / 'nextcloud-posix-failure'), 'WHEEL_HASH_MISMATCH')


if __name__ == '__main__':
    unittest.main()
