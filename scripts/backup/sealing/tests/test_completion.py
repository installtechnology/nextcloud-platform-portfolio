import copy
import json
import unittest
from unittest.mock import patch

from scripts.backup.sealing.common import SealFailure
from scripts.backup.sealing.completion import SimulatedSeal
from scripts.backup.sealing.tests.support import Assertions, SOURCE, TEMPLATE, age_tools, candidate, decrypt
from scripts.backup.sealing.simulator import RemoteFailure


class CompletionTests(Assertions, unittest.TestCase):
    def setUp(self):
        self.age, self.recipient, self.identity = age_tools(self)
        self.api, self.store, self.backend, self.bid, _, _ = candidate(self)
        self.seal = SimulatedSeal(self.api, self.store, self.age, SOURCE, TEMPLATE['compatibility'], TEMPLATE['scope'],
                                  self.recipient, 'synthetic-recipient', 'fixture-platform-backups')

    def run_seal(self):
        return self.seal.seal(self.bid)

    def rewrite(self, change):
        journal = self.journal(self.store, self.bid)
        change(journal)
        self.store.write(self.bid + '/journal.json', journal)

    def no_marker(self):
        self.assertFalse(any(c[0] == 'put' and c[2].endswith('/completion.json') for c in self.api.calls))

    def test_complete_simulation_all_ciphertexts_and_marker_last(self):
        result = self.run_seal()
        puts = [c for c in self.api.calls if c[0] in ('put', 'complete_multipart')]
        self.assertEqual(len(puts), 7)
        self.assertTrue(puts[-1][2].endswith('/completion.json'))
        self.assertFalse(result['backup_complete'])
        self.assertFalse(result['operationally_homologated'])
        self.assertEqual(result['functional_restore'], 'not_verified')
        self.assertEqual(self.journal(self.store, self.bid)['status'], 'local_candidate_only')
        manifest = decrypt(self.age, self.identity, (self.store.operation(self.bid) / 'manifest.json.age').read_bytes())
        self.assertEqual(json.loads(manifest), result['manifest'])
        inventory = decrypt(self.age, self.identity, (self.store.operation(self.bid) / 'object_inventory.age').read_bytes())
        self.assertEqual(json.loads(inventory), result['inventory'])
        for role, artifact in result['manifest']['artifacts'].items():
            self.assertEqual(artifact['encryption']['method'], 'age-v1')
            self.assertTrue(artifact['version_id'])
        marker_text = json.dumps(result['completion'])
        self.assertNotIn('installation_identity', marker_text)
        self.assertNotIn('password', marker_text)

    def test_no_hooks_candidate_rejected_before_write(self):
        self.rewrite(lambda j: j.pop('inventory_observation'))
        self.code('PAIRED_INVENTORY_REQUIRED', self.run_seal)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_incomplete_candidate_rejected(self):
        self.rewrite(lambda j: j.update(status='incomplete'))
        self.code('CAPTURE_NOT_READY', self.run_seal)
        self.no_marker()

    def test_unrestored_state_rejected(self):
        self.rewrite(lambda j: j.update(service_state_restored=False))
        self.code('CAPTURE_NOT_READY', self.run_seal)

    def test_unproven_external_writers_rejected(self):
        self.rewrite(lambda j: j['inventory_observation']['controls_after'].update(external_writers_fenced=False))
        self.code('WRITER_CONTROLS_UNPROVEN', self.run_seal)

    def test_mixed_capture_id_rejected(self):
        self.rewrite(lambda j: j['inventory_observation']['after']['document'].update(consistency_id='capture-other'))
        self.code('MIXED_INVENTORY', self.run_seal)

    def test_source_identity_mismatch_rejected(self):
        self.rewrite(lambda j: j['original'].update(identity='0' * 64))
        self.code('SOURCE_IDENTITY_MISMATCH', self.run_seal)

    def test_missing_artifact_rejected(self):
        self.rewrite(lambda j: j['artifacts'].pop('configuration'))
        self.code('CAPTURE_ARTIFACTS_INCOMPLETE', self.run_seal)

    def test_dump_catalog_event_missing_rejected(self):
        self.rewrite(lambda j: j.update(events=[e for e in j['events'] if e['phase'] != 'dump_catalog_checked']))
        self.code('CAPTURE_SEQUENCE_INVALID', self.run_seal)

    def test_changed_plaintext_candidate_rejected(self):
        (self.store.operation(self.bid) / 'database.dump').write_bytes(b'changed capture')
        self.code('CAPTURE_ARTIFACT_CHANGED', self.run_seal)
        self.no_marker()

    def test_encryption_failure_prevents_marker(self):
        with patch.object(self.age, 'encrypt', side_effect=SealFailure('ENCRYPTION_FAILED')):
            self.code('ENCRYPTION_FAILED', self.run_seal)
        self.no_marker()

    def test_upload_failure_prevents_marker(self):
        with patch.object(self.api, 'put', side_effect=RemoteFailure('DENIED')):
            self.code('REMOTE_WRITE_AMBIGUOUS', self.run_seal)
        self.no_marker()

    def test_repetition_never_overwrites(self):
        self.run_seal()
        self.code('EXPLICIT_VERIFICATION_REQUIRED', self.run_seal)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'put']), 7)

    def test_data_versions_lost_before_marker(self):
        # Simulates remote retention loss after upload; no AWS deletion is implemented.
        self.api.seed(SOURCE['data_bucket'], 'data-key', b'fixture')
        # Make a new capture including that data version.
        from scripts.backup.capture.engine import Capture
        from scripts.backup.sealing.hooks import InventoryHooks
        from scripts.backup.sealing.tests.support import Attestor
        self.bid = Capture(self.store, self.backend, InventoryHooks(self.api, SOURCE, Attestor(self.backend))).start()['operation_id']
        original = self.api.put
        def lost(bucket, key, body, **kwargs):
            result = original(bucket, key, body, **kwargs)
            if key.endswith('/manifest.json.age'):
                self.api.objects.pop((SOURCE['data_bucket'], 'data-key'))
            return result
        with patch.object(self.api, 'put', side_effect=lost):
            self.code('DATA_VERSION_UNAVAILABLE', self.run_seal)
        self.no_marker()

    def test_state_not_advanced_if_marker_response_lost(self):
        original = self.api.put
        def lost(bucket, key, body, **kwargs):
            result = original(bucket, key, body, **kwargs)
            if key.endswith('/completion.json'):
                raise RemoteFailure('TIMEOUT')
            return result
        with patch.object(self.api, 'put', side_effect=lost):
            self.code('REMOTE_WRITE_AMBIGUOUS', self.run_seal)
        transfer = self.store.read(self.bid + '/transfer-journal.json')
        self.assertEqual(transfer['status'], 'incomplete')
        self.assertEqual(transfer['objects']['completion.json']['phase'], 'verification_required')

    def test_storage_separation_required(self):
        self.code('STORAGE_SEPARATION', lambda: SimulatedSeal(self.api, self.store, self.age, SOURCE,
                  TEMPLATE['compatibility'], TEMPLATE['scope'], self.recipient, 'fixture', SOURCE['data_bucket']))

    def test_corrupted_receipt_hash_rejected(self):
        self.rewrite(lambda j: j['inventory_observation']['after'].update(projection_sha256='0' * 64))
        self.code('INVENTORY_RECEIPT_CHANGED', self.run_seal)
        self.no_marker()

    def test_total_ciphertext_limit_prevents_marker(self):
        from scripts.backup.sealing.common import Bounds
        self.seal.bounds = Bounds(total_artifact_bytes=1)
        self.code('TOTAL_CIPHERTEXT_LIMIT', self.run_seal)
        self.no_marker()

    def test_invalid_scope_fails_schema_before_marker(self):
        self.seal.scope['required_secret_values_encrypted'] = False
        self.code('SCHEMA_INVALID', self.run_seal)
        self.no_marker()

    def test_remote_artifact_corrupted_before_completion(self):
        original = self.api.put
        def corrupted(bucket, key, body, **kwargs):
            result = original(bucket, key, body, **kwargs)
            if key.endswith('/manifest.json.age'):
                record = self.api.objects[(bucket, key.replace('manifest.json.age', 'database.age'))][0]
                record['body'] = b'X' * len(record['body'])
            return result
        with patch.object(self.api, 'put', side_effect=corrupted):
            self.code('REMOTE_HASH_MISMATCH', self.run_seal)
        self.no_marker()

    def test_observation_after_window_rejected_before_any_write(self):
        self.rewrite(lambda j: j['inventory_observation']['after'].update(finished_at='2999-01-01T00:00:00Z'))
        self.code('CAPTURE_TIME_INCONSISTENT', self.run_seal)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_observation_before_window_rejected(self):
        self.rewrite(lambda j: j['inventory_observation']['before'].update(started_at='2000-01-01T00:00:00Z'))
        self.code('CAPTURE_TIME_INCONSISTENT', self.run_seal)
        self.no_marker()

    def test_missing_observation_time_rejected(self):
        self.rewrite(lambda j: j['inventory_observation']['before'].pop('started_at'))
        self.code('CAPTURE_TIME_INVALID', self.run_seal)
        self.no_marker()

    def test_duplicate_dump_phase_rejected(self):
        self.rewrite(lambda j: j['events'].append(copy.deepcopy(next(e for e in j['events']
                                            if e['phase'] == 'dump_catalog_checked'))))
        self.code('CAPTURE_SEQUENCE_INVALID', self.run_seal)

    def test_malformed_controls_rejected_with_safe_reason(self):
        self.rewrite(lambda j: j['inventory_observation'].update(controls_before=[]))
        self.code('WRITER_CONTROLS_UNPROVEN', self.run_seal)

    def test_changed_valid_journal_before_marker_rejected(self):
        original = self.api.put
        def mutated(bucket, key, body, **kwargs):
            result = original(bucket, key, body, **kwargs)
            if key.endswith('/manifest.json.age'):
                self.rewrite(lambda j: j['original'].update(config_fingerprint='different-fixture'))
            return result
        with patch.object(self.api, 'put', side_effect=mutated):
            self.code('CAPTURE_RECEIPT_CHANGED', self.run_seal)
        self.no_marker()

    def test_marker_gate_checks_exact_manifest_version(self):
        from scripts.backup.sealing.transport import Transport
        original = Transport.publish_completion
        def mismatched(transport, bundle, relative, expected):
            bundle = copy.deepcopy(bundle)
            bundle['completion']['manifest_version_id'] = 'another-version'
            return original(transport, bundle, relative, expected)
        with patch.object(Transport, 'publish_completion', mismatched):
            self.code('COMPLETION_SET_MISMATCH', self.run_seal)
        self.no_marker()

    def test_database_identifiers_cannot_be_supplied_for_another_capture(self):
        self.seal.compatibility['database_name'] = 'another_database'
        self.code('CAPTURE_DATABASE_MISMATCH', self.run_seal)
        self.assertFalse(any(c[0] == 'put' for c in self.api.calls))

    def test_missing_original_database_metadata_rejected(self):
        self.rewrite(lambda j: j['original'].pop('owner'))
        self.code('CAPTURE_DATABASE_MISMATCH', self.run_seal)
        self.no_marker()
