"""Scoped SDK adapter, injected client only. No credential discovery or CLI.

Used by the host-only operational producer. Local tests use fake clients.
The legacy simulated Transport intentionally continues to reject this adapter.
"""
import re
from urllib.parse import unquote, urlsplit

from ..sealing.common import SealFailure, backup_id, require, version


def decoded(value):
    require(isinstance(value, str) and not re.search(r'%(?![0-9a-fA-F]{2})', value),
            'S3_ENCODING_INVALID')
    try:
        return unquote(value, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise SealFailure('S3_ENCODING_INVALID') from None


class DataInventory:
    simulation = False
    actions = frozenset({'list_object_versions', 'head_object'})

    def __init__(self, client, bucket, account, region):
        require(isinstance(bucket, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket),
                'S3_SCOPE_INVALID')
        require(isinstance(account, str) and re.fullmatch(r'[0-9]{12}', account)
                and isinstance(region, str) and re.fullmatch(r'[a-z]{2}(-[a-z]+)+-[0-9]+', region),
                'S3_SCOPE_INVALID')
        try:
            endpoint = urlsplit(client.meta.endpoint_url)
            require(client.meta.region_name == region and endpoint.scheme == 'https'
                    and endpoint.hostname == f's3.{region}.amazonaws.com'
                    and endpoint.port in (None, 443) and not endpoint.username
                    and not endpoint.password and endpoint.path in ('', '/')
                    and not endpoint.query and not endpoint.fragment, 'S3_ENDPOINT_REJECTED')
        except SealFailure:
            raise
        except Exception:
            raise SealFailure('S3_ENDPOINT_REJECTED') from None
        self.client, self.bucket, self.account = client, bucket, account

    def scope(self, bucket, key=None):
        require(bucket == self.bucket, 'S3_BUCKET_REJECTED')
        if key is not None:
            require(isinstance(key, str) and 0 < len(key.encode('utf-8')) <= 1024,
                    'S3_KEY_REJECTED')

    def call(self, action, **kwargs):
        require(action in self.actions, 'S3_ACTION_REJECTED')
        try:
            response = getattr(self.client, action)(ExpectedBucketOwner=self.account, **kwargs)
        except Exception:
            # Do not echo SDK errors, request parameters, keys, tokens or bodies.
            raise SealFailure('S3_REQUEST_FAILED') from None
        require(isinstance(response, dict) and 'Error' not in response
                and response.get('ResponseMetadata', {}).get('HTTPStatusCode') in (200, 204),
                'S3_RESPONSE_INVALID')
        return response

    def list_versions(self, bucket, prefix='', cursor=None):
        self.scope(bucket)
        require(isinstance(prefix, str) and len(prefix.encode('utf-8')) <= 1024,
                'S3_KEY_REJECTED')
        args = dict(Bucket=bucket, Prefix=prefix, MaxKeys=1000, EncodingType='url')
        if cursor is not None:
            require(isinstance(cursor, tuple) and len(cursor) == 2
                    and isinstance(cursor[0], str) and cursor[0].startswith(prefix), 'S3_CURSOR_INVALID')
            args.update(KeyMarker=cursor[0], VersionIdMarker=version(cursor[1]))
        response = self.call('list_object_versions', **args)
        require(response.get('EncodingType') == 'url', 'S3_ENCODING_INVALID')
        # Decode keys/markers exactly once. VersionIds are opaque and never decoded.
        for name in ('Prefix', 'KeyMarker', 'NextKeyMarker'):
            if name in response:
                response[name] = decoded(response[name])
        for field in ('Versions', 'DeleteMarkers'):
            require(isinstance(response.get(field, []), list), 'S3_RESPONSE_INVALID')
            for record in response.get(field, []):
                require(isinstance(record, dict) and 'Key' in record, 'S3_RESPONSE_INVALID')
                record['Key'] = decoded(record['Key'])
        # Existing PR5 performs pagination, duplicates, IsLatest and semantic checks.
        return response

    def head(self, bucket, key, vid):
        self.scope(bucket, key)
        vid = version(vid)
        response = self.call('head_object', Bucket=bucket, Key=key, VersionId=vid,
                             ChecksumMode='ENABLED')
        require(response.get('VersionId') == vid and not response.get('DeleteMarker'),
                'S3_VERSION_MISMATCH')
        return response


class BackupS3(DataInventory):
    """SSE-S3 artifact primitives only; producer must gate completion separately.

    Completion.json is delegated to the separate, gated completion producer.
    No DeleteObject/DeleteObjectVersion or IAM/EC2 methods are exposed.
    """
    actions = DataInventory.actions | frozenset({'get_object', 'put_object',
        'create_multipart_upload', 'upload_part', 'list_parts',
        'complete_multipart_upload', 'abort_multipart_upload'})
    def __init__(self, client, bucket, account, region, bid):
        super().__init__(client, bucket, account, region)
        backup_id(bid)
        self.prefix = f'backups/{bid}/'

    def scope(self, bucket, key=None):
        super().scope(bucket, key)
        if key is not None:
            require(key.startswith(self.prefix) and key[len(self.prefix):] in {
                'database.dump', 'configuration.tar', 'local_files.tar',
                'recovery_parameters.json', 'object_inventory.json', 'manifest.json'},
                'S3_BACKUP_KEY_REJECTED')

    def list_versions(self, bucket, prefix='', cursor=None):
        require(isinstance(prefix, str) and prefix.startswith(self.prefix), 'S3_BACKUP_PREFIX_REJECTED')
        return super().list_versions(bucket, prefix, cursor)

    def head(self, bucket, key, vid):
        response = super().head(bucket, key, vid)
        require(response.get('ServerSideEncryption') == 'AES256', 'S3_ENCRYPTION_MISMATCH')
        return response

    def get(self, bucket, key, vid):
        self.scope(bucket, key)
        response = self.call('get_object', Bucket=bucket, Key=key, VersionId=version(vid))
        body = response.get('Body')
        if response.get('VersionId') != vid or response.get('DeleteMarker') or body is None:
            if body is not None:
                body.close()
            raise SealFailure('S3_VERSION_MISMATCH')
        if response.get('ServerSideEncryption') != 'AES256':
            body.close()
            raise SealFailure('S3_ENCRYPTION_MISMATCH')
        return body

    def put(self, bucket, key, body, *, if_none_match, encryption):
        self.scope(bucket, key)
        require(isinstance(body, bytes) and 0 < len(body) <= 64 * 1024**2, 'S3_BODY_INVALID')
        require(if_none_match == '*' and encryption == 'AES256', 'S3_WRITE_CONTROLS_REQUIRED')
        response = self.call('put_object', Bucket=bucket, Key=key, Body=body,
                             IfNoneMatch='*', ServerSideEncryption='AES256')
        version(response.get('VersionId'))
        return response

    def create_multipart(self, bucket, key, *, encryption):
        self.scope(bucket, key)
        require(encryption == 'AES256', 'S3_WRITE_CONTROLS_REQUIRED')
        response = self.call('create_multipart_upload', Bucket=bucket, Key=key,
                             ServerSideEncryption='AES256')
        upload = response.get('UploadId')
        require(isinstance(upload, str) and 0 < len(upload) <= 1024, 'INVALID_UPLOAD_ID')
        return upload

    def upload_part(self, bucket, key, upload, number, body):
        self.scope(bucket, key)
        require(isinstance(upload, str) and 0 < len(upload) <= 1024
                and type(number) is int and 1 <= number <= 10000
                and isinstance(body, bytes) and 0 < len(body) <= 64 * 1024**2, 'S3_PART_INVALID')
        # Full-artifact SHA-256 verification remains a producer requirement.
        response = self.call('upload_part', Bucket=bucket, Key=key, UploadId=upload,
                             PartNumber=number, Body=body)
        require(isinstance(response.get('ETag'), str) and response['ETag'], 'INVALID_PART_RECEIPT')
        return response['ETag']

    def list_parts(self, bucket, key, upload):
        self.scope(bucket, key)
        require(isinstance(upload, str) and 0 < len(upload) <= 1024, 'INVALID_UPLOAD_ID')
        parts, marker = [], 0
        for _ in range(20):
            response = self.call('list_parts', Bucket=bucket, Key=key, UploadId=upload,
                                 MaxParts=1000, PartNumberMarker=marker)
            require(type(response.get('IsTruncated')) is bool
                    and isinstance(response.get('Parts', []), list), 'S3_PART_LIST_INVALID')
            values = response.get('Parts', [])
            require(len(values) <= 1000, 'S3_PART_LIST_INVALID')
            for item in values:
                require(isinstance(item, dict) and type(item.get('PartNumber')) is int
                        and marker < item['PartNumber'] <= 10000
                        and type(item.get('Size')) is int and item['Size'] > 0
                        and isinstance(item.get('ETag'), str) and item['ETag'], 'S3_PART_LIST_INVALID')
                marker = item['PartNumber']
                parts.append((marker, item['Size'], item['ETag']))
            if not response['IsTruncated']:
                # S3 can return the last part number even on the final page.
                # IsTruncated controls pagination; a supplied terminal marker
                # must still match that last part (or the empty zero marker).
                next_marker = response.get('NextPartNumberMarker')
                require(next_marker is None or type(next_marker) is int
                        and next_marker in (0, marker), 'S3_PART_LIST_INVALID')
                return parts
            require(values and type(response.get('NextPartNumberMarker')) is int
                    and response['NextPartNumberMarker'] == marker,
                    'S3_PART_LIST_INVALID')
        raise SealFailure('S3_PART_LIST_LIMIT')

    def complete_multipart(self, bucket, key, upload, parts, *, if_none_match):
        self.scope(bucket, key)
        require(isinstance(upload, str) and 0 < len(upload) <= 1024 and if_none_match == '*'
                and isinstance(parts, list) and 1 <= len(parts) <= 10000,
                'S3_COMPLETION_INVALID')
        for number, item in enumerate(parts, 1):
            require(isinstance(item, tuple) and len(item) == 2 and type(item[0]) is int
                    and item[0] == number and isinstance(item[1], str) and item[1],
                    'S3_COMPLETION_INVALID')
        response = self.call('complete_multipart_upload', Bucket=bucket, Key=key,
                             UploadId=upload, IfNoneMatch='*',
                             MultipartUpload={'Parts':[{'PartNumber':n, 'ETag':etag} for n,etag in parts]})
        version(response.get('VersionId'))
        return response

    def abort(self, bucket, key, upload):
        self.scope(bucket, key)
        require(isinstance(upload, str) and 0 < len(upload) <= 1024, 'INVALID_UPLOAD_ID')
        # Known uncommitted upload only; never deletes objects or object versions.
        self.call('abort_multipart_upload', Bucket=bucket, Key=key, UploadId=upload)
