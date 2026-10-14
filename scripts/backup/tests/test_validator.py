"""Synthetic offline inputs only. No AWS, application or operational data."""
import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup.__main__ import main
from scripts.backup.errors import ValidationFailure
from scripts.backup.integrity import hash_stream
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.safe_files import SafeRoot, relative_parts, _is_reparse
from scripts.backup.schemas import SchemaCatalog, canonical, safe_schema
from scripts.backup.semantics import validate_semantics
from scripts.backup.validator import validate

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / 'contracts/recovery/fixtures'
FILES = {'manifest': 'complete-unproven.json', 'inventory': 'object-inventory.json',
         'completion': 'completion.json', 'evidence': 'restore-evidence.json'}


def sample():
    return {role: json.loads((FIXTURES / filename).read_text(encoding='utf-8')) for role, filename in FILES.items()}


def mutate(document, case):
    parts = case['path'].strip('/').split('/')
    parent = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = parts[-1]
    if isinstance(parent, list):
        if key == '-':
            parent.append(copy.deepcopy(case['value']))
        else:
            parent[int(key)] = copy.deepcopy(case['value'])
    elif case['op'] == 'remove':
        del parent[key]
    else:
        parent[key] = copy.deepcopy(case['value'])


def write_bundle(root, bundle, ciphertext=False):
    if ciphertext:
        (root / 'ciphertext').mkdir(exist_ok=True)
        for role, artifact in bundle['manifest']['artifacts'].items():
            payload = ('synthetic ciphertext ' + role).encode() * 100
            (root / 'ciphertext' / f'{role}.age').write_bytes(payload)
            artifact.update(size_bytes=len(payload), ciphertext_sha256=hashlib.sha256(payload).hexdigest())
        payload = b'synthetic encrypted manifest; NOT an age envelope'
        (root / 'ciphertext/manifest.json.age').write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        bundle['completion']['manifest_ciphertext_sha256'] = digest
        if 'evidence' in bundle:
            bundle['evidence']['manifest_ciphertext_sha256'] = digest
            report = b'synthetic report'
            (root / 'ciphertext/restore-report.age').write_bytes(report)
            bundle['evidence']['report']['ciphertext_sha256'] = hashlib.sha256(report).hexdigest()
    for role, document in bundle.items():
        (root / f'{role}.json').write_text(json.dumps(document), encoding='utf-8')


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = SchemaCatalog()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = sample()

    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(ValidationFailure) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_documents_no_ciphertexts_required(self):
        write_bundle(self.root, self.bundle)
        result = validate(self.root, mode='documents')
        self.assertEqual(result['local_integrity'], 'not_checked')
        self.assertEqual(result['restore_evidence'], 'conformant_declaration')
        self.assertEqual(result['functional_restore'], 'not_verified')

    def test_default_local_requires_ciphertexts(self):
        write_bundle(self.root, self.bundle)
        self.assertEqual(self.assert_code('FILE_MISSING', validate, self.root).exit_code, 4)

    def test_all_ciphertexts_verified_without_restore_claim(self):
        write_bundle(self.root, self.bundle, True)
        result = validate(self.root)
        self.assertEqual(len(result['verified_artifacts']), 7)
        self.assertEqual(result['local_integrity'], 'verified')
        self.assertEqual(result['functional_restore'], 'not_verified')
        self.assertNotIn('destroy_authorized', result)
        self.assertIn('PLAINTEXT_CIPHERTEXT_BINDING_NOT_VERIFIED', result['limitations'])

    def test_no_evidence_required(self):
        del self.bundle['evidence']
        write_bundle(self.root, self.bundle, True)
        self.assertEqual(len(validate(self.root)['verified_artifacts']), 6)

    def test_present_optional_evidence_is_not_ignored(self):
        del self.bundle['evidence']
        write_bundle(self.root, self.bundle)
        (self.root / 'evidence.json').write_text('{', encoding='utf-8')
        self.assert_code('INVALID_JSON', validate, self.root, mode='documents')

    def test_explicit_missing_evidence_rejected(self):
        write_bundle(self.root, self.bundle)
        self.assert_code('FILE_MISSING', validate, self.root, mode='documents', evidence='missing.json')

    def test_missing_required_document(self):
        write_bundle(self.root, self.bundle)
        (self.root / 'completion.json').unlink()
        self.assert_code('FILE_MISSING', validate, self.root, mode='documents')

    def test_size_mismatch(self):
        write_bundle(self.root, self.bundle, True)
        (self.root / 'ciphertext/database.age').write_bytes(b'short')
        self.assert_code('ARTIFACT_SIZE_MISMATCH', validate, self.root)

    def test_hash_mismatch_same_size(self):
        write_bundle(self.root, self.bundle, True)
        path = self.root / 'ciphertext/database.age'
        path.write_bytes(b'x' * path.stat().st_size)
        self.assert_code('ARTIFACT_HASH_MISMATCH', validate, self.root)

    def test_missing_each_ciphertext(self):
        write_bundle(self.root, self.bundle, True)
        for path in (self.root / 'ciphertext').iterdir():
            content = path.read_bytes()
            path.unlink()
            self.assert_code('FILE_MISSING', validate, self.root)
            path.write_bytes(content)

    def test_empty_manifest_ciphertext(self):
        write_bundle(self.root, self.bundle, True)
        (self.root / 'ciphertext/manifest.json.age').write_bytes(b'')
        self.assert_code('ARTIFACT_EMPTY', validate, self.root)

    def test_total_artifact_limit(self):
        write_bundle(self.root, self.bundle, True)
        self.assert_code('ARTIFACT_TOTAL_LIMIT', validate, self.root,
                         limits=replace(Limits(), total_artifact_bytes=1))

    def test_individual_artifact_limit(self):
        write_bundle(self.root, self.bundle, True)
        self.assert_code('ARTIFACT_LIMIT', validate, self.root, limits=replace(Limits(), artifact_bytes=1))

    def test_inventory_limit(self):
        write_bundle(self.root, self.bundle)
        self.assert_code('INVENTORY_LIMIT', validate, self.root, mode='documents',
                         limits=replace(Limits(), inventory_objects=0))

    def test_streaming_reads_bounded_chunks(self):
        path = self.root / 'large'
        payload = b'x' * 10001
        path.write_bytes(payload)
        with path.open('rb') as stream:
            class Reader:
                def fileno(self):
                    return stream.fileno()
                def read(inner, size):
                    self.assertEqual(size, 17)
                    return stream.read(size)
            self.assertEqual(hash_stream(Reader(), replace(Limits(), chunk_bytes=17)),
                             (len(payload), hashlib.sha256(payload).hexdigest()))

    def test_concurrent_artifact_change_rejected(self):
        path = self.root / 'file'
        path.write_bytes(b'abc')
        with path.open('rb') as stream:
            with patch('scripts.backup.integrity.signature', side_effect=[(1, 2, 3, 4, 5), (1, 2, 3, 6, 5)]):
                self.assert_code('FILE_CHANGED', hash_stream, stream, Limits())

    def test_truncated_artifact_stream_even_with_unchanged_metadata(self):
        path = self.root / 'file'
        path.write_bytes(b'abcdef')
        with path.open('rb') as stream:
            class Truncated:
                def fileno(self):
                    return stream.fileno()
                def read(self, size):
                    return stream.read(3) if stream.tell() == 0 else b''
            self.assert_code('FILE_CHANGED', hash_stream, Truncated(), Limits())

    @unittest.skipUnless(os.name == 'posix', 'real concurrent writes require POSIX sharing semantics')
    def test_actual_concurrent_artifact_truncation(self):
        path = self.root / 'file'
        path.write_bytes(b'abcdef')
        with path.open('rb', buffering=0) as stream:
            class Changed:
                def fileno(self):
                    return stream.fileno()
                def read(self, size):
                    data = stream.read(size)
                    if data:
                        with path.open('r+b') as writer:
                            writer.truncate(1)
                    return data
            self.assert_code('FILE_CHANGED', hash_stream, Changed(), replace(Limits(), chunk_bytes=3))

    def test_hash_limit_rejected_before_read(self):
        path = self.root / 'file'
        path.write_bytes(b'abcdef')
        with path.open('rb') as stream:
            class NoRead:
                def fileno(self):
                    return stream.fileno()
                def read(self, size):
                    raise AssertionError('oversized file must not be read')
            self.assert_code('ARTIFACT_LIMIT', hash_stream, NoRead(), replace(Limits(), artifact_bytes=5))

    def test_hash_growth_beyond_limit(self):
        class Growing:
            def read(self, size):
                return b'xx'
        with patch('scripts.backup.integrity.signature', return_value=(1, 2, 1, 4, 5)):
            self.assert_code('ARTIFACT_LIMIT', hash_stream, Growing(), replace(Limits(), artifact_bytes=3))

    def test_actual_size_returned_by_stream_must_match_inspected_size(self):
        write_bundle(self.root, self.bundle, True)
        size = (self.root / 'ciphertext/manifest.json.age').stat().st_size
        digest = self.bundle['completion']['manifest_ciphertext_sha256']
        with patch('scripts.backup.integrity.hash_stream', return_value=(size + 1, digest)):
            self.assert_code('FILE_CHANGED', validate, self.root)

    def test_windows_reparse_attribute_detected_independently_of_symlink_mode(self):
        from types import SimpleNamespace
        self.assertTrue(_is_reparse(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)))
        self.assertFalse(_is_reparse(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)))

    def test_directory_cannot_be_read_as_document(self):
        (self.root / 'manifest.json').mkdir()
        with self.assertRaises(ValidationFailure) as caught:
            validate(self.root, mode='documents')
        self.assertIn(caught.exception.code, ('UNSAFE_FILE', 'FILE_UNREADABLE'))

    def test_nonregular_file_mocked_fstat(self):
        write_bundle(self.root, self.bundle)
        from types import SimpleNamespace
        with SafeRoot(self.root) as root:
            with patch('scripts.backup.safe_files.os.fstat', return_value=SimpleNamespace(st_mode=stat.S_IFIFO, st_nlink=1)):
                self.assert_code('UNSAFE_FILE', lambda: root.open('manifest.json').__enter__())

    @unittest.skipUnless(os.name == 'posix', 'FIFO/socket are POSIX file types')
    def test_real_fifo_and_socket_rejected_without_read(self):
        import socket
        os.mkfifo(self.root / 'fifo')
        with socket.socket(socket.AF_UNIX) as local_socket:
            local_socket.bind(str(self.root / 'socket'))
            with SafeRoot(self.root) as root:
                self.assert_code('UNSAFE_FILE', lambda: root.open('fifo').__enter__())
                self.assert_code('FILE_UNREADABLE', lambda: root.open('socket').__enter__())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing protection')
    def test_windows_write_is_denied_while_read_handle_open(self):
        write_bundle(self.root, self.bundle)
        with SafeRoot(self.root) as root:
            with root.open('manifest.json'):
                with self.assertRaises(OSError):
                    with (self.root / 'manifest.json').open('r+b'):
                        self.fail('write sharing must be denied')

    def test_hardlink_rejected(self):
        write_bundle(self.root, self.bundle)
        (self.root / 'alias').hardlink_to(self.root / 'manifest.json')
        self.assert_code('UNSAFE_FILE', validate, self.root, mode='documents')

    def test_symlink_escape_rejected(self):
        write_bundle(self.root, self.bundle)
        try:
            (self.root / 'link').symlink_to(self.root / 'manifest.json')
        except OSError:
            self.skipTest('OS does not permit creating synthetic symlinks')
        self.assert_code('UNSAFE_PATH', validate, self.root, mode='documents', manifest='link')

    def test_directory_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            write_bundle(Path(outside), self.bundle)
            try:
                (self.root / 'escape').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('OS does not permit creating synthetic symlinks')
            self.assert_code('UNSAFE_PATH', validate, self.root, mode='documents', manifest='escape/manifest.json')

    def test_root_symlink_rejected(self):
        (self.root / 'real').mkdir()
        try:
            (self.root / 'alias').symlink_to(self.root / 'real', target_is_directory=True)
        except OSError:
            self.skipTest('OS does not permit creating synthetic symlinks')
        self.assert_code('UNSAFE_ROOT', SafeRoot, self.root / 'alias')

    def test_document_changed_between_validation_steps(self):
        write_bundle(self.root, self.bundle)
        def changed(bundle):
            validate_semantics(bundle)
            (self.root / 'manifest.json').write_text('{}', encoding='utf-8')
        with patch('scripts.backup.validator.validate_semantics', side_effect=changed):
            self.assert_code('FILE_CHANGED', validate, self.root, mode='documents')

    def test_optional_evidence_created_mid_validation_rejected(self):
        del self.bundle['evidence']
        write_bundle(self.root, self.bundle)
        def changed(bundle):
            validate_semantics(bundle)
            (self.root / 'evidence.json').write_text('{}', encoding='utf-8')
        with patch('scripts.backup.validator.validate_semantics', side_effect=changed):
            self.assert_code('FILE_CHANGED', validate, self.root, mode='documents')

    def test_json_short_read_even_with_unchanged_metadata(self):
        from scripts.backup.json_input import load_document
        from contextlib import contextmanager
        class ShortRoot:
            @contextmanager
            def open(self, path):
                yield io.BytesIO(b'{}')
        with patch('scripts.backup.json_input.signature', return_value=(1, 2, 4, 5, 6)):
            self.assert_code('FILE_CHANGED', load_document, ShortRoot(), 'manifest.json', 'manifest')

    def test_json_changed_during_read(self):
        from scripts.backup.json_input import load_document
        write_bundle(self.root, self.bundle)
        with SafeRoot(self.root) as root:
            with patch('scripts.backup.json_input.signature', side_effect=[(1, 2, 3, 4, 5), (1, 2, 3, 6, 5)]):
                self.assert_code('FILE_CHANGED', load_document, root, 'manifest.json', 'manifest')

    def test_invalid_inventory_type_is_schema_error(self):
        self.bundle['inventory']['objects'] = None
        write_bundle(self.root, self.bundle)
        self.assert_code('SCHEMA_INVALID', validate, self.root, mode='documents')

    def test_cli_semantic_integrity_internal_and_interrupt_codes(self):
        for error, expected in ((ValidationFailure('MIXED_SET', 3), 3),
                                (ValidationFailure('FILE_MISSING', 4), 4),
                                (RuntimeError('synthetic-sensitive-value'), 5),
                                (KeyboardInterrupt(), 130)):
            with patch('scripts.backup.__main__.validate', side_effect=error), patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['--root', str(self.root)]), expected)
                self.assertNotIn('synthetic-sensitive-value', out.getvalue())

    def test_dependency_missing_fails_closed(self):
        import builtins
        original = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'jsonschema':
                raise ImportError('synthetic')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=missing):
            self.assertEqual(self.assert_code('DEPENDENCY_MISSING', SchemaCatalog).exit_code, 5)

    def test_no_network_or_subprocess_needed(self):
        write_bundle(self.root, self.bundle, True)
        with (patch('socket.socket', side_effect=AssertionError('network forbidden')),
              patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden'))):
            self.assertTrue(validate(self.root)['ok'])

    def test_cli_success(self):
        write_bundle(self.root, self.bundle)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--root', str(self.root), '--mode', 'documents']), 0)
        self.assertTrue(json.loads(output.getvalue())['ok'])

    def test_cli_errors_never_echo_inputs(self):
        secret = 'synthetic-sensitive-input'
        for args in (['--unknown', secret], ['--root', str(self.root), '--manifest', '../' + secret]):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(args), 2)
            self.assertNotIn(secret, output.getvalue())

    def test_unknown_property_value_not_logged(self):
        self.bundle['manifest']['synthetic-private-key'] = 'synthetic-sensitive-value'
        write_bundle(self.root, self.bundle)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--root', str(self.root), '--mode', 'documents']), 2)
        self.assertNotIn('synthetic-sensitive-value', output.getvalue())
        self.assertNotIn('synthetic-private-key', output.getvalue())

    def test_cli_no_sensitive_output_on_either_stream(self):
        self.bundle['manifest']['unknown-sensitive-field'] = 'synthetic-sensitive-value'
        write_bundle(self.root, self.bundle)
        with (patch('sys.stdout', new_callable=io.StringIO) as stdout,
              patch('sys.stderr', new_callable=io.StringIO) as stderr):
            self.assertEqual(main(['--root', str(self.root), '--mode', 'documents']), 2)
        combined = stdout.getvalue() + stderr.getvalue()
        for value in ('unknown-sensitive-field', 'synthetic-sensitive-value', str(self.root)):
            self.assertNotIn(value, combined)
        self.assertEqual(stderr.getvalue(), '')

    def test_unknown_schema_version(self):
        self.bundle['manifest']['schema_version'] = 2
        self.assert_code('SCHEMA_INVALID', self.catalog.validate, 'manifest', self.bundle['manifest'])

    def test_empty_complete_inventory(self):
        self.bundle['inventory'].update(objects=[], object_count=0, absent_current_keys=[])
        write_bundle(self.root, self.bundle)
        self.assertTrue(validate(self.root, mode='documents')['ok'])

    def test_opaque_s3_key_never_used_as_path(self):
        self.bundle['inventory']['objects'][0]['key'] = '../../synthetic-object-key'
        write_bundle(self.root, self.bundle)
        self.assertTrue(validate(self.root, mode='documents')['ok'])

    def test_unique_items_json_equality(self):
        self.assertEqual(canonical(1), canonical(1.0))
        self.assertNotEqual(canonical(True), canonical(1))
        self.assertEqual(canonical({'a': 1, 'b': 2}), canonical({'b': 2, 'a': 1}))

    def test_remote_schema_reference_rejected(self):
        self.assert_code('SCHEMA_REFERENCE_FORBIDDEN', safe_schema,
                         {'properties': {'x': {'$ref': 'https://invalid.example/schema'}}})


