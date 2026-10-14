import unittest
from types import SimpleNamespace
from scripts.backup.operational.fence_admin_api import AdministrativePolicyAPI, review_permissions
from scripts.backup.operational.tests.test_fencing import SCOPE, ORIGINAL
from scripts.backup.capture.storage import CaptureFailure


class APIError(Exception):
    def __init__(self, code): self.response = {'Error': {'Code': code}}


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(region_name='us-east-1', endpoint_url='https://s3.us-east-1.amazonaws.com')
        self.calls = []; self.role = 'fence-admin'; self.response = {}
    def get_caller_identity(self):
        return {'Account': SCOPE.account, 'Arn': 'arn:aws:sts::123456789012:assumed-role/'+self.role+'/synthetic-run',
                'UserId': 'AROASYNTHETIC:synthetic-run'}
    def __getattr__(self, action):
        def call(**kwargs):
            self.calls.append((action, kwargs))
            response = self.response.get(action, {})
            if isinstance(response, Exception): raise response
            return {'ResponseMetadata': {'HTTPStatusCode': 200}, **response}
        return call


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.api = AdministrativePolicyAPI(self.client, self.client, SCOPE, 'us-east-1', SimpleNamespace(projection_hash=lambda bucket: 'a'*64))
    def test_constructor_no_remote_calls(self): self.assertFalse(self.client.calls)
    def test_non_admin_role(self):
        self.client.role = 'backup'
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_ADMIN_IDENTITY_MISMATCH$'): self.api.identity(SCOPE)
    def test_put_scoped_and_no_lockout_confirmation(self):
        self.api.put_policy(SCOPE, ORIGINAL)
        action, request = self.client.calls[-1]
        self.assertEqual(action, 'put_bucket_policy')
        self.assertEqual(request['ExpectedBucketOwner'], SCOPE.account)
        self.assertEqual(request['Bucket'], SCOPE.bucket)
        self.assertIs(request['ConfirmRemoveSelfBucketAccess'], False)
    def test_delete_policy_not_available(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_API_REJECTED$'): self.api.call('delete_bucket_policy')
        self.assertFalse(self.client.calls)
    def test_no_object_write(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_API_REJECTED$'): self.api.call('put_object')
    def test_pending_truncated_multipart_not_ignored(self):
        self.client.response['list_multipart_uploads'] = {'IsTruncated': True, 'Uploads': []}
        self.assertNotEqual(self.api.pending_uploads(SCOPE), [])
    def test_empty_uploads_must_be_untruncated(self):
        self.client.response['list_multipart_uploads'] = {'IsTruncated': False, 'Uploads': []}
        self.assertEqual(self.api.pending_uploads(SCOPE), [])
    def test_missing_truncation_status(self):
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_MULTIPART_RESPONSE_INVALID$'): self.api.pending_uploads(SCOPE)
    def test_access_denied_not_treated_as_absence(self):
        self.client.response['get_bucket_lifecycle_configuration'] = APIError('AccessDenied')
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_API_OUTCOME_UNKNOWN$'):
            self.api.call('get_bucket_lifecycle_configuration')
    def test_exact_absence_error_only(self):
        self.client.response['get_bucket_lifecycle_configuration'] = APIError('NoSuchLifecycleConfiguration')
        self.assertIsNone(self.api.call('get_bucket_lifecycle_configuration'))
    def test_permission_fragment_not_attached_and_bucket_only(self):
        policy = review_permissions(SCOPE)
        actions = policy['Statement'][0]['Action']
        self.assertIn('s3:PutBucketPolicy', actions)
        self.assertNotIn('s3:DeleteBucketPolicy', actions)
        self.assertNotIn('s3:PutObject', actions)
        self.assertEqual(policy['Statement'][0]['Resource'], 'arn:aws:s3:::synthetic-data')
    def test_untrusted_endpoint_rejected(self):
        self.client.meta.endpoint_url = 'http://evil.invalid'
        with self.assertRaisesRegex(CaptureFailure, '^FENCE_ENDPOINT_REJECTED$'):
            AdministrativePolicyAPI(self.client, self.client, SCOPE, 'us-east-1', None)
