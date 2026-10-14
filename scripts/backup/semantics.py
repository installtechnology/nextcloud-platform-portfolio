"""Cross-document rules of recovery v1/v2; run only after schema validation."""
import base64
from datetime import datetime, timezone
from fractions import Fraction

from .errors import ValidationFailure, EXIT_SEMANTIC


def check(condition, code):
    if not condition:
        raise ValidationFailure(code, EXIT_SEMANTIC)


def timestamp(value):
    body = value[:-1]
    whole, separator, fraction = body.partition('.')
    return (datetime.fromisoformat(whole).replace(tzinfo=timezone.utc),
            Fraction(int(fraction), 10 ** len(fraction)) if separator else Fraction(0))


def validate_semantics(bundle):
    check({'manifest', 'inventory', 'completion'} <= bundle.keys(), 'INCOMPLETE_SET')
    manifest, inventory, marker = (bundle[k] for k in ('manifest', 'inventory', 'completion'))
    v2 = manifest['schema_version'] == 2
    check(marker['schema_version'] == manifest['schema_version'] and
          ('evidence' not in bundle or bundle['evidence']['schema_version'] == manifest['schema_version']), 'MIXED_CONTRACT_VERSION')
    bid, capture = manifest['backup_id'], manifest['consistency']['id']
    try:
        started = datetime.strptime(bid[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationFailure('INVALID_BACKUP_TIMESTAMP', EXIT_SEMANTIC) from None
    source, artifacts = manifest['source'], list(manifest['artifacts'].values())
    for artifact in artifacts:
        check(artifact['backup_id'] == bid and artifact['consistency_id'] == capture, 'MIXED_SET')
        check(artifact['key'].startswith(f'backups/{bid}/'), 'WRONG_PREFIX')
    buckets = {a['bucket'] for a in artifacts}
    check(len(buckets) == 1 and source['data_bucket'] not in buckets, 'STORAGE_SEPARATION')
    check(len({a['key'] for a in artifacts}) == len(artifacts), 'DUPLICATE_ARTIFACT')
    if not v2:
        check(len({a['encryption']['recipient_id'] for a in artifacts}) == 1, 'MIXED_ENCRYPTION_RECIPIENT')
    else:
        from .sealing.completion import FILES
        names = {**{role: spec[0] for role, spec in FILES.items()}, 'object_inventory': 'object_inventory.json'}
        check(all(a['key'] == f"backups/{bid}/{names[role]}" for role, a in manifest['artifacts'].items()), 'WRONG_ARTIFACT_NAME')
    compatibility = manifest['compatibility']
    check(compatibility['nextcloud_image'].startswith('nextcloud:' + compatibility['nextcloud_version'] + '-apache@'),
          'IMAGE_VERSION_MISMATCH')
    check(inventory['backup_id'] == bid and inventory['consistency_id'] == capture
          and inventory['source'] == source, 'MIXED_INVENTORY')
    check(inventory['object_count'] == len(inventory['objects']), 'OBJECT_COUNT')
    keys = [o['key'] for o in inventory['objects']]
    check(len(keys) == len(set(keys)), 'DUPLICATE_OBJECT_KEY')
    check(not set(keys) & set(inventory['absent_current_keys']), 'VISIBLE_AND_ABSENT')
    for record in inventory['objects']:
        checksum = record['checksum']
        if checksum is not None:
            try:
                digest = base64.b64decode(checksum['value_base64'], validate=True)
                lengths = {'SHA256': 32, 'SHA1': 20, 'CRC32': 4, 'CRC32C': 4, 'CRC64NVME': 8}
                valid = (len(digest) == lengths[checksum['algorithm']]
                         and base64.b64encode(digest).decode() == checksum['value_base64'])
            except ValueError:
                valid = False
            check(valid, 'INVALID_CHECKSUM_DESCRIPTOR')
    consistency = manifest['consistency']
    check(consistency['inventory_before_sha256'] == consistency['inventory_after_sha256'],
          'WRITERS_OR_INVENTORY_CHANGED')
    times = [(started, Fraction(0)), timestamp(consistency['quiesced_at']), timestamp(consistency['capture_finished_at']),
             timestamp(manifest['created_at']), timestamp(marker['completed_at'])]
    check(times == sorted(times), 'TIME_ORDER')
    check(marker['backup_id'] == bid and marker['manifest_key'] == f'backups/{bid}/manifest.json' + ('' if v2 else '.age'),
          'MIXED_COMPLETION')
    evidence = bundle.get('evidence')
    if evidence is not None:
        check(evidence['backup_id'] == bid and evidence['source'] == source
              and evidence['manifest_sha256' if v2 else 'manifest_ciphertext_sha256'] == marker['manifest_sha256' if v2 else 'manifest_ciphertext_sha256'],
              'UNBOUND_RESTORE_EVIDENCE')
        check(evidence['target']['data_bucket'] != source['data_bucket'], 'NONISOLATED_RESTORE')
        check(times[-1] <= timestamp(evidence['started_at']) <= timestamp(evidence['finished_at']), 'RESTORE_TIME_ORDER')
        check(evidence['report']['key'].startswith(f'evidence/{bid}/'), 'MIXED_RESTORE_REPORT')
        if v2:
            check(evidence['report']['key'] == f'evidence/{bid}/restore-report.json', 'MIXED_RESTORE_REPORT')
