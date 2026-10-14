import ast
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.backup.sealing.common import Bounds, json_bytes
from scripts.backup.sealing.completion import SimulatedSeal
from scripts.backup.sealing.crypto import Age
from scripts.backup.sealing.inventory import inventory, checksum
from scripts.backup.sealing.tests.support import Assertions, ROOT, SOURCE, TEMPLATE, age_tools, candidate
from scripts.backup.sealing.transport import Transport


class SecurityTests(Assertions, unittest.TestCase):
    def test_bound_configuration_rejects_unbounded_memory_and_retries(self):
        for kwargs in ({'chunk_bytes': 9 * 1024 ** 2}, {'attempts': 100}, {'records': 20001},
                       {'part_bytes': 1}, {'artifact_bytes': 9 * 1024 ** 3}, {'pages': True},
                       {'timeout_seconds': 3601}, {'multipart_threshold': 65 * 1024 ** 2}):
            with self.subTest(config=kwargs):
                self.code('INVALID_LIMITS', lambda: Bounds(**kwargs))

    def test_json_duplicate_and_deep_documents_use_existing_guard(self):
        from scripts.backup.json_input import parse_json
        self.code('DUPLICATE_JSON_KEY', lambda: parse_json(b'{"a":1,"a":2}'))
        self.code('JSON_LIMIT', lambda: parse_json(b'[' * 40 + b'0' + b']' * 40))

    def test_product_has_no_cloud_clients_private_key_or_decryption_command(self):
        for path in (ROOT / 'scripts/backup/sealing').glob('*.py'):
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    name = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                    self.assertNotIn(name.split('.')[0], {'boto3', 'botocore', 'requests', 'urllib', 'socket', 'http'})
            self.assertNotIn("'--decrypt'", source)
            self.assertNotIn("'--identity'", source)
            self.assertNotIn('delete_object', source)

    def test_product_imports_do_not_spawn_processes(self):
        script = ('import subprocess, importlib\n'
                  'def forbidden(*a, **kw): raise AssertionError("Import side effect")\n'
                  'subprocess.Popen = forbidden\n'
                  'for name in ("common", "inventory", "crypto", "simulator", "transport", "hooks", "completion"):\n'
                  ' importlib.import_module("scripts.backup.sealing." + name)\n')
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=20, cwd=ROOT)
        self.assertEqual(result.returncode, 0, 'Import had a side effect')

    def test_oversized_json_projection_rejected(self):
        self.code('JSON_LIMIT', lambda: json_bytes({'value': 'x' * 4097}))

    def test_malformed_head_not_accepted(self):
        api, _, _, bid, _, _ = candidate(self)
        api.seed(SOURCE['data_bucket'], 'k', b'fixture')
        with patch.object(api, 'head', return_value=None):
            self.code('DATA_VERSION_CHANGED', lambda: inventory(api, SOURCE, bid, 'capture-fixture'))

    def test_multiple_checksum_algorithms_rejected_as_ambiguous(self):
        self.code('INVALID_CHECKSUM', lambda: checksum({'ChecksumSHA256': 'x', 'ChecksumCRC32': 'y',
                                                        'ChecksumType': 'FULL_OBJECT'}))

    def test_sealing_lock_conflict_refuses_concurrent_controller(self):
        api, store, _, bid, _, _ = candidate(self)
        age, recipient, _ = age_tools(self)
        seal = SimulatedSeal(api, store, age, SOURCE, TEMPLATE['compatibility'], TEMPLATE['scope'],
                             recipient, 'synthetic-recipient', 'fixture-platform-backups')
        with store.lock():
            self.code('CAPTURE_BUSY', lambda: seal.seal(bid))

    def test_hardlinked_source_rejected_before_encryption(self):
        _, store, _, bid, _, _ = candidate(self)
        age, recipient, _ = age_tools(self)
        directory = store.operation(bid)
        os.link(directory / 'database.dump', directory / 'linked.dump')
        self.code('UNSAFE_FILE', lambda: age.encrypt(directory, 'linked.dump', 'linked.age', recipient))

    def test_plaintext_path_traversal_rejected(self):
        _, store, _, bid, _, _ = candidate(self)
        age, recipient, _ = age_tools(self)
        self.code('UNSAFE_PATH', lambda: age.encrypt(store.operation(bid), '../outside', 'file.age', recipient))

    def test_interrupted_journal_mixed_backup_id_rejected(self):
        api, store, _, bid, _, _ = candidate(self)
        transfer = Transport(api, store, bid, 'fixture-platform-backups')
        transfer.begin()
        journal = store.read(bid + '/transfer-journal.json')
        journal['backup_id'] = TEMPLATE['backup_id']
        store.write(bid + '/transfer-journal.json', journal)
        self.code('TRANSFER_JOURNAL_MISMATCH', transfer.inspect_interrupted)

    def test_delete_marker_version_unavailable_detected(self):
        from scripts.backup.sealing.inventory import verify_data_versions
        api, _, _, bid, _, _ = candidate(self)
        api.seed(SOURCE['data_bucket'], 'gone', delete=True, vid='marker')
        observation = inventory(api, SOURCE, bid, 'capture-fixture')
        api.objects.clear()
        self.code('DELETE_MARKER_UNAVAILABLE', lambda: verify_data_versions(
            api, observation['document'], observation['delete_markers']))

    def test_history_cursor_must_have_both_markers(self):
        api, _, _, _, _, _ = candidate(self)
        with patch.object(api, 'list_versions', return_value={'IsTruncated': True, 'NextKeyMarker': 'k'}):
            self.code('INVALID_VERSION', lambda: inventory(api, SOURCE, TEMPLATE['backup_id'], 'capture-fixture'))

    def test_invalid_calendar_and_non_utc_receipt_dates_rejected(self):
        from scripts.backup.sealing.common import instant
        for value in ('2026-02-30T00:00:00Z', '2026-10-09T00:00:00+01:00', None, 'synthetic-sensitive-value'):
            with self.subTest(value=value):
                self.code('CAPTURE_TIME_INVALID', lambda: instant(value))

    def test_simulator_parts_enforce_type_and_size_limits(self):
        api, _, _, _, _, _ = candidate(self)
        upload = api.create_multipart('fixture-platform-backups', 'fixture', encryption='AES256')
        for number, body in ((True, b'x'), (10001, b'x'), (1, b''), (1, 'not-bytes')):
            with self.subTest(part=number):
                self.code('SIMULATOR_MEMORY_LIMIT', lambda: api.upload_part('fixture-platform-backups', 'fixture', upload, number, body))