def negative_test(case):
    def test(self):
        bundle = sample()
        mutate(bundle[case['document']], case)
        for extra in case.get('additional_mutations', []):
            mutate(bundle[case['document']], extra)
        if case['rejection_layer'] == 'schema':
            # Check the prescribed keyword, even when another schema error precedes it.
            errors = list(self.catalog.validators[case['document']].iter_errors(bundle[case['document']]))
            self.assertIn(case['expected_validator'], {e.validator for e in errors})
            self.assert_code('SCHEMA_INVALID', self.catalog.validate, case['document'], bundle[case['document']])
        else:
            for role, document in bundle.items():
                self.catalog.validate(role, document)
            self.assert_code(case['expected_error'], validate_semantics, bundle)
    return test


for case in json.loads((FIXTURES / 'rejection-cases.json').read_text(encoding='utf-8')):
    setattr(ValidatorTests, 'test_pr1_' + case['name'], negative_test(case))


class InputTests(unittest.TestCase):
    def assert_code(self, code, data, limits=None):
        with self.assertRaises(ValidationFailure) as caught:
            parse_json(data, limits or Limits())
        self.assertEqual(caught.exception.code, code)

    def test_duplicate_nested_keys(self):
        self.assert_code('DUPLICATE_JSON_KEY', b'{"a":{"b":1,"b":2}}')

    def test_depth_limit(self):
        self.assert_code('JSON_LIMIT', b'{"a":' + b'[' * 40 + b'0' + b']' * 40 + b'}')

    def test_byte_limit(self):
        self.assert_code('JSON_LIMIT', b'{"a":0}', replace(Limits(), json_bytes=1))

    def test_string_limit(self):
        self.assert_code('JSON_LIMIT', b'{"a":"12345"}', replace(Limits(), string_chars=4))

    def test_array_limit(self):
        self.assert_code('JSON_LIMIT', b'{"a":[0,1]}', replace(Limits(), array_items=1))

    def test_node_limit(self):
        self.assert_code('JSON_LIMIT', b'{"a":[0,1]}', replace(Limits(), nodes=2))

    def test_node_limit_before_decoder_allocates(self):
        with patch('scripts.backup.json_input.json.loads', side_effect=AssertionError('decoder must not run')):
            self.assert_code('JSON_LIMIT', b'{"a":[' + b'0,' * 100 + b'0]}', replace(Limits(), nodes=20))

    def test_node_limit_includes_object_keys(self):
        with patch('scripts.backup.json_input.json.loads', side_effect=AssertionError('decoder must not run')):
            self.assert_code('JSON_LIMIT', b'{"a":0,"b":0}', replace(Limits(), nodes=4))

    def test_large_integer(self):
        self.assert_code('JSON_LIMIT', b'{"a":' + b'1' * 129 + b'}')

    def test_valid_quotes_brackets_escaped(self):
        self.assertEqual(parse_json(b'{"a":"[\\\"{"}', Limits()), {'a': '["{'})

    def test_exact_integral_json_numbers(self):
        self.assertEqual(parse_json(b'{"a":1.0,"b":1e2}', Limits()), {'a': 1, 'b': 100})

    def test_float_rounding_cannot_hide_fraction(self):
        self.assert_code('INVALID_JSON', b'{"a":1.00000000000000001}')

    def test_malformed_decimal_exponent(self):
        self.assert_code('INVALID_JSON', b'{"a":1e9999999999999999999999}')

    def test_paths(self):
        for value in ('../x', '/x', 'C:/x', 'C:x', 'a\\b', 'a//b', 'a/./b', 'a/../b',
                      '//server/share', 'a:stream', 'CON', 'nul.txt', 'a.', 'a ', 'a\x00', '', 'a?'):
            with self.subTest(value=value), self.assertRaises(ValidationFailure) as caught:
                relative_parts(value)
            self.assertEqual(caught.exception.code, 'UNSAFE_PATH')


for name, data, code in (
    ('invalid_utf8', b'{"x":"\xff"}', 'INVALID_JSON'),
    ('malformed', b'{', 'INVALID_JSON'),
    ('bom', b'\xef\xbb\xbf{}', 'INVALID_JSON'),
    ('nan', b'{"a":NaN}', 'INVALID_JSON'),
    ('infinity', b'{"a":Infinity}', 'INVALID_JSON'),
    ('overflow', b'{"a":1e999}', 'JSON_LIMIT'),
    ('surrogate', b'{"a":"\\ud800"}', 'INVALID_JSON'),
    ('array_root', b'[]', 'JSON_OBJECT_REQUIRED'),
):
    def test(self, data=data, code=code):
        self.assert_code(code, data)
    setattr(InputTests, 'test_' + name, test)


if __name__ == '__main__':
    unittest.main()
