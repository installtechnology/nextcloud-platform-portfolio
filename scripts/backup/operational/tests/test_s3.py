import io
from types import SimpleNamespace
import unittest
from scripts.backup.operational.s3 import DataInventory, BackupS3, decoded
from scripts.backup.sealing.common import SealFailure

BID = '20260101T000000Z-01234567-89ab-4cde-8fab-0123456789ab'


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url='https://s3.us-east-1.amazonaws.com', region_name='us-east-1')
        self.calls = []
        self.response = {'ResponseMetadata': {'HTTPStatusCode': 200}, 'VersionId': 'opaque/+==', 'ServerSideEncryption': 'AES256'}
    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            if isinstance(self.response, Exception): raise self.response
            return dict(self.response)
        return call


class S3Tests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.data = DataInventory(self.client, 'synthetic-data', '123456789012', 'us-east-1')
        self.backup = BackupS3(self.client, 'synthetic-backups', '123456789012', 'us-east-1', BID)
        self.key = self.backup.prefix + 'database.dump'
    def code(self, expected, fn):
        with self.assertRaises(SealFailure) as error: fn()
        self.assertEqual(error.exception.code, expected)
    def test_constructor_no_requests(self): self.assertFalse(self.client.calls)
    def test_not_a_simulation(self): self.assertIs(self.data.simulation, False)
    def test_scope_rejected_before_request(self):
        self.code('S3_BUCKET_REJECTED', lambda: self.data.head('other', 'x', 'v'))
        self.assertFalse(self.client.calls)
    def test_endpoint_rejected(self):
        for endpoint in ('http://s3.us-east-1.amazonaws.com', 'https://evil.example', 'https://u:p@s3.us-east-1.amazonaws.com', 'https://s3.us-east-1.amazonaws.com/path'):
            with self.subTest(endpoint=endpoint):
                self.client.meta.endpoint_url=endpoint
                self.code('S3_ENDPOINT_REJECTED', lambda: DataInventory(self.client,'synthetic-data','123456789012','us-east-1'))
    def test_version_bound_and_owner(self):
        self.data.head('synthetic-data','opaque/%2F','opaque/+==')
        args=self.client.calls[-1][1]
        self.assertEqual(args['Key'],'opaque/%2F');self.assertEqual(args['VersionId'],'opaque/+==')
        self.assertEqual(args['ExpectedBucketOwner'],'123456789012')
    def test_wrong_version(self):
        self.code('S3_VERSION_MISMATCH',lambda:self.data.head('synthetic-data','x','wrong'))
    def test_encoding_once(self):
        self.assertEqual(decoded('%C3%A1%20%252F'), 'á %2F')
        self.code('S3_ENCODING_INVALID',lambda:decoded('%xy'))
        self.code('S3_ENCODING_INVALID',lambda:decoded('%FF'))
    def test_list_preserves_version_and_delete_marker(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'EncodingType':'url','IsTruncated':True,
            'Versions':[{'Key':'x%252F','VersionId':'opaque%2F','IsLatest':False}],
            'DeleteMarkers':[{'Key':'x%252F','VersionId':'delete/+','IsLatest':True}],
            'NextKeyMarker':'x%252F','NextVersionIdMarker':'opaque%2F'}
        result=self.data.list_versions('synthetic-data','',('x%2F','opaque%2F'))
        self.assertEqual(result['Versions'][0]['Key'],'x%2F')
        self.assertEqual(result['Versions'][0]['VersionId'],'opaque%2F')
        self.assertTrue(result['DeleteMarkers'][0]['IsLatest'])
        self.assertEqual(self.client.calls[-1][1]['VersionIdMarker'],'opaque%2F')
    def test_missing_encoding_rejected(self):
        self.code('S3_ENCODING_INVALID',lambda:self.data.list_versions('synthetic-data'))
    def test_backup_prefix_rejected(self):
        self.code('S3_BACKUP_PREFIX_REJECTED',lambda:self.backup.list_versions('synthetic-backups',''))
    def test_empty_body_rejected(self):
        self.code('S3_BODY_INVALID',lambda:self.backup.put('synthetic-backups',self.key,b'',if_none_match='*',encryption='AES256'))
        self.assertFalse(self.client.calls)
    def test_conditional_sse_s3_put(self):
        self.backup.put('synthetic-backups',self.key,b'synthetic backup bytes',if_none_match='*',encryption='AES256')
        args=self.client.calls[-1][1];self.assertEqual(args['IfNoneMatch'],'*');self.assertEqual(args['ServerSideEncryption'],'AES256')
    def test_completion_cannot_be_written(self):
        self.code('S3_BACKUP_KEY_REJECTED',lambda:self.backup.put('synthetic-backups',self.backup.prefix+'completion.json',b'{}',if_none_match='*',encryption='AES256'))
    def test_errors_sanitized(self):
        self.client.response=RuntimeError('synthetic-secret')
        self.code('S3_REQUEST_FAILED',lambda:self.data.head('synthetic-data','x','v'))
    def test_http_error_rejected(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':403}}
        self.code('S3_RESPONSE_INVALID',lambda:self.data.head('synthetic-data','x','v'))
    def test_get_wrong_version_closes_stream(self):
        body=io.BytesIO(b'synthetic');self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'Body':body,'VersionId':'wrong'}
        self.code('S3_VERSION_MISMATCH',lambda:self.backup.get('synthetic-backups',self.key,'v'))
        self.assertTrue(body.closed)
    def test_multipart_parameters(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'UploadId':'u'}
        self.assertEqual(self.backup.create_multipart('synthetic-backups',self.key,encryption='AES256'),'u')
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'ETag':'opaque-etag'}
        self.assertEqual(self.backup.upload_part('synthetic-backups',self.key,'u',1,b'synthetic backup bytes'),'opaque-etag')
    def test_no_deletion_api(self):
        self.assertFalse(hasattr(self.backup,'delete'));self.assertFalse(hasattr(self.backup,'delete_version'))
        self.code('S3_ACTION_REJECTED',lambda:self.backup.call('delete_object',Bucket='synthetic-backups',Key=self.key))
    def test_part_listing(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':False,
            'Parts':[{'PartNumber':1,'Size':10,'ETag':'opaque'}]}
        self.assertEqual(self.backup.list_parts('synthetic-backups',self.key,'u'),[(1,10,'opaque')])
    def test_part_listing_loop_rejected(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':True,
            'Parts':[{'PartNumber':1,'Size':10,'ETag':'opaque'}],'NextPartNumberMarker':1}
        self.code('S3_PART_LIST_INVALID',lambda:self.backup.list_parts('synthetic-backups',self.key,'u'))
    def test_final_page_last_part_marker_from_real_s3(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':False,
            'PartNumberMarker':0,'NextPartNumberMarker':3,
            'Parts':[{'PartNumber':n,'Size':10,'ETag':'opaque'} for n in (1,2,3)]}
        self.assertEqual(self.backup.list_parts('synthetic-backups',self.key,'u'),
                         [(n,10,'opaque') for n in (1,2,3)])
    def test_final_page_inconsistent_marker_rejected(self):
        for marker in (2,True,'1',-1):
            with self.subTest(marker=marker):
                self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':False,
                    'NextPartNumberMarker':marker,'Parts':[{'PartNumber':1,'Size':10,'ETag':'opaque'}]}
                self.code('S3_PART_LIST_INVALID',lambda:self.backup.list_parts('synthetic-backups',self.key,'u'))
    def test_empty_final_page_zero_marker(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':False,
            'NextPartNumberMarker':0,'Parts':[]}
        self.assertEqual(self.backup.list_parts('synthetic-backups',self.key,'u'),[])
    def test_truncated_boolean_marker_rejected(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'IsTruncated':True,
            'NextPartNumberMarker':True,'Parts':[{'PartNumber':1,'Size':10,'ETag':'opaque'}]}
        self.code('S3_PART_LIST_INVALID',lambda:self.backup.list_parts('synthetic-backups',self.key,'u'))
    def test_conditional_multipart_completion(self):
        self.backup.complete_multipart('synthetic-backups',self.key,'u',[(1,'opaque')],if_none_match='*')
        self.assertEqual(self.client.calls[-1][1]['IfNoneMatch'],'*')
    def test_multipart_embedded_error_rejected(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':200},'Error':{'Message':'synthetic-sensitive'}}
        self.code('S3_RESPONSE_INVALID',lambda:self.backup.complete_multipart('synthetic-backups',self.key,'u',[(1,'opaque')],if_none_match='*'))
    def test_abort_only_known_upload(self):
        self.client.response={'ResponseMetadata':{'HTTPStatusCode':204}}
        self.backup.abort('synthetic-backups',self.key,'u')
        self.assertEqual(self.client.calls[-1][0],'abort_multipart_upload')


if __name__ == '__main__': unittest.main()
