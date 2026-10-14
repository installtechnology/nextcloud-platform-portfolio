import hashlib
import io
import unittest
from unittest.mock import patch

from scripts.backup.sealing.common import Bounds
from scripts.backup.sealing.transport import Transport
from scripts.backup.sealing.simulator import RemoteFailure
from scripts.backup.sealing.tests.support import Assertions, candidate


class TransportTests(Assertions, unittest.TestCase):
    def setUp(self):
        self.api, self.store, _, self.bid, _, _ = candidate(self)
        self.bucket = 'fixture-platform-backups'
        self.transport = Transport(self.api, self.store, self.bid, self.bucket)

    def prepare(self, data=b'age-encryption.org/v1\nfixture', bounds=None):
        if bounds:
            self.transport = Transport(self.api, self.store, self.bid, self.bucket, bounds)
            data = b'age-encryption.org/v1\n' + data
        self.store.artifact(self.store.operation(self.bid), 'database.age', lambda out: out.write(data), len(data))
        self.transport.begin()
        expected = {'size_bytes': len(data), 'ciphertext_sha256': hashlib.sha256(data).hexdigest()}
        return lambda: self.transport.upload('database.age', self.bid + '/database.age', expected)

    def test_put_download_hash_version(self):
        record = self.prepare()()
        self.assertEqual(record['phase'], 'verified')
        self.assertTrue(any(c[0] == 'get' and c[-1] == record['version_id'] for c in self.api.calls))

    def test_historical_delete_marker_collision(self):
        key = self.transport.prefix + 'database.age'
        self.api.seed(self.bucket, key, b'old')
        self.api.seed(self.bucket, key, delete=True)
        self.code('HISTORICAL_PREFIX_COLLISION', self.transport.begin)

    def test_historical_version_without_current_body_collision(self):
        self.api.seed(self.bucket, self.transport.prefix + 'database.age', delete=True)
        self.code('HISTORICAL_PREFIX_COLLISION', self.transport.begin)

    def test_duplicate_upload_rejected(self):
        upload = self.prepare()
        upload()
        self.code('DUPLICATE_TRANSFER', upload)

    def test_repeated_begin_requires_explicit_verification(self):
        self.transport.begin()
        self.code('EXPLICIT_VERIFICATION_REQUIRED', self.transport.begin)

    def test_traversal_relative_rejected(self):
        upload = self.prepare()
        self.code('TRANSFER_PATH_MISMATCH', lambda: self.transport.upload('database.age', '../secret', {}))
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_unknown_name_rejected(self):
        self.transport.begin()
        self.code('DUPLICATE_TRANSFER', lambda: self.transport.upload('unknown', '', {}))

    def test_ciphertext_hash_changed_before_upload(self):
        upload = self.prepare()
        (self.store.operation(self.bid) / 'database.age').write_bytes(b'changed')
        self.code('CIPHERTEXT_CHANGED', upload)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_download_corruption(self):
        upload = self.prepare()
        with patch.object(self.api, 'get', return_value=io.BytesIO(b'wrong')):
            self.code('REMOTE_HASH_MISMATCH', upload)

    def test_download_excess_length(self):
        upload = self.prepare()
        with patch.object(self.api, 'get', return_value=io.BytesIO(b'wrong' * 100)):
            self.code('REMOTE_SIZE_OR_VERSION_MISMATCH', upload)

    def test_wrong_head_version(self):
        upload = self.prepare()
        with patch.object(self.api, 'head', return_value={'VersionId': 'wrong', 'ContentLength': 1}):
            self.code('REMOTE_SIZE_OR_VERSION_MISMATCH', upload)

    def test_ambiguous_put_never_retried(self):
        upload = self.prepare()
        original = self.api.put
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RemoteFailure('TIMEOUT')
        with patch.object(self.api, 'put', side_effect=lost):
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'put']), 1)
        self.code('REMOTE_WRITE_AMBIGUOUS', self.transport.inspect_interrupted)

    def test_explicit_verified_versions_do_not_resume(self):
        self.prepare()()
        before = len([c for c in self.api.calls if c[0] == 'put'])
        result = self.transport.inspect_interrupted()
        self.assertFalse(result['backup_complete'])
        self.assertEqual(before, len([c for c in self.api.calls if c[0] == 'put']))

    def test_multipart_success(self):
        data = b'x' * (6 * 1024 ** 2)
        bounds = Bounds(multipart_threshold=1, part_bytes=5 * 1024 ** 2)
        self.prepare(data, bounds)()
        self.assertFalse(self.api.uploads)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'part']), 2)

    def test_multipart_transient_part_bounded_retry(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1, part_bytes=5 * 1024 ** 2))
        original, count = self.api.upload_part, [0]
        def transient(*args):
            count[0] += 1
            if count[0] == 1:
                raise RemoteFailure('TRANSIENT')
            return original(*args)
        with patch.object(self.api, 'upload_part', side_effect=transient):
            upload()
        self.assertEqual(count[0], 3)

    def test_multipart_failure_aborts_known_upload_only(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        with patch.object(self.api, 'upload_part', side_effect=RemoteFailure('TRANSIENT')) as method:
            self.code('MULTIPART_PART_FAILED', upload)
        self.assertEqual(method.call_count, 3)
        self.assertFalse(self.api.uploads)
        self.assertTrue(any(c[0] == 'abort' for c in self.api.calls))
        self.assertFalse(any(c[0] == 'complete_multipart' for c in self.api.calls))

    def test_multipart_parts_mismatch(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        with patch.object(self.api, 'list_parts', return_value=[]):
            self.code('MULTIPART_PART_MISMATCH', upload)

    def test_complete_409_not_retried(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        with patch.object(self.api, 'complete_multipart', side_effect=RemoteFailure('CONFLICT')) as method:
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)
        self.assertEqual(method.call_count, 1)

    def test_cancellation_before_write(self):
        upload = self.prepare()
        self.transport.cancelled = lambda: True
        self.code('TRANSFER_CANCELLED', upload)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_overwrite_race_rejected_by_conditional(self):
        upload = self.prepare()
        original = self.api.put
        def race(bucket, key, body, **kwargs):
            self.api.seed(bucket, key, b'competing')
            return original(bucket, key, body, **kwargs)
        with patch.object(self.api, 'put', side_effect=race):
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)

    def test_deleted_current_race_leaves_conflict_not_success(self):
        upload = self.prepare()
        original = self.api.put
        def race(bucket, key, body, **kwargs):
            self.api.seed(bucket, key, b'old')
            self.api.seed(bucket, key, delete=True)
            return original(bucket, key, body, **kwargs)
        with patch.object(self.api, 'put', side_effect=race):
            self.code('REMOTE_HISTORY_CONFLICT', upload)

    def test_non_simulated_api_refused(self):
        self.api.simulation = False
        self.code('SIMULATION_ONLY', lambda: Transport(self.api, self.store, self.bid, self.bucket))

    def test_abort_failure_retains_upload_for_explicit_diagnosis(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        with patch.object(self.api, 'upload_part', side_effect=RemoteFailure('DENIED')):
            with patch.object(self.api, 'abort', side_effect=RemoteFailure('DENIED')):
                self.code('MULTIPART_PART_FAILED', upload)
        self.assertEqual(len(self.api.uploads), 1)
        journal = self.store.read(self.bid + '/transfer-journal.json')
        self.assertIn(journal['objects']['database.age']['upload_id'], self.api.uploads)
        self.assertEqual(journal['status'], 'incomplete')

    def test_create_response_lost_not_blindly_retried(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        original = self.api.create_multipart
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RemoteFailure('TIMEOUT')
        with patch.object(self.api, 'create_multipart', side_effect=lost) as method:
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)
        self.assertEqual(method.call_count, 1)
        self.assertEqual(len(self.api.uploads), 1)
        self.assertFalse(any(c[0] == 'complete_multipart' for c in self.api.calls))

    def test_complete_response_lost_preserves_uploaded_version(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        original = self.api.complete_multipart
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RemoteFailure('TIMEOUT')
        with patch.object(self.api, 'complete_multipart', side_effect=lost) as method:
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)
        self.assertEqual(method.call_count, 1)
        self.assertTrue(self.api.objects.get((self.bucket, self.transport.prefix + 'database.age')))
        self.code('REMOTE_WRITE_AMBIGUOUS', self.transport.inspect_interrupted)

    def test_mid_multipart_cancel_aborts_owned_upload(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1, part_bytes=5 * 1024 ** 2))
        self.transport.cancelled = lambda: any(c[0] == 'part' for c in self.api.calls)
        self.code('TRANSFER_CANCELLED', upload)
        self.assertFalse(self.api.uploads)

    def test_read_denied_error_is_sanitized(self):
        upload = self.prepare()
        with patch.object(self.api, 'get', side_effect=RuntimeError('fictitious-password-not-to-print')):
            self.code('REMOTE_VERIFICATION_FAILED', upload)

    def test_failed_begin_cannot_open_transport(self):
        self.api.seed(self.bucket, self.transport.prefix + 'collision', b'existing')
        self.code('HISTORICAL_PREFIX_COLLISION', self.transport.begin)
        self.assertIsNone(self.transport.state)
        self.code('TRANSFER_NOT_OPEN', lambda: self.transport.upload('database.age', self.bid + '/database.age', {}))

    def test_invalid_expected_hash_rejected(self):
        self.transport.begin()
        self.code('INVALID_CIPHERTEXT_HASH', lambda: self.transport.upload('database.age', self.bid + '/database.age',
                  {'size_bytes': 1, 'ciphertext_sha256': 'not-a-sha256'}))

    def test_boolean_head_size_rejected(self):
        data = b'age-encryption.org/v1\nX'
        upload = self.prepare(data)
        original = self.api.head
        def invalid(*args):
            value = original(*args)
            value['ContentLength'] = True
            return value
        with patch.object(self.api, 'head', side_effect=invalid):
            self.code('REMOTE_SIZE_OR_VERSION_MISMATCH', upload)

    def test_public_upload_cannot_publish_premature_marker(self):
        self.transport.begin()
        self.code('COMPLETION_REQUIRES_VALIDATED_SET', lambda: self.transport.upload(
            'completion.json', self.bid + '/completion.json', {}))
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_marker_gate_rejects_missing_artifacts(self):
        self.transport.begin()
        self.code('COMPLETION_REQUIRES_VALIDATED_SET', lambda: self.transport.publish_completion({}, '', {}))

    def test_ambiguous_session_cannot_continue_with_another_object(self):
        upload = self.prepare()
        with patch.object(self.api, 'put', side_effect=RemoteFailure('TIMEOUT')):
            self.code('REMOTE_WRITE_AMBIGUOUS', upload)
        self.assertTrue(self.store.read(self.bid + '/transfer-journal.json')['blocked'])
        self.code('TRANSFER_NOT_OPEN', lambda: self.transport.upload('configuration.age', '', {}))

    def test_embedded_multipart_error_never_treated_as_success(self):
        upload = self.prepare(b'x' * (6 * 1024 ** 2), Bounds(multipart_threshold=1))
        with patch.object(self.api, 'complete_multipart', return_value={'Error': 'synthetic', 'VersionId': 'pretended'}):
            self.code('REMOTE_RESPONSE_INVALID', upload)
        self.assertFalse(self.api.uploads)
        self.assertTrue(self.transport.state['blocked'])

    def test_malformed_write_response_is_safely_rejected(self):
        upload = self.prepare()
        with patch.object(self.api, 'put', return_value=None):
            self.code('REMOTE_RESPONSE_INVALID', upload)

    def test_unknown_interrupted_journal_version_rejected(self):
        self.prepare()()
        journal = self.store.read(self.bid + '/transfer-journal.json')
        journal['internal_version'] = 2
        self.store.write(self.bid + '/transfer-journal.json', journal)
        self.code('TRANSFER_JOURNAL_MISMATCH', self.transport.inspect_interrupted)

    def test_plaintext_is_not_uploaded_even_with_matching_hash(self):
        upload = self.prepare(b'synthetic sensitive plaintext')
        self.code('INVALID_AGE_ENVELOPE', upload)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))
