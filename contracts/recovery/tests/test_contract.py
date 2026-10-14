"""Synthetic contract examples only. Not an operational backup/restore validator."""
import copy
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

BASE = Path(__file__).resolve().parents[1]
DOCUMENTS = {
    'manifest': ('complete-unproven.json', 'manifest-v1.schema.json'),
    'inventory': ('object-inventory.json', 'object-inventory-v1.schema.json'),
    'completion': ('completion.json', 'completion-v1.schema.json'),
    'evidence': ('restore-evidence.json', 'restore-evidence-v1.schema.json'),
}


def load_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_pairs)


def sample_bundle():
    return {name: load_json(BASE / 'fixtures' / files[0]) for name, files in DOCUMENTS.items()}


def semantic_example_errors(bundle):
    """Test oracle illustrating normative rules; no artifact I/O or production CLI."""
    errors = []
    if not {'manifest', 'inventory', 'completion'} <= bundle.keys():
        return ['INCOMPLETE_SET']
    manifest, inventory, marker = (bundle[k] for k in ('manifest', 'inventory', 'completion'))
    evidence = bundle.get('evidence')
    bid, capture = manifest['backup_id'], manifest['consistency']['id']
    try:
        backup_started = datetime.strptime(bid[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return ['INVALID_BACKUP_TIMESTAMP']
    source = manifest['source']
    for artifact in manifest['artifacts'].values():
        if artifact['backup_id'] != bid or artifact['consistency_id'] != capture:
            errors.append('MIXED_SET')
        if not artifact['key'].startswith(f'backups/{bid}/'):
            errors.append('WRONG_PREFIX')
    buckets = {a['bucket'] for a in manifest['artifacts'].values()}
    if len(buckets) != 1 or source['data_bucket'] in buckets:
        errors.append('STORAGE_SEPARATION')
    keys = [a['key'] for a in manifest['artifacts'].values()]
    if len(keys) != len(set(keys)):
        errors.append('DUPLICATE_ARTIFACT')
    recipients = {a['encryption']['recipient_id'] for a in manifest['artifacts'].values()}
    if len(recipients) != 1:
        errors.append('MIXED_ENCRYPTION_RECIPIENT')
    compatibility = manifest['compatibility']
    if not compatibility['nextcloud_image'].startswith('nextcloud:' + compatibility['nextcloud_version'] + '-apache@'):
        errors.append('IMAGE_VERSION_MISMATCH')
    if inventory['backup_id'] != bid or inventory['consistency_id'] != capture or inventory['source'] != source:
        errors.append('MIXED_INVENTORY')
    if inventory['object_count'] != len(inventory['objects']):
        errors.append('OBJECT_COUNT')
    object_keys = [o['key'] for o in inventory['objects']]
    if len(object_keys) != len(set(object_keys)):
        errors.append('DUPLICATE_OBJECT_KEY')
    if set(object_keys) & set(inventory['absent_current_keys']):
        errors.append('VISIBLE_AND_ABSENT')
    for record in inventory['objects']:
        checksum = record['checksum']
        if checksum is not None:
            digest = base64.b64decode(checksum['value_base64'], validate=True)
            lengths = {'SHA256': 32, 'SHA1': 20, 'CRC32': 4, 'CRC32C': 4, 'CRC64NVME': 8}
            if len(digest) != lengths[checksum['algorithm']] or base64.b64encode(digest).decode() != checksum['value_base64']:
                errors.append('INVALID_CHECKSUM_DESCRIPTOR')
    consistency = manifest['consistency']
    if consistency['inventory_before_sha256'] != consistency['inventory_after_sha256']:
        errors.append('WRITERS_OR_INVENTORY_CHANGED')
    times = [backup_started,
             datetime.fromisoformat(consistency['quiesced_at'].replace('Z', '+00:00')),
             datetime.fromisoformat(consistency['capture_finished_at'].replace('Z', '+00:00')),
             datetime.fromisoformat(manifest['created_at'].replace('Z', '+00:00')),
             datetime.fromisoformat(marker['completed_at'].replace('Z', '+00:00'))]
    if times != sorted(times):
        errors.append('TIME_ORDER')
    if marker['backup_id'] != bid or marker['manifest_key'] != f'backups/{bid}/manifest.json.age':
        errors.append('MIXED_COMPLETION')
    if evidence is None:
        return errors
    if (evidence['backup_id'] != bid or evidence['source'] != source
            or evidence['manifest_ciphertext_sha256'] != marker['manifest_ciphertext_sha256']):
        errors.append('UNBOUND_RESTORE_EVIDENCE')
    if evidence['target']['data_bucket'] == source['data_bucket']:
        errors.append('NONISOLATED_RESTORE')
    start, finish = (datetime.fromisoformat(evidence[k].replace('Z', '+00:00')) for k in ('started_at', 'finished_at'))
    if not times[-1] <= start <= finish:
        errors.append('RESTORE_TIME_ORDER')
    if not evidence['report']['key'].startswith(f'evidence/{bid}/'):
        errors.append('MIXED_RESTORE_REPORT')
    return errors


def mutate(document, case):
    parts = case['path'].strip('/').split('/')
    parent = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = parts[-1]
    if isinstance(parent, list):
        if key == '-':
            parent.append(case['value'])
        else:
            parent[int(key)] = case['value']
    elif case['op'] == 'remove':
        del parent[key]
    else:
        parent[key] = case['value']


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validators = {}
        checker = FormatChecker()
        if 'date-time' not in checker.checkers:
            raise RuntimeError('Install all test requirements: RFC3339 checker required')
        for name, (_, filename) in DOCUMENTS.items():
            schema = load_json(BASE / 'schemas' / filename)
            Draft202012Validator.check_schema(schema)
            cls.validators[name] = Draft202012Validator(schema, format_checker=checker)

    def test_positive_synthetic_examples(self):
        bundle = sample_bundle()
        for name, document in bundle.items():
            self.validators[name].validate(document)
        self.assertEqual(semantic_example_errors(bundle), [])

    def test_integrity_never_claims_functional_restore(self):
        manifest = sample_bundle()['manifest']
        self.validators['manifest'].validate(manifest)
        self.assertFalse(manifest['verification']['restore_proven'])
        # Functional evidence is separate, not obtained from a checksum or this fixture.
        self.assertNotIn('restore_evidence', manifest)

    def test_empty_bucket_inventory_can_be_complete(self):
        bundle = sample_bundle()
        bundle['inventory'].update(objects=[], object_count=0, absent_current_keys=[])
        self.validators['inventory'].validate(bundle['inventory'])
        self.assertEqual(semantic_example_errors(bundle), [])

    def test_complete_unproven_set_needs_no_restore_evidence(self):
        bundle = sample_bundle()
        del bundle['evidence']
        self.assertEqual(semantic_example_errors(bundle), [])
        self.assertFalse(bundle['manifest']['verification']['restore_proven'])

    def test_no_completion_marker_means_incomplete(self):
        bundle = sample_bundle()
        del bundle['completion']
        self.assertEqual(semantic_example_errors(bundle), ['INCOMPLETE_SET'])

    def test_checksum_string_is_only_structural_evidence(self):
        manifest = sample_bundle()['manifest']
        manifest['artifacts']['database']['ciphertext_sha256'] = '0' * 64
        # There are no artifact bytes in PR1. Schema cannot verify this declaration.
        self.validators['manifest'].validate(manifest)
        self.assertFalse(manifest['verification']['restore_proven'])

    def test_latest_is_not_a_backup_id(self):
        marker = sample_bundle()['completion']
        marker['backup_id'] = 'latest'
        self.assertFalse(self.validators['completion'].is_valid(marker))

    def test_manifest_version_is_mandatory(self):
        marker = sample_bundle()['completion']
        del marker['manifest_version_id']
        self.assertFalse(self.validators['completion'].is_valid(marker))

    def test_invalid_datetime_rejected(self):
        manifest = sample_bundle()['manifest']
        manifest['created_at'] = '2026-02-30T12:00:00Z'
        self.assertFalse(self.validators['manifest'].is_valid(manifest))

    def test_unknown_fields_rejected_in_nested_records(self):
        inventory = sample_bundle()['inventory']
        inventory['objects'][0]['secret'] = 'synthetic-forbidden'
        self.assertFalse(self.validators['inventory'].is_valid(inventory))

    def test_missing_required_local_files_rejected(self):
        manifest = sample_bundle()['manifest']
        del manifest['artifacts']['local_files']
        self.assertFalse(self.validators['manifest'].is_valid(manifest))

    def test_duplicate_artifact_key_rejected(self):
        bundle = sample_bundle()
        bundle['manifest']['artifacts']['local_files']['key'] = bundle['manifest']['artifacts']['configuration']['key']
        self.assertIn('DUPLICATE_ARTIFACT', semantic_example_errors(bundle))

    def test_schema_validation_requires_no_network(self):
        with patch('socket.socket', side_effect=AssertionError('NETWORK_FORBIDDEN')):
            for name, document in sample_bundle().items():
                self.validators[name].validate(document)

    def test_shared_schema_identifiers_and_source_types_match(self):
        schemas = {k: load_json(BASE / 'schemas' / names[1]) for k, names in DOCUMENTS.items()}
        self.assertEqual(len({s['$id'] for s in schemas.values()}), 4)
        for name, schema in schemas.items():
            self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
            self.assertEqual(schema['properties']['schema_version'], {'const': 1})
            self.assertEqual(schema['properties']['backup_id'], schemas['manifest']['properties']['backup_id'])
        for name in ('inventory', 'evidence'):
            self.assertEqual(schemas[name]['properties']['source'], schemas['manifest']['properties']['source'])
        version_type = schemas['completion']['properties']['manifest_version_id']
        self.assertEqual(schemas['inventory']['properties']['objects']['items']['properties']['version_id'], version_type)
        self.assertEqual(schemas['evidence']['properties']['report']['properties']['version_id'], version_type)
        for artifact in schemas['manifest']['properties']['artifacts']['properties'].values():
            self.assertEqual(artifact['properties']['version_id'], version_type)
            self.assertEqual(artifact['properties']['backup_id'], schemas['manifest']['properties']['backup_id'])

    def test_schemas_have_no_external_refs_or_destroy_permissions(self):
        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn('$ref', node)
                self.assertNotIn('$dynamicRef', node)
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        for _, filename in DOCUMENTS.values():
            walk(load_json(BASE / 'schemas' / filename))
        for name, document in sample_bundle().items():
            document['destroy_authorized'] = True
            self.assertFalse(self.validators[name].is_valid(document))

    def test_all_version_references_reject_missing_null_and_whitespace(self):
        paths = [('manifest', '/artifacts/' + kind + '/version_id') for kind in sample_bundle()['manifest']['artifacts']]
        paths += [('inventory', '/objects/0/version_id'), ('completion', '/manifest_version_id'), ('evidence', '/report/version_id')]
        for document, path in paths:
            for value in (None, '', ' ', 'null', 'version\n'):
                with self.subTest(document=document, path=path, value=value):
                    bundle = sample_bundle()
                    mutate(bundle[document], {'op': 'replace', 'path': path, 'value': value})
                    self.assertFalse(self.validators[document].is_valid(bundle[document]))
            bundle = sample_bundle()
            mutate(bundle[document], {'op': 'remove', 'path': path})
            self.assertFalse(self.validators[document].is_valid(bundle[document]))

    def test_valid_opaque_versions_are_not_treated_as_digests(self):
        inventory = sample_bundle()['inventory']
        inventory['objects'][0]['version_id'] = 'opaque-version.+/=_-'
        self.validators['inventory'].validate(inventory)

    def test_all_capture_and_integrity_assertions_are_required(self):
        paths = ['/consistency/' + name for name in ('application_writers_quiesced', 'background_writers_quiesced',
                 'inflight_requests_drained', 'original_service_state_restored')]
        paths += ['/verification/' + name for name in ('dump_catalog_readable', 'ciphertext_checksums_verified_after_download',
                  'all_artifact_versions_available', 'all_referenced_data_versions_available_at_verification')]
        for path in paths:
            for operation in ('replace', 'remove'):
                with self.subTest(path=path, operation=operation):
                    manifest = sample_bundle()['manifest']
                    mutate(manifest, {'op': operation, 'path': path, 'value': False})
                    self.assertFalse(self.validators['manifest'].is_valid(manifest))

    def test_every_restore_check_is_required_but_still_only_a_declaration(self):
        example = sample_bundle()['evidence']
        for name in example['checks']:
            for operation in ('replace', 'remove'):
                with self.subTest(check=name, operation=operation):
                    evidence = copy.deepcopy(example)
                    mutate(evidence, {'op': operation, 'path': '/checks/' + name, 'value': False})
                    self.assertFalse(self.validators['evidence'].is_valid(evidence))
        self.validators['evidence'].validate(example)
        self.assertNotIn('destroy_authorized', example)

    def test_every_required_artifact_cannot_be_omitted(self):
        for kind in sample_bundle()['manifest']['artifacts']:
            with self.subTest(kind=kind):
                manifest = sample_bundle()['manifest']
                del manifest['artifacts'][kind]
                self.assertFalse(self.validators['manifest'].is_valid(manifest))

    def test_dates_require_valid_calendar_and_utc_for_all_documents(self):
        paths = [('manifest', '/created_at'), ('manifest', '/consistency/quiesced_at'),
                 ('manifest', '/consistency/capture_finished_at'), ('completion', '/completed_at'),
                 ('evidence', '/started_at'), ('evidence', '/finished_at')]
        for document, path in paths:
            for value in ('2026-02-30T12:00:00Z', '2026-10-09T12:00:00+01:00', '2026-10-09T12:00:00Z\n'):
                with self.subTest(document=document, path=path, value=value):
                    bundle = sample_bundle()
                    mutate(bundle[document], {'op': 'replace', 'path': path, 'value': value})
                    self.assertFalse(self.validators[document].is_valid(bundle[document]))

    def test_fractional_utc_dates_are_supported(self):
        marker = sample_bundle()['completion']
        marker['completed_at'] = '2026-10-09T12:05:01.123456Z'
        self.validators['completion'].validate(marker)

    def test_artifact_paths_are_not_local_filesystem_paths(self):
        for key in ('/etc/passwd', 'C:\\Users\\fixture\\config', 'backups/../config.age', 'backups/fixture/../config.age'):
            with self.subTest(key=key):
                manifest = sample_bundle()['manifest']
                manifest['artifacts']['configuration']['key'] = key
                self.assertFalse(self.validators['manifest'].is_valid(manifest))

    def test_digest_shape_is_checked_not_registry_authenticity(self):
        for image in ('nextcloud_image', 'postgres_image'):
            manifest = sample_bundle()['manifest']
            manifest['compatibility'][image] = manifest['compatibility'][image].split('@')[0]
            self.assertFalse(self.validators['manifest'].is_valid(manifest))
        # Synthetic digests are deliberately accepted as shape only, never official-image proof.
        self.validators['manifest'].validate(sample_bundle()['manifest'])

    def test_full_and_composite_checksum_descriptors_are_distinct(self):
        for checksum in ({'algorithm': 'SHA256', 'value_base64': base64.b64encode(bytes(32)).decode(), 'type': 'FULL_OBJECT'},
                         {'algorithm': 'SHA256', 'value_base64': base64.b64encode(bytes(32)).decode(), 'type': 'COMPOSITE', 'part_count': 2},
                         {'algorithm': 'CRC64NVME', 'value_base64': base64.b64encode(bytes(8)).decode(), 'type': 'FULL_OBJECT'}):
            with self.subTest(checksum=checksum):
                bundle = sample_bundle()
                bundle['inventory']['objects'][0]['checksum'] = checksum
                self.validators['inventory'].validate(bundle['inventory'])
                self.assertEqual(semantic_example_errors(bundle), [])

    def test_rejection_fixture_names_unique(self):
        names = [c['name'] for c in load_json(BASE / 'fixtures/rejection-cases.json')]
        self.assertEqual(len(names), len(set(names)))

    def test_every_json_has_unique_keys(self):
        for path in BASE.rglob('*.json'):
            load_json(path)


def rejection_test(case):
    def test(self):
        bundle = copy.deepcopy(sample_bundle())
        mutate(bundle[case['document']], case)
        for extra in case.get('additional_mutations', []):
            mutate(bundle[case['document']], extra)
        valid = self.validators[case['document']].is_valid(bundle[case['document']])
        if case['rejection_layer'] == 'schema':
            self.assertFalse(valid, case['name'])
            validators = {e.validator for e in self.validators[case['document']].iter_errors(bundle[case['document']])}
            self.assertIn(case['expected_validator'], validators, case['name'])
        else:
            self.assertTrue(valid, 'Expected a structurally valid semantic counterexample')
            self.assertEqual(semantic_example_errors(bundle), [case['expected_error']], case['name'])
    return test


for case in load_json(BASE / 'fixtures/rejection-cases.json'):
    setattr(ContractTests, 'test_reject_' + case['name'], rejection_test(case))


if __name__ == '__main__':
    unittest.main()
