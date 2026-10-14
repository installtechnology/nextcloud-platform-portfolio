"""Synthetic SSE-S3 v2 regressions. No SDK requests or private keys."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup.schemas import SchemaCatalog
from scripts.backup.semantics import validate_semantics
from scripts.backup.errors import ValidationFailure
from scripts.backup.integrity import verify_artifacts
from scripts.backup.safe_files import SafeRoot
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import Bounds, SealFailure
from scripts.backup.operational import completion, host_worker, runtime_config
from scripts.backup.operational.transport import ArtifactTransport
from scripts.backup.operational.s3 import BackupS3
from scripts.backup.operational.tests.test_s3 import Client, BID

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / 'contracts/recovery/fixtures/sse-s3-v2'


def documents():
    return {kind: json.loads((FIXTURES / name).read_text(encoding='utf-8'))
            for kind, name in (('manifest', 'complete-unproven.json'),
                               ('inventory', 'object-inventory.json'),
                               ('completion', 'completion.json'),
                               ('evidence', 'restore-evidence.json'))}


class ContractV2(unittest.TestCase):
    def setUp(self):
        self.docs = documents()
        self.catalog = SchemaCatalog()

    def test_four_documents_and_semantics(self):
        for kind, doc in self.docs.items():
            self.catalog.validate(kind, doc)
        validate_semantics(self.docs)
        self.assertFalse(self.docs['manifest']['dependencies']['private_decryption_key_required'])
        self.assertFalse(self.docs['manifest']['verification']['restore_proven'])

    def test_age_recipient_rejected(self):
        artifact = self.docs['manifest']['artifacts']['database']
        artifact['encryption']['recipient_id'] = 'unexpected'
        with self.assertRaisesRegex(ValidationFailure, '^SCHEMA_INVALID$'):
            self.catalog.validate('manifest', self.docs['manifest'])

    def test_wrong_encryption_rejected(self):
        for method in ('age-v1', 'SSE-KMS', 'none'):
            doc = copy.deepcopy(self.docs['manifest'])
            doc['artifacts']['database']['encryption']['method'] = method
            with self.subTest(method=method), self.assertRaisesRegex(ValidationFailure, '^SCHEMA_INVALID$'):
                self.catalog.validate('manifest', doc)

    def test_mixed_contract_versions_rejected(self):
        self.docs['completion']['schema_version'] = 1
        with self.assertRaisesRegex(ValidationFailure, '^MIXED_CONTRACT_VERSION$'):
            validate_semantics(self.docs)

    def test_unknown_version_rejected(self):
        self.docs['manifest']['schema_version'] = 3
        with self.assertRaisesRegex(ValidationFailure, '^SCHEMA_INVALID$'):
            self.catalog.validate('manifest', self.docs['manifest'])

    def test_wrong_artifact_name_rejected(self):
        self.docs['manifest']['artifacts']['database']['key'] = 'backups/' + self.docs['manifest']['backup_id'] + '/wrong.tar'
        with self.assertRaisesRegex(ValidationFailure, '^WRONG_ARTIFACT_NAME$'):
            validate_semantics(self.docs)

    def test_private_key_requirement_rejected(self):
        self.docs['manifest']['dependencies']['private_decryption_key_required'] = True
        with self.assertRaisesRegex(ValidationFailure, '^SCHEMA_INVALID$'):
            self.catalog.validate('manifest', self.docs['manifest'])

    def test_v1_remains_readable(self):
        old = ROOT / 'contracts/recovery/fixtures'
        for kind, name in (('manifest', 'complete-unproven.json'), ('completion', 'completion.json'), ('evidence', 'restore-evidence.json')):
            self.catalog.validate(kind, json.loads((old / name).read_text()))

    def test_local_bytes_not_s3_ciphertext(self):
        bundle = self.docs
        bundle.pop('evidence')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'artifacts').mkdir()
            raw = b'synthetic artifact bytes'
            for artifact in bundle['manifest']['artifacts'].values():
                name = artifact['key'].rsplit('/', 1)[1]
                (root / 'artifacts' / name).write_bytes(raw)
                artifact.update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            manifest = json.dumps(bundle['manifest']).encode()
            (root / 'artifacts/manifest.json').write_bytes(manifest)
            bundle['completion']['manifest_sha256'] = hashlib.sha256(manifest).hexdigest()
            with SafeRoot(root) as safe:
                self.assertEqual(len(verify_artifacts(safe, bundle, Limits())), 6)
            (root / 'artifacts/database.dump').write_bytes(b'corrupt')
            with SafeRoot(root) as safe, self.assertRaisesRegex(ValidationFailure, '^ARTIFACT_SIZE_MISMATCH$'):
                verify_artifacts(safe, bundle, Limits())


class SseAdapter(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.adapter = BackupS3(self.client, 'synthetic-backups', '123456789012', 'us-east-1', BID)
        self.key = self.adapter.prefix + 'database.dump'

    def test_no_age_framing_and_explicit_sse(self):
        self.adapter.put(self.adapter.bucket, self.key, b'synthetic dump', if_none_match='*', encryption='AES256')
        self.assertEqual(self.client.calls[-1][1]['ServerSideEncryption'], 'AES256')

    def test_encryption_cannot_be_disabled(self):
        for encryption in ('none', 'aws:kms', None):
            with self.subTest(encryption=encryption), self.assertRaisesRegex(SealFailure, '^S3_WRITE_CONTROLS_REQUIRED$'):
                self.adapter.put(self.adapter.bucket, self.key, b'x', if_none_match='*', encryption=encryption)
        self.assertFalse(self.client.calls)

    def test_exact_version_head_requires_aes256(self):
        self.client.response['ContentLength'] = 3
        self.adapter.head(self.adapter.bucket, self.key, 'opaque/+==')
        for encryption in (None, 'aws:kms'):
            self.client.response['ServerSideEncryption'] = encryption
            with self.assertRaisesRegex(SealFailure, '^S3_ENCRYPTION_MISMATCH$'):
                self.adapter.head(self.adapter.bucket, self.key, 'opaque/+==')

    def test_get_without_aes256_closes_body(self):
        body = io.BytesIO(b'synthetic')
        self.client.response.update(Body=body, ServerSideEncryption='aws:kms')
        with self.assertRaisesRegex(SealFailure, '^S3_ENCRYPTION_MISMATCH$'):
            self.adapter.get(self.adapter.bucket, self.key, 'opaque/+==')
        self.assertTrue(body.closed)

    def test_legacy_age_key_rejected(self):
        with self.assertRaisesRegex(SealFailure, '^S3_BACKUP_KEY_REJECTED$'):
            self.adapter.put(self.adapter.bucket, self.adapter.prefix + 'database.age', b'x', if_none_match='*', encryption='AES256')

    def test_empty_and_invalid_bodies_rejected(self):
        for body in (b'', 'not bytes', None):
            with self.subTest(body=body), self.assertRaisesRegex(SealFailure, '^S3_BODY_INVALID$'):
                self.adapter.put(self.adapter.bucket, self.key, body, if_none_match='*', encryption='AES256')


class IntegrationBoundaries(unittest.TestCase):
    def test_no_operational_age_import_or_invocation(self):
        for module in (completion, host_worker, runtime_config):
            text = Path(module.__file__).read_text()
            self.assertNotIn('from ..sealing.crypto', text)
            self.assertNotIn('age.encrypt(', text)
            self.assertNotIn("config['recipient']", text)
        self.assertFalse((ROOT / 'scripts/backup/operational/encryption.py').exists())

    def test_no_age_host_installation(self):
        text = (ROOT / 'ansible/roles/recovery_operational/tasks/main.yml').read_text()
        self.assertNotIn('recovery_verified_age_binary', text)
        self.assertNotIn('/nextcloud-recovery/age', text)

    def test_no_simulated_seal_can_create_live_completion(self):
        from types import SimpleNamespace
        with self.assertRaisesRegex(SealFailure, '^LIVE_SEAL_ADAPTERS_REQUIRED$'):
            completion.OperationalSeal(SimpleNamespace(synthetic=True), None, None, None, {}, {}, {})

    def test_transport_uses_sha256_and_fixed_names(self):
        self.assertEqual(ArtifactTransport.hash_field, 'sha256')
        self.assertFalse(ArtifactTransport.require_age)
        self.assertNotIn('completion.json', ArtifactTransport.names)
        self.assertTrue(all(not name.endswith('.age') for name in ArtifactTransport.names))


class TransferAlgorithm(unittest.TestCase):
    """Bypass construction ONLY in unit tests; simulator cannot instantiate it."""
    def setUp(self):
        from scripts.backup.capture.storage import Store
        from scripts.backup.sealing.simulator import MemoryS3
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name, synthetic=True)
        self.store.operation(BID).mkdir()
        self.api = MemoryS3()
        self.transport = object.__new__(ArtifactTransport)
        self.transport.api = self.api
        self.transport.store = self.store
        self.transport.bid = BID
        self.transport.bucket = 'synthetic-backups'
        self.transport.bounds = Bounds()
        self.transport.cancelled = lambda: False
        self.transport.prefix = 'backups/' + BID + '/'
        self.transport.journal_path = BID + '/operational-transfer-journal.json'
        self.transport.state = None
        self.transport.begin()

    def upload(self, raw=b'synthetic dump bytes'):
        self.store.artifact(self.store.operation(BID), 'database.dump', lambda output: output.write(raw), 32 * 1024**2)
        return self.transport.upload('database.dump', BID + '/database.dump',
                                     {'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})

    def test_single_put_exact_bytes_and_version(self):
        record = self.upload()
        self.assertEqual(record['phase'], 'verified')
        self.assertEqual(record['sha256'], hashlib.sha256(b'synthetic dump bytes').hexdigest())
        self.assertEqual(self.api.get(self.transport.bucket, self.transport.prefix + 'database.dump', record['version_id']).read(), b'synthetic dump bytes')
        self.assertFalse(self.transport.state['backup_complete'])

    def test_multipart_exact_bytes(self):
        self.transport.bounds = Bounds(multipart_threshold=1024, part_bytes=5*1024**2)
        self.assertEqual(self.upload(b'x' * (5*1024**2 + 17))['phase'], 'verified')
        self.assertFalse(self.api.uploads)
        self.assertTrue(any(call[0] == 'complete_multipart' for call in self.api.calls))

    def test_corrupted_local_hash_blocks_write(self):
        with self.assertRaisesRegex(SealFailure, '^CIPHERTEXT_CHANGED$'):
            self.store.artifact(self.store.operation(BID), 'database.dump', lambda output: output.write(b'x'), 100)
            self.transport.upload('database.dump', BID + '/database.dump', {'size_bytes': 1, 'sha256': '0'*64})
        self.assertTrue(self.transport.state['blocked'])
        self.assertFalse(any(call[0] == 'put' for call in self.api.calls))

    def test_remote_bytes_corruption_blocks_verification(self):
        record = self.upload()
        self.api.objects[(self.transport.bucket, self.transport.prefix + 'database.dump')][0]['body'] = b'bad'
        with self.assertRaisesRegex(SealFailure, '^REMOTE_SIZE_OR_VERSION_MISMATCH$'):
            self.transport.verify('database.dump', record)

    def test_lost_put_ack_no_retry_or_completion(self):
        original = self.api.put
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('synthetic-sensitive')
        self.api.put = lost
        with self.assertRaisesRegex(SealFailure, '^REMOTE_WRITE_AMBIGUOUS$'):
            self.upload()
        self.assertEqual(sum(call[0] == 'put' for call in self.api.calls), 1)
        self.assertTrue(self.transport.state['blocked'])
        self.assertFalse(self.transport.state['backup_complete'])

    def test_historical_delete_marker_collision(self):
        self.api.seed(self.transport.bucket, self.transport.prefix + 'database.dump', delete=True)
        with self.assertRaisesRegex(SealFailure, '^HISTORICAL_KEY_COLLISION$'):
            self.upload()

    def test_completion_cannot_bypass_live_writer(self):
        with self.assertRaisesRegex(SealFailure, '^LIVE_CAPTURE_RECEIPT_REQUIRED$'):
            self.transport.publish_completion({'backup_complete': True})


class ProducerAlgorithm(unittest.TestCase):
    """Unit-test collaborators are patched; no live receipt or AWS proof."""
    def setUp(self):
        from scripts.backup.capture.storage import Store
        from scripts.backup.sealing.simulator import MemoryS3
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Only this test subclass creates synthetic Windows staging. Production
        # still requires exact Store + root POSIX, with synthetic=False.
        class TestStore(Store):
            def __init__(self, path):
                super().__init__(path, synthetic=True)
                self.synthetic = False
        self.store = TestStore(self.temp.name)
        self.template = documents()
        m = self.template['manifest']
        self.bid = m['backup_id']
        self.store.operation(self.bid).mkdir()
        self.journal = {'original': {'database': m['compatibility']['database_name'],
                                    'owner': m['compatibility']['database_owner'],
                                    'table_prefix': m['compatibility']['table_prefix'], 'maintenance': True},
                        'consistency_id': m['consistency']['id'],
                        'quiesced_at': m['consistency']['quiesced_at'],
                        'capture_finished_at': m['consistency']['capture_finished_at'], 'artifacts': {}}
        for role, spec in completion.FILES.items():
            self.journal['artifacts'][role] = self.store.artifact(self.store.operation(self.bid), spec[0],
                                                              lambda out: out.write(b'synthetic'), 100)
        self.snapshot = {'document': self.template['inventory'], 'delete_markers': []}
        self.data = MemoryS3()
        self.data.bucket = m['source']['data_bucket']
        self.backups = MemoryS3()
        self.backups.bucket = 'fixture-platform-backups'
        self.backups.prefix = 'backups/' + self.bid + '/'
        trace = self
        class TestWriter:
            def publish(self, docs, transport):
                for kind, doc in docs.items():
                    SchemaCatalog().validate(kind, doc)
                validate_semantics(docs)
                trace.produced = docs
                trace.published = True
                return 'synthetic-completion'
        self.writer = TestWriter()
        self.published = False
        # Mock constructor only. Algorithms run on real confined temporary files.
        def transport(api, store, bid, bucket, bounds):
            obj = object.__new__(ArtifactTransport)
            obj.api, obj.store, obj.bid, obj.bucket = api, store, bid, bucket
            obj.bounds, obj.cancelled = bounds, lambda: False
            obj.prefix, obj.journal_path = api.prefix, bid + '/operational-transfer-journal.json'
            obj.state = None
            return obj
        for name, value in (('Store', TestStore), ('DataInventory', MemoryS3), ('BackupS3', MemoryS3),
                            ('CompletionWriter', TestWriter), ('ArtifactTransport', transport)):
            context = patch.object(completion, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(completion, 'live_receipt', side_effect=lambda *args:
                               (self.journal, self.snapshot, m['consistency']['inventory_before_sha256']))
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(completion, 'verify_data_versions')
        self.verify_versions = context.start()
        self.addCleanup(context.stop)
        self.seal = completion.OperationalSeal(self.store, self.data, self.backups, self.writer,
                                               m['source'], m['compatibility'], m['scope'])

    def test_producer_v2_without_age(self):
        result = self.seal.seal(self.bid)
        self.assertEqual(self.produced['manifest']['schema_version'], 2)
        self.assertTrue(self.published)
        self.assertFalse(result['restore_proven'])
        self.assertFalse(result['destroy_authorized'])
        self.assertEqual(self.verify_versions.call_count, 2)
        self.assertTrue(all(a['encryption'] == {'method': 'SSE-S3', 'algorithm': 'AES256'}
                            for a in self.produced['manifest']['artifacts'].values()))
        self.assertEqual(len([call for call in self.backups.calls if call[0] == 'put']), 6)
        self.assertFalse(any(p.suffix == '.age' for p in self.store.operation(self.bid).iterdir()))

    def test_mutated_capture_cannot_publish(self):
        (self.store.operation(self.bid) / 'database.dump').write_bytes(b'changed')
        with self.assertRaisesRegex(SealFailure, '^CAPTURE_ARTIFACT_CHANGED$'):
            self.seal.seal(self.bid)
        self.assertFalse(self.published)
        self.assertFalse(any(call[0] == 'put' for call in self.backups.calls))

    def test_missing_capture_cannot_publish(self):
        self.journal['artifacts'].pop('database')
        with self.assertRaisesRegex(SealFailure, '^CAPTURE_ARTIFACTS_INCOMPLETE$'):
            self.seal.seal(self.bid)
        self.assertFalse(self.published)


if __name__ == '__main__':
    unittest.main()
