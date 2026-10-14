"""Version-bound, conditional simulated transport. Ambiguous writes require explicit verification."""
import hashlib
import re

from ..safe_files import SafeRoot, signature, relative_parts
from ..schemas import SchemaCatalog
from ..semantics import validate_semantics
from .common import Bounds, Budget, SealFailure, backup_id, require, version, json_bytes, sha
from .inventory import list_versions
from .simulator import RemoteFailure

NAMES = {'database.age', 'configuration.age', 'local_files.age', 'recovery_parameters.age',
         'object_inventory.age', 'manifest.json.age', 'completion.json'}


class Transport:
    names = NAMES
    hash_field = 'ciphertext_sha256'
    require_age = True

    def __init__(self, api, store, bid, bucket, bounds=Bounds(), cancelled=lambda: False):
        require(getattr(api, 'simulation', False) is True and store.synthetic is True, 'SIMULATION_ONLY')
        backup_id(bid)
        self.api, self.store, self.bid, self.bucket = api, store, bid, bucket
        self.bounds, self.cancelled = bounds, cancelled
        self.prefix = f'backups/{bid}/'
        self.journal_path = bid + '/transfer-journal.json'
        self.state = None

    def begin(self):
        # A pre-existing journal requires a separate explicit verify path, never automatic resume.
        require(not (self.store.root / self.journal_path).exists(), 'EXPLICIT_VERIFICATION_REQUIRED')
        require(not list_versions(self.api, self.bucket, self.prefix, self.bounds), 'HISTORICAL_PREFIX_COLLISION')
        state = {'internal_version': 1, 'kind': 'simulated-transfer-journal', 'backup_id': self.bid,
                      'bucket': self.bucket, 'status': 'incomplete', 'objects': {}, 'simulation': True}
        # Exclusive local reservation also rejects another producer racing the first journal.
        data = json_bytes(state)
        self.store.artifact(self.store.operation(self.bid), 'transfer-journal.json',
                            lambda out: out.write(data), 1024 * 1024)
        self.state = state

    def save(self):
        self.store.write(self.journal_path, self.state)

    def retry_part(self, operation, budget):
        for attempt in range(self.bounds.attempts):
            budget.check()
            try:
                return operation()
            except RemoteFailure as error:
                if error.code != 'TRANSIENT' or attempt + 1 == self.bounds.attempts:
                    raise SealFailure('MULTIPART_PART_FAILED') from None
            except Exception:
                raise SealFailure('MULTIPART_PART_FAILED') from None
        raise SealFailure('MULTIPART_PART_FAILED')

    def exact_history(self, name, vid):
        records = list_versions(self.api, self.bucket, self.prefix + name, self.bounds)
        exact = [r for r in records if r['Key'] == self.prefix + name]
        require(len(exact) == 1 and exact[0]['VersionId'] == vid and exact[0]['IsLatest']
                and not exact[0]['delete_marker'], 'REMOTE_HISTORY_CONFLICT')

    def upload(self, name, relative, expected):
        require(name != 'completion.json', 'COMPLETION_REQUIRES_VALIDATED_SET')
        return self._upload(name, relative, expected)

    def publish_completion(self, bundle, relative, expected):
        """Separate gate; verification in a simulator is never operational authorization."""
        require(self.state is not None and self.state['status'] == 'incomplete'
                and not self.state.get('blocked'), 'TRANSFER_NOT_OPEN')
        records = self.state['objects']
        require(set(records) == NAMES - {'completion.json'}
                and all(r.get('phase') == 'verified' for r in records.values()), 'COMPLETION_REQUIRES_VALIDATED_SET')
        require(isinstance(bundle, dict) and set(bundle) == {'manifest', 'inventory', 'completion'},
                'COMPLETION_REQUIRES_VALIDATED_SET')
        for kind, document in bundle.items():
            SchemaCatalog().validate(kind, document)
        validate_semantics(bundle)
        marker = bundle['completion']
        manifest_record = records['manifest.json.age']
        require(marker['backup_id'] == self.bid and marker['manifest_key'] == self.prefix + 'manifest.json.age'
                and marker['manifest_version_id'] == manifest_record['version_id']
                and marker['manifest_ciphertext_sha256'] == manifest_record[self.hash_field], 'COMPLETION_SET_MISMATCH')
        for role, descriptor in bundle['manifest']['artifacts'].items():
            record = records.get(role + '.age', {})
            require(descriptor['bucket'] == self.bucket and descriptor['key'] == self.prefix + role + '.age'
                    and all(descriptor[k] == record.get(k) for k in ('version_id', 'size_bytes', 'ciphertext_sha256')),
                    'COMPLETION_SET_MISMATCH')
        marker_bytes = json_bytes(marker)
        require(expected == {'size_bytes': len(marker_bytes), 'ciphertext_sha256': sha(marker_bytes)},
                'COMPLETION_SET_MISMATCH')
        for name, record in records.items():
            self.verify(name, record)
        return self._upload('completion.json', relative, expected)

    def _upload(self, name, relative, expected):
        require(self.state is not None and self.state['status'] == 'incomplete'
                and not self.state.get('blocked'), 'TRANSFER_NOT_OPEN')
        require(name in self.names and name not in self.state['objects'], 'DUPLICATE_TRANSFER')
        require(relative == self.bid + '/' + name, 'TRANSFER_PATH_MISMATCH')
        relative_parts(relative)
        require(isinstance(expected, dict) and type(expected.get('size_bytes')) is int
                and 0 < expected['size_bytes'] <= self.bounds.artifact_bytes,
                'CIPHERTEXT_LIMIT')
        require(isinstance(expected.get(self.hash_field), str)
                and re.fullmatch(r'[a-f0-9]{64}', expected[self.hash_field]), 'INVALID_CIPHERTEXT_HASH')
        require(not list_versions(self.api, self.bucket, self.prefix + name, self.bounds), 'HISTORICAL_KEY_COLLISION')
        budget = Budget(self.bounds, self.cancelled)
        record = {'size_bytes': expected['size_bytes'], self.hash_field: expected[self.hash_field],
                  'phase': 'intent', 'upload_id': None, 'version_id': None}
        self.state['objects'][name] = record
        self.save()
        upload = None
        try:
            with SafeRoot(self.store.root) as safe, safe.open(relative) as stream:
                before = signature(stream)
                require(before[2] == record['size_bytes'], 'CIPHERTEXT_CHANGED')
                if self.require_age and name != 'completion.json':
                    # Producer age adapter validates framing. Defense in depth prevents
                    # an accidentally supplied plaintext file from entering the protocol.
                    require(stream.read(22) == b'age-encryption.org/v1\n', 'INVALID_AGE_ENVELOPE')
                    stream.seek(0)
                digest = hashlib.sha256()
                if before[2] < self.bounds.multipart_threshold:
                    # Bounded single PUT; large content always uses fixed-size parts.
                    require(before[2] <= 64 * 1024 ** 2, 'SINGLE_PUT_LIMIT')
                    body = stream.read(before[2] + 1)
                    digest.update(body)
                    require(len(body) == before[2] and digest.hexdigest() == record[self.hash_field]
                            and before == signature(stream), 'CIPHERTEXT_CHANGED')
                    budget.check()
                    record['phase'] = 'put_intent'
                    self.save()
                    response = self.api.put(self.bucket, self.prefix + name, body,
                                            if_none_match='*', encryption='AES256')
                else:
                    budget.check()
                    upload = self.api.create_multipart(self.bucket, self.prefix + name, encryption='AES256')
                    require(isinstance(upload, str) and 0 < len(upload) <= 1024, 'INVALID_UPLOAD_ID')
                    record.update(upload_id=upload, phase='multipart_open')
                    self.save()
                    parts, sizes = [], []
                    while body := stream.read(self.bounds.part_bytes):
                        budget.check()
                        require(len(parts) < 10000, 'MULTIPART_LIMIT')
                        digest.update(body)
                        number = len(parts) + 1
                        etag = self.retry_part(lambda: self.api.upload_part(
                            self.bucket, self.prefix + name, upload, number, body), budget)
                        require(isinstance(etag, str) and etag, 'INVALID_PART_RECEIPT')
                        parts.append((number, etag))
                        sizes.append((number, len(body), etag))
                    require(digest.hexdigest() == record[self.hash_field]
                            and before == signature(stream), 'CIPHERTEXT_CHANGED')
                    budget.check()
                    require(self.api.list_parts(self.bucket, self.prefix + name, upload) == sizes,
                            'MULTIPART_PART_MISMATCH')
                    record['phase'] = 'completion_intent'
                    self.save()
                    response = self.api.complete_multipart(self.bucket, self.prefix + name, upload, parts, if_none_match='*')
                budget.check()
                require(isinstance(response, dict) and 'Error' not in response, 'REMOTE_RESPONSE_INVALID')
                vid = version(response.get('VersionId'))
                record.update(version_id=vid, phase='uploaded')
                self.save()
                safe.assert_unchanged()
            self.verify(name, record, budget)
            record['phase'] = 'verified'
            self.save()
            return dict(record)
        except BaseException as error:
            # Lost PUT/Complete responses are ambiguous. No overwrite, delete or blind retry.
            record['phase'] = 'verification_required'
            self.state['blocked'] = True
            try:
                self.save()
            except Exception:
                pass
            if upload and record.get('version_id') is None:
                try:
                    self.api.abort(self.bucket, self.prefix + name, upload)
                except Exception:
                    pass  # Known UploadId remains in journal for explicit diagnosis.
            if isinstance(error, SealFailure):
                raise
            raise SealFailure('REMOTE_WRITE_AMBIGUOUS') from None

    def verify(self, name, record, budget=None):
        budget = budget or Budget(self.bounds, self.cancelled)
        vid = version(record['version_id'])
        try:
            budget.check()
            self.exact_history(name, vid)
            head = self.api.head(self.bucket, self.prefix + name, vid)
            require(isinstance(head, dict) and head.get('VersionId') == vid
                    and type(head.get('ContentLength')) is int and head['ContentLength'] == record['size_bytes'],
                    'REMOTE_SIZE_OR_VERSION_MISMATCH')
            digest, size = hashlib.sha256(), 0
            with self.api.get(self.bucket, self.prefix + name, vid) as stream:
                while chunk := stream.read(self.bounds.chunk_bytes):
                    budget.check()
                    size += len(chunk)
                    require(size <= record['size_bytes'], 'REMOTE_SIZE_OR_VERSION_MISMATCH')
                    digest.update(chunk)
            require(size == record['size_bytes'] and digest.hexdigest() == record[self.hash_field],
                    'REMOTE_HASH_MISMATCH')
            self.exact_history(name, vid)
        except SealFailure:
            raise
        except Exception:
            raise SealFailure('REMOTE_VERIFICATION_FAILED') from None

    def inspect_interrupted(self):
        """Read-only explicit verification; never resumes or publishes a marker."""
        state = self.store.read(self.journal_path)
        require(type(state.get('internal_version')) is int and state['internal_version'] == 1
                and state.get('status') in ('incomplete', 'simulated_integrity_verified')
                and state.get('kind') == 'simulated-transfer-journal' and state.get('backup_id') == self.bid
                and state.get('bucket') == self.bucket and state.get('simulation') is True
                and isinstance(state.get('objects'), dict) and 0 < len(state['objects']) <= len(NAMES),
                'TRANSFER_JOURNAL_MISMATCH')
        for name, record in state['objects'].items():
            require(name in self.names and isinstance(record, dict), 'TRANSFER_JOURNAL_MISMATCH')
            require(type(record.get('size_bytes')) is int and 0 < record['size_bytes'] <= self.bounds.artifact_bytes
                    and isinstance(record.get('ciphertext_sha256'), str)
                    and re.fullmatch(r'[a-f0-9]{64}', record[self.hash_field]), 'TRANSFER_JOURNAL_MISMATCH')
            require(record.get('version_id') is not None, 'REMOTE_WRITE_AMBIGUOUS')
            self.verify(name, record)
        return {'code': 'EXPLICIT_VERSIONS_VERIFIED_NO_RESUME', 'simulation': True, 'backup_complete': False}
