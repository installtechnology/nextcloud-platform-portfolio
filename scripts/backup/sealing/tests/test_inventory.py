import base64
import copy
import unittest
from unittest.mock import patch

from scripts.backup.sealing.common import Bounds
from scripts.backup.sealing.inventory import inventory, list_versions, projection, checksum, verify_data_versions
from scripts.backup.sealing.simulator import MemoryS3, RemoteFailure
from scripts.backup.sealing.tests.support import Assertions, SOURCE, TEMPLATE

BID = TEMPLATE['backup_id']


class InventoryTests(Assertions, unittest.TestCase):
    def setUp(self):
        self.api = MemoryS3(page_size=1)
        self.bucket = SOURCE['data_bucket']

    def observe(self):
        return inventory(self.api, SOURCE, BID, 'capture-fixture')

    def test_empty_listing(self):
        self.assertEqual(self.observe()['document']['objects'], [])

    def test_pagination_versions_and_delete_markers(self):
        self.api.seed(self.bucket, 'visible', b'old', vid='old')
        self.api.seed(self.bucket, 'visible', b'new', vid='latest')
        self.api.seed(self.bucket, 'absent', b'old', vid='removed')
        self.api.seed(self.bucket, 'absent', delete=True, vid='marker')
        result = self.observe()
        self.assertEqual(result['document']['objects'][0]['version_id'], 'latest')
        self.assertEqual(result['document']['absent_current_keys'], ['absent'])
        self.assertEqual(result['delete_markers'], [{'key': 'absent', 'version_id': 'marker'}])
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'list']), 4)

    def test_opaque_keys_versions_and_etags_preserved(self):
        key, vid = 'urn:oid:á /%25\\x', 'opaque/+=='
        self.api.seed(self.bucket, key, b'abc', vid=vid)
        item = self.observe()['document']['objects'][0]
        self.assertEqual((item['key'], item['version_id']), (key, vid))
        self.assertTrue(item['etag'].startswith('"'))
        self.assertIsNone(item['checksum'])

    def test_order_independent_projection(self):
        self.api.seed(self.bucket, 'z', b'abc')
        self.api.seed(self.bucket, 'á', b'def')
        observation = self.observe()
        document = observation['document']
        other = copy.deepcopy(document)
        other['objects'].reverse()
        other['backup_id'] = 'ignored-for-projection'
        self.assertEqual(projection(document, []), projection(other, []))

    def test_delete_marker_version_changes_projection(self):
        self.api.seed(self.bucket, 'absent', delete=True, vid='first')
        first = self.observe()
        self.api.seed(self.bucket, 'absent', delete=True, vid='second')
        second = self.observe()
        self.assertEqual(first['document'], second['document'])
        self.assertNotEqual(first['projection_sha256'], second['projection_sha256'])

    def test_incomplete_missing_marker(self):
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': True}):
            self.code('INCOMPLETE_LISTING', self.observe)

    def test_invalid_truncated_type(self):
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': 'false'}):
            self.code('INCOMPLETE_LISTING', self.observe)

    def test_duplicate_records_rejected(self):
        item = {'Key': 'k', 'VersionId': 'v', 'IsLatest': True, 'Size': 1, 'ETag': 'e'}
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'Versions': [item, item]}):
            self.code('DUPLICATE_VERSION_RECORD', self.observe)

    def test_pagination_loop_rejected(self):
        pages = [{'IsTruncated': True, 'NextKeyMarker': 'k', 'NextVersionIdMarker': 'cursor',
                  'Versions': [{'Key': 'k', 'VersionId': v, 'IsLatest': i == 0, 'Size': 1, 'ETag': 'e'}]}
                 for i, v in enumerate(('v1', 'v2'))]
        with patch.object(self.api, 'list_versions', side_effect=pages):
            self.code('PAGINATION_LOOP', self.observe)

    def test_page_bound(self):
        self.api.seed(self.bucket, 'k', b'old')
        self.api.seed(self.bucket, 'k', b'new')
        self.code('INVENTORY_LIMIT', lambda: list_versions(self.api, self.bucket, bounds=Bounds(pages=1)))

    def test_record_bound(self):
        self.api.seed(self.bucket, 'one', b'1')
        self.api.seed(self.bucket, 'two', b'2')
        self.code('INVENTORY_LIMIT', lambda: list_versions(self.api, self.bucket, bounds=Bounds(records=1)))

    def test_missing_current_rejected(self):
        item = {'Key': 'k', 'VersionId': 'v', 'IsLatest': False, 'Size': 1, 'ETag': 'e'}
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'Versions': [item]}):
            self.code('AMBIGUOUS_CURRENT_VERSION', self.observe)

    def test_two_current_records_rejected(self):
        values = [{'Key': 'k', 'VersionId': v, 'IsLatest': True, 'Size': 1, 'ETag': 'e'} for v in ('v1', 'v2')]
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'Versions': values}):
            self.code('AMBIGUOUS_CURRENT_VERSION', self.observe)

    def test_invalid_versions(self):
        for vid in (None, '', 'null', ' ', 'a\n'):
            with self.subTest(version=repr(vid)):
                with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'Versions': [
                        {'Key': 'k', 'VersionId': vid, 'IsLatest': True, 'Size': 1, 'ETag': 'e'}]}):
                    self.code('INVALID_VERSION', self.observe)

    def test_negative_size(self):
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'Versions': [
                {'Key': 'k', 'VersionId': 'v', 'IsLatest': True, 'Size': -1, 'ETag': 'e'}]}):
            self.code('INVALID_LISTING', self.observe)

    def test_list_access_denied_not_absence(self):
        with patch.object(self.api, 'list_versions', side_effect=RemoteFailure('DENIED')):
            self.code('INVENTORY_LIST_FAILED', self.observe)

    def test_head_access_denied_not_absence(self):
        self.api.seed(self.bucket, 'k', b'1')
        with patch.object(self.api, 'head', side_effect=RemoteFailure('DENIED')):
            self.code('DATA_VERSION_UNAVAILABLE', self.observe)

    def test_head_wrong_version_rejected(self):
        self.api.seed(self.bucket, 'k', b'1')
        with patch.object(self.api, 'head', return_value={'VersionId': 'wrong', 'ContentLength': 1, 'ETag': 'e'}):
            self.code('DATA_VERSION_CHANGED', self.observe)

    def test_exact_version_revalidated(self):
        vid = self.api.seed(self.bucket, 'k', b'1')
        observation = self.observe()
        self.api.seed(self.bucket, 'k', b'new-current')
        verify_data_versions(self.api, observation['document'])
        self.assertTrue(any(c == ('head', self.bucket, 'k', vid) for c in self.api.calls))

    def test_missing_selected_version(self):
        self.api.seed(self.bucket, 'k', b'1')
        observation = self.observe()
        self.api.objects.clear()  # Synthetic simulator state, no remote deletion.
        self.code('DATA_VERSION_UNAVAILABLE', lambda: verify_data_versions(self.api, observation['document']))

    def test_full_sha256_descriptor(self):
        value = base64.b64encode(bytes(32)).decode()
        self.assertEqual(checksum({'ChecksumSHA256': value, 'ChecksumType': 'FULL_OBJECT'})['value_base64'], value)

    def test_composite_descriptor(self):
        value = base64.b64encode(bytes(32)).decode()
        self.assertEqual(checksum({'ChecksumSHA256': value + '-2', 'ChecksumType': 'COMPOSITE'})['part_count'], 2)

    def test_checksum_bad_base64_length_type_parts(self):
        for head in ({'ChecksumSHA256': 'x', 'ChecksumType': 'FULL_OBJECT'},
                     {'ChecksumSHA256': 'YQ==', 'ChecksumType': 'FULL_OBJECT'},
                     {'ChecksumSHA256': base64.b64encode(bytes(32)).decode()},
                     {'ChecksumCRC64NVME': base64.b64encode(bytes(8)).decode() + '-2', 'ChecksumType': 'COMPOSITE'},
                     {'ChecksumSHA256': base64.b64encode(bytes(32)).decode() + '-0', 'ChecksumType': 'COMPOSITE'}):
            with self.subTest(head=head):
                self.code('INVALID_CHECKSUM', lambda: checksum(head))

    def test_grouped_listing_cannot_hide_versions(self):
        for field in ({'Delimiter': '/'}, {'CommonPrefixes': [{'Prefix': 'hidden/'}]}, {'Error': 'synthetic'}):
            with self.subTest(response=field):
                with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, **field}):
                    self.code('GROUPED_OR_INVALID_LISTING', self.observe)

    def test_unicode_key_limit_is_utf8_bytes(self):
        self.api.seed(self.bucket, 'á' * 513, b'fixture')
        self.code('INVALID_LISTING', self.observe)

    def test_empty_truncated_page_rejected(self):
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': True,
                'NextKeyMarker': 'k', 'NextVersionIdMarker': 'cursor'}):
            self.code('INCOMPLETE_LISTING', self.observe)

    def test_keys_cannot_move_backwards_across_pages(self):
        item = lambda k: {'Key': k, 'VersionId': k, 'IsLatest': True, 'Size': 1, 'ETag': 'e'}
        pages = [{'IsTruncated': True, 'NextKeyMarker': 'z', 'NextVersionIdMarker': 'z', 'Versions': [item('z')]},
                 {'IsTruncated': False, 'Versions': [item('a')]}]
        with patch.object(self.api, 'list_versions', side_effect=pages):
            self.code('PAGINATION_ORDER_INVALID', self.observe)

    def test_terminal_page_cannot_keep_continuation(self):
        with patch.object(self.api, 'list_versions', return_value={'IsTruncated': False, 'NextKeyMarker': 'hidden'}):
            self.code('INCOMPLETE_LISTING', self.observe)

    def test_canonically_equivalent_unicode_keys_remain_distinct(self):
        self.api.seed(self.bucket, 'é', b'first')
        self.api.seed(self.bucket, 'e\u0301', b'second')
        observation = self.observe()
        self.assertEqual(len(observation['document']['objects']), 2)
        self.assertNotEqual(observation['document']['objects'][0]['key'], observation['document']['objects'][1]['key'])
