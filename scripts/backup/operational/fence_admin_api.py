"""Review-only administrative SDK bridge: narrow policy/read methods.

No SDK import, credential discovery, client factory, operational CLI or launcher.
Never uses the application or backup role; no object mutations or delete policy.
Owner/TLS/scope checks accompany every request. No CAS is claimed.
"""
import re
from urllib.parse import urlsplit
from .fence_policy import FenceScope, canonical, require
from ..capture.storage import CaptureFailure


class AdministrativePolicyAPI:
    def __init__(self, s3, sts, scope, region, inventory):
        require(type(scope) is FenceScope, 'FENCE_SCOPE_REQUIRED')
        require(isinstance(region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region), 'FENCE_REGION_INVALID')
        try:
            url = urlsplit(s3.meta.endpoint_url)
            require(url.scheme == 'https' and url.hostname == f's3.{region}.amazonaws.com'
                    and url.port in (None, 443) and not url.username and not url.password
                    and not url.query and not url.fragment and url.path in ('', '/')
                    and s3.meta.region_name == region, 'FENCE_ENDPOINT_REJECTED')
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure('FENCE_ENDPOINT_REJECTED') from None
        self.s3, self.sts, self.scope, self.region, self.inventory = s3, sts, scope, region, inventory

    def check(self, scope):
        require(scope == self.scope, 'FENCE_SCOPE_MISMATCH')

    def call(self, action, **kwargs):
        require(action in ('get_bucket_policy', 'put_bucket_policy', 'get_bucket_versioning',
                          'get_bucket_lifecycle_configuration', 'get_bucket_replication',
                          'get_object_lock_configuration', 'list_multipart_uploads'), 'FENCE_API_REJECTED')
        try:
            response = getattr(self.s3, action)(Bucket=self.scope.bucket,
                ExpectedBucketOwner=self.scope.account, **kwargs)
        except Exception as error:
            failure = getattr(error, 'response', {})
            code = failure.get('Error', {}).get('Code') if isinstance(failure, dict) else None
            # Absence must be distinguished from AccessDenied/timeout.
            absent = {'get_bucket_lifecycle_configuration': 'NoSuchLifecycleConfiguration',
                      'get_bucket_policy': 'NoSuchBucketPolicy',
                      'get_bucket_replication': 'ReplicationConfigurationNotFoundError',
                      'get_object_lock_configuration': 'ObjectLockConfigurationNotFoundError'}
            if action in absent and code == absent[action]:
                return None
            raise CaptureFailure('FENCE_API_OUTCOME_UNKNOWN') from None
        require(isinstance(response, dict) and 'Error' not in response
                and response.get('ResponseMetadata', {}).get('HTTPStatusCode') in (200, 204), 'FENCE_API_RESPONSE_INVALID')
        return response

    def identity(self, scope):
        self.check(scope)
        try:
            found = self.sts.get_caller_identity()
            role_name = scope.administrative_role.rsplit('/', 1)[1]
            require(found['Account'] == scope.account and re.fullmatch(
                'arn:aws:sts::' + scope.account + ':assumed-role/' + re.escape(role_name)
                + '/[A-Za-z0-9+=,.@_-]{2,64}', found['Arn']), 'FENCE_ADMIN_IDENTITY_MISMATCH')
            require(isinstance(found.get('UserId'), str)
                    and re.fullmatch(r'[A-Z0-9]{8,128}:[A-Za-z0-9+=,.@_-]{2,64}', found['UserId']),
                    'FENCE_ADMIN_IDENTITY_MISMATCH')
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure('FENCE_ADMIN_IDENTITY_MISMATCH') from None
        return {'account': scope.account, 'role_arn': scope.administrative_role,
                'principal_arn': found['Arn'], 'principal_id': found.get('UserId')}

    def read_policy(self, scope):
        self.check(scope)
        value = self.call('get_bucket_policy')
        if value is None:
            raise CaptureFailure('DATA_POLICY_ABSENT')
        require(isinstance(value.get('Policy'), str), 'FENCE_POLICY_REQUIRED')
        return value['Policy']

    def put_policy(self, scope, policy):
        self.check(scope)
        self.identity(scope)
        return self.call('put_bucket_policy', Policy=canonical(policy), ConfirmRemoveSelfBucketAccess=False)

    def read_configuration(self, scope):
        self.check(scope)
        version = self.call('get_bucket_versioning')
        lifecycle = self.call('get_bucket_lifecycle_configuration')
        replication = self.call('get_bucket_replication')
        object_lock = self.call('get_object_lock_configuration')
        return {'versioning': version.get('Status'), 'lifecycle': lifecycle.get('Rules') if lifecycle else [],
                'replication': replication.get('ReplicationConfiguration') if replication else None,
                'object_lock': object_lock.get('ObjectLockConfiguration') if object_lock else None}

    def pending_uploads(self, scope):
        self.check(scope)
        # Any pending upload rejects the window. Do not abort or silently omit
        # a truncated response; fully empty response must be untruncated.
        value = self.call('list_multipart_uploads', MaxUploads=1000)
        require(type(value.get('IsTruncated')) is bool and isinstance(value.get('Uploads', []), list), 'FENCE_MULTIPART_RESPONSE_INVALID')
        if value['IsTruncated'] or value.get('Uploads'):
            return ['PENDING_OR_TRUNCATED']
        return []

    def inventory_hash(self, scope):
        self.check(scope)
        # Injected inventory adapter must reuse PR5 validation/projection; no ETag
        # shortcut. No default implementation or user-supplied hash fallback.
        return self.inventory.projection_hash(scope.bucket)


def review_permissions(scope):
    """Separate coordinator role only. Not attached or merged into Terraform."""
    require(type(scope) is FenceScope, 'FENCE_SCOPE_REQUIRED')
    return {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'AdministrativeDataFenceOnly', 'Effect': 'Allow',
         'Action': ['s3:GetBucketPolicy', 's3:PutBucketPolicy', 's3:GetBucketLocation',
                    's3:GetBucketVersioning', 's3:GetLifecycleConfiguration',
                    's3:GetReplicationConfiguration', 's3:GetBucketObjectLockConfiguration',
                    's3:ListBucketMultipartUploads', 's3:ListBucketVersions'],
         'Resource': 'arn:aws:s3:::' + scope.bucket},
    ]}
