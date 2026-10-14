"""Local simulated orchestration. Never promotes PR3's candidate to an operational backup."""
import copy
import hashlib

from ..capture.engine import Capture, utc
from ..schemas import SchemaCatalog
from ..semantics import validate_semantics
from ..safe_files import SafeRoot, signature
from .common import Bounds, Budget, instant, json_bytes, require, sha, version
from .hooks import CONTROLS
from .inventory import projection, verify_data_versions
from .transport import Transport

FILES = {'database': ('database.dump', 'pg-custom'), 'configuration': ('configuration.tar', 'tar'),
         'local_files': ('local_files.tar', 'tar'), 'recovery_parameters': ('recovery_parameters.json', 'json')}


class SimulatedSeal:
    def __init__(self, api, store, age, source, compatibility, scope, recipient, recipient_id,
                 backups_bucket, bounds=Bounds(), cancelled=lambda: False):
        require(getattr(api, 'simulation', False) is True and store.synthetic is True, 'SIMULATION_ONLY')
        require(backups_bucket != source['data_bucket'], 'STORAGE_SEPARATION')
        self.api, self.store, self.age, self.source = api, store, age, copy.deepcopy(source)
        self.compatibility, self.scope = copy.deepcopy(compatibility), copy.deepcopy(scope)
        self.recipient, self.recipient_id, self.bucket = recipient, recipient_id, backups_bucket
        self.bounds, self.cancelled = bounds, cancelled

    def capture_receipt(self, bid):
        journal = self.store.read(bid + '/journal.json')
        require(Capture.valid_journal(journal, bid) and journal['status'] == 'local_candidate_only'
                and journal.get('service_state_restored') is True, 'CAPTURE_NOT_READY')
        active = self.store.read('active.json')
        require(active.get('operation_id') == bid and active.get('terminal') is True, 'CAPTURE_NOT_READY')
        require(journal['original']['identity'] == self.source['installation_identity_sha256'], 'SOURCE_IDENTITY_MISMATCH')
        require(all(journal['original'].get(k) == self.compatibility.get(v)
                    and isinstance(journal['original'].get(k), str)
                    for k, v in (('database', 'database_name'), ('owner', 'database_owner'), ('table_prefix', 'table_prefix'))),
                'CAPTURE_DATABASE_MISMATCH')
        observation = journal.get('inventory_observation')
        require(isinstance(observation, dict) and observation.get('simulation') is True
                and {'before', 'after', 'controls_before', 'controls_after'} <= observation.keys(), 'PAIRED_INVENTORY_REQUIRED')
        hashes = []
        for stage in ('before', 'after'):
            item = observation[stage]
            require(isinstance(item, dict) and {'document', 'delete_markers', 'projection_sha256'} <= item.keys(),
                    'INVALID_INVENTORY_RECEIPT')
            document = item['document']
            SchemaCatalog().validate('inventory', document)
            keys = [r['key'] for r in document['objects']]
            absent = document['absent_current_keys']
            require(document['object_count'] == len(keys) and len(keys) == len(set(keys))
                    and not set(keys).intersection(absent), 'INVALID_INVENTORY_RECEIPT')
            markers = item['delete_markers']
            require(isinstance(markers, list) and all(isinstance(r, dict) and set(r) == {'key', 'version_id'}
                    and isinstance(r['key'], str) and r['key']
                    for r in markers) and sorted(r['key'] for r in markers) == sorted(absent),
                    'INVALID_INVENTORY_RECEIPT')
            for marker in markers:
                version(marker['version_id'])
            require(document['backup_id'] == bid and document['consistency_id'] == journal['consistency_id']
                    and document['source'] == self.source, 'MIXED_INVENTORY')
            digest = sha(projection(document, item['delete_markers']))
            require(digest == item['projection_sha256'], 'INVENTORY_RECEIPT_CHANGED')
            controls = observation['controls_' + stage]
            require(isinstance(controls, dict) and controls.get('operation_id') == bid and controls.get('consistency_id') == journal['consistency_id']
                    and controls.get('simulation') is True and all(controls.get(k) is True for k in CONTROLS),
                    'WRITER_CONTROLS_UNPROVEN')
            hashes.append(digest)
        require(hashes[0] == hashes[1], 'INVENTORY_CHANGED')
        require(observation['controls_before'].get('fence_id')
                and observation['controls_before']['fence_id'] == observation['controls_after'].get('fence_id'),
                'WRITER_FENCE_CHANGED')
        require(all(isinstance(e, dict) and isinstance(e.get('phase'), str) for e in journal['events']),
                'CAPTURE_SEQUENCE_INVALID')
        phases = [e['phase'] for e in journal['events']]
        required = ['quiesced', 'inventory_before_intent', 'inventory_before_observed', 'dump_intent',
                    'dump_catalog_checked', 'configuration_captured', 'local_files_captured',
                    'recovery_parameters_captured', 'inventory_after_intent', 'inventory_after_observed',
                    'local_capture_finished', 'service_state_restored', 'candidate_ready']
        require(all(phases.count(p) == 1 for p in required) and [phases.index(p) for p in required] ==
                sorted(phases.index(p) for p in required), 'CAPTURE_SEQUENCE_INVALID')
        times = [instant(e.get('at')) for e in journal['events']]
        require(times == sorted(times), 'CAPTURE_TIME_INCONSISTENT')
        at = {p: instant(journal['events'][phases.index(p)].get('at')) for p in required}
        before, after = observation['before'], observation['after']
        timeline = [instant(journal.get('quiesced_at')), at['quiesced'], at['inventory_before_intent'],
                    instant(before.get('started_at')), instant(before.get('finished_at')),
                    at['inventory_before_observed'], at['dump_intent'], at['dump_catalog_checked'],
                    at['configuration_captured'], at['local_files_captured'], at['recovery_parameters_captured'],
                    at['inventory_after_intent'], instant(after.get('started_at')), instant(after.get('finished_at')),
                    at['inventory_after_observed'], instant(journal.get('capture_finished_at')),
                    at['local_capture_finished'], at['service_state_restored'], at['candidate_ready'], instant(utc())]
        require(timeline == sorted(timeline), 'CAPTURE_TIME_INCONSISTENT')
        require(set(journal['artifacts']) == set(FILES), 'CAPTURE_ARTIFACTS_INCOMPLETE')
        return journal, observation['after']['document'], hashes[0]

    def local_artifact(self, bid, name, data):
        return self.store.artifact(self.store.operation(bid), name, lambda out: out.write(data), 8 * 1024 ** 2)

    def seal(self, bid):
        with self.store.lock():
            return self._seal(bid)

    def _seal(self, bid):
        budget = Budget(self.bounds, self.cancelled)
        budget.check()
        # Validation precedes encryption/remote writes; missing fencing cannot publish anything.
        journal, inventory, inventory_hash = self.capture_receipt(bid)
        delete_markers = journal['inventory_observation']['after']['delete_markers']
        verify_data_versions(self.api, inventory, delete_markers)
        transport = Transport(self.api, self.store, bid, self.bucket, self.bounds, self.cancelled)
        transport.begin()
        artifacts = {}
        roles = {**FILES, 'object_inventory': ('object_inventory.json', 'json')}
        self.local_artifact(bid, 'object_inventory.json', json_bytes(inventory))
        for role, (name, format_name) in roles.items():
            budget.check()
            relative = bid + '/' + name
            with SafeRoot(self.store.root) as safe, safe.open(relative) as stream:
                info, digest = signature(stream), hashlib.sha256()
                require(0 < info[2] <= self.bounds.artifact_bytes, 'PLAINTEXT_LIMIT')
                while chunk := stream.read(self.bounds.chunk_bytes):
                    budget.check()
                    digest.update(chunk)
                require(info == signature(stream), 'PLAINTEXT_CHANGED')
                safe.assert_unchanged()
            plaintext_hash = digest.hexdigest()
            if role in FILES:
                expected = journal['artifacts'][role]
                require(expected['size_bytes'] == info[2] and expected['plaintext_sha256'] == plaintext_hash,
                        'CAPTURE_ARTIFACT_CHANGED')
            cipher_name = role + '.age'
            encrypted = self.age.encrypt(self.store.operation(bid), name, cipher_name, self.recipient,
                                         self.cancelled, plaintext_sha256=plaintext_hash)
            require(sum(a['size_bytes'] for a in artifacts.values()) + encrypted['size_bytes']
                    <= self.bounds.total_artifact_bytes, 'TOTAL_CIPHERTEXT_LIMIT')
            uploaded = transport.upload(cipher_name, bid + '/' + cipher_name, encrypted)
            artifacts[role] = {'kind': role, 'format': format_name, 'backup_id': bid,
                               'consistency_id': journal['consistency_id'], 'bucket': self.bucket,
                               'key': transport.prefix + cipher_name, 'version_id': uploaded['version_id'],
                               **encrypted, 'encryption': {'method': 'age-v1', 'recipient_id': self.recipient_id}}
        manifest = {'schema_version': 1, 'kind': 'recovery-manifest', 'backup_id': bid,
                    'status': 'complete_integrity_verified', 'created_at': utc(), 'mode': 'version_referenced_data_bucket',
                    'source': self.source, 'dependencies': {'data_bucket_and_versions_required': True,
                    'protects_against_data_bucket_loss': False, 'secret_recovery_required': True,
                    'private_decryption_key_required': True}, 'compatibility': self.compatibility,
                    'consistency': {'id': journal['consistency_id'], 'quiesced_at': journal['quiesced_at'],
                    'capture_finished_at': journal['capture_finished_at'], 'application_writers_quiesced': True,
                    'background_writers_quiesced': True, 'inflight_requests_drained': True,
                    'inventory_before_sha256': inventory_hash, 'inventory_after_sha256': inventory_hash,
                    'maintenance_initially_enabled': journal['original']['maintenance'],
                    'original_service_state_restored': True}, 'scope': self.scope, 'artifacts': artifacts,
                    'verification': {'dump_catalog_readable': True, 'ciphertext_checksums_verified_after_download': True,
                    'all_artifact_versions_available': True, 'all_referenced_data_versions_available_at_verification': True,
                    'restore_proven': False}}
        catalog = SchemaCatalog()
        catalog.validate('manifest', manifest)
        plain = self.local_artifact(bid, 'manifest.json', json_bytes(manifest))
        encrypted = self.age.encrypt(self.store.operation(bid), 'manifest.json', 'manifest.json.age', self.recipient,
                                     self.cancelled, plaintext_sha256=plain['plaintext_sha256'])
        require(sum(a['size_bytes'] for a in artifacts.values()) + encrypted['size_bytes']
                <= self.bounds.total_artifact_bytes, 'TOTAL_CIPHERTEXT_LIMIT')
        uploaded = transport.upload('manifest.json.age', bid + '/manifest.json.age', encrypted)
        marker = {'schema_version': 1, 'backup_id': bid, 'manifest_key': transport.prefix + 'manifest.json.age',
                  'manifest_version_id': uploaded['version_id'], 'manifest_ciphertext_sha256': encrypted['ciphertext_sha256'],
                  'completed_at': utc()}
        catalog.validate('completion', marker)
        validate_semantics({'manifest': manifest, 'inventory': inventory, 'completion': marker})
        # Recheck all exact versions immediately before marker. Marker is last remote write.
        latest, _, _ = self.capture_receipt(bid)
        require(json_bytes(latest) == json_bytes(journal), 'CAPTURE_RECEIPT_CHANGED')
        budget.check()
        verify_data_versions(self.api, inventory, delete_markers)
        for name, record in transport.state['objects'].items():
            transport.verify(name, record)
        marker_data = json_bytes(marker)
        self.local_artifact(bid, 'completion.json', marker_data)
        transport.publish_completion({'manifest': manifest, 'inventory': inventory, 'completion': marker}, bid + '/completion.json',
                         {'size_bytes': len(marker_data), 'ciphertext_sha256': sha(marker_data)})
        transport.state['status'] = 'simulated_integrity_verified'
        transport.save()
        # PR3 remains local_candidate_only. This result cannot authorize any operational action.
        return {'code': 'SIMULATED_SET_VERIFIED', 'simulation': True, 'backup_complete': False,
                'operationally_homologated': False, 'functional_restore': 'not_verified',
                'manifest': manifest, 'inventory': inventory, 'completion': marker}
