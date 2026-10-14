"""In-memory S3 protocol simulator. No SDK, network, credentials or external calls."""
import copy
import hashlib
import io
import uuid

from .common import require


class RemoteFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__('SIMULATED_REMOTE_FAILURE')


class MemoryS3:
    simulation = True

    def __init__(self, page_size=1000):
        require(type(page_size) is int and 1 <= page_size <= 1000, 'INVALID_SIMULATOR_LIMIT')
        self.objects, self.uploads, self.calls = {}, {}, []
        self.page_size = page_size

    def seed(self, bucket, key, body=b'', delete=False, vid=None, checksum=None):
        require(isinstance(body, bytes) and len(body) <= 64 * 1024 ** 2, 'SIMULATOR_MEMORY_LIMIT')
        require(sum(len(v['body']) for values in self.objects.values() for v in values)
                + sum(len(b) for u in self.uploads.values() for b, _ in u['parts'].values())
                + len(body) <= 128 * 1024 ** 2, 'SIMULATOR_MEMORY_LIMIT')
        vid = vid or str(uuid.uuid4())
        record = {'VersionId': vid, 'body': body, 'delete': delete,
                  'ETag': '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"', 'checksum': checksum or {}}
        self.objects.setdefault((bucket, key), []).insert(0, record)
        return vid

    def list_versions(self, bucket, prefix, cursor):
        self.calls.append(('list', bucket, prefix, cursor))
        records = []
        for (b, key), values in sorted(self.objects.items()):
            if b != bucket or not key.startswith(prefix):
                continue
            for i, value in enumerate(values):
                records.append({'Key': key, 'VersionId': value['VersionId'], 'IsLatest': i == 0,
                                'Size': len(value['body']), 'ETag': value['ETag'], 'delete': value['delete']})
        start = 0
        if cursor is not None:
            positions = [i for i, r in enumerate(records) if (r['Key'], r['VersionId']) == cursor]
            if len(positions) != 1:
                raise RemoteFailure('INVALID_CURSOR')
            start = positions[0] + 1
        selected = records[start:start + self.page_size]
        result = {'Versions': [], 'DeleteMarkers': [], 'IsTruncated': start + len(selected) < len(records)}
        for record in selected:
            value = copy.deepcopy(record)
            delete = value.pop('delete')
            if delete:
                value.pop('Size')
                value.pop('ETag')
            result['DeleteMarkers' if delete else 'Versions'].append(value)
        if result['IsTruncated']:
            result.update(NextKeyMarker=selected[-1]['Key'], NextVersionIdMarker=selected[-1]['VersionId'])
        return result

    def _version(self, bucket, key, vid):
        for record in self.objects.get((bucket, key), []):
            if record['VersionId'] == vid and not record['delete']:
                return record
        raise RemoteFailure('NOT_FOUND')

    def head(self, bucket, key, vid):
        self.calls.append(('head', bucket, key, vid))
        record = self._version(bucket, key, vid)
        return {'VersionId': vid, 'ContentLength': len(record['body']), 'ETag': record['ETag'], **record['checksum']}

    def get(self, bucket, key, vid):
        self.calls.append(('get', bucket, key, vid))
        return io.BytesIO(self._version(bucket, key, vid)['body'])

    def _condition(self, bucket, key):
        current = self.objects.get((bucket, key), [])
        if current and not current[0]['delete']:
            raise RemoteFailure('PRECONDITION')

    def put(self, bucket, key, body, *, if_none_match, encryption):
        self.calls.append(('put', bucket, key))
        assert if_none_match == '*' and encryption == 'AES256'
        self._condition(bucket, key)
        return {'VersionId': self.seed(bucket, key, body)}

    def create_multipart(self, bucket, key, *, encryption):
        self.calls.append(('create_multipart', bucket, key))
        assert encryption == 'AES256'
        upload = str(uuid.uuid4())
        self.uploads[upload] = {'bucket': bucket, 'key': key, 'parts': {}}
        return upload

    def upload_part(self, bucket, key, upload, number, body):
        value = self.uploads[upload]
        assert (value['bucket'], value['key']) == (bucket, key)
        require(type(number) is int and 1 <= number <= 10000
                and isinstance(body, bytes) and 0 < len(body) <= 64 * 1024 ** 2, 'SIMULATOR_MEMORY_LIMIT')
        previous = len(value['parts'].get(number, (b'', None))[0])
        require(sum(len(v['body']) for values in self.objects.values() for v in values)
                + sum(len(b) for u in self.uploads.values() for b, _ in u['parts'].values())
                - previous + len(body) <= 128 * 1024 ** 2, 'SIMULATOR_MEMORY_LIMIT')
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()
        value['parts'][number] = (body, etag)
        self.calls.append(('part', bucket, key, number))
        return etag

    def list_parts(self, bucket, key, upload):
        value = self.uploads[upload]
        assert (value['bucket'], value['key']) == (bucket, key)
        return [(n, len(b), e) for n, (b, e) in sorted(value['parts'].items())]

    def complete_multipart(self, bucket, key, upload, parts, *, if_none_match):
        self.calls.append(('complete_multipart', bucket, key))
        assert if_none_match == '*'
        self._condition(bucket, key)
        value = self.uploads[upload]
        assert (value['bucket'], value['key']) == (bucket, key)
        assert parts == [(n, e) for n, (_, e) in sorted(value['parts'].items())]
        data = b''.join(b for _, (b, _) in sorted(value['parts'].items()))
        version = self.seed(bucket, key, data)
        del self.uploads[upload]
        return {'VersionId': version}

    def abort(self, bucket, key, upload):
        self.calls.append(('abort', bucket, key))
        value = self.uploads.get(upload)
        if value is not None:
            assert (value['bucket'], value['key']) == (bucket, key)
            del self.uploads[upload]
