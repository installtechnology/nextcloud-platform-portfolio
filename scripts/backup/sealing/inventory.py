"""Complete version pagination; current visibility is not an atomic S3 snapshot."""
import base64
import copy

from ..schemas import SchemaCatalog
from .common import Bounds, Budget, SealFailure, json_bytes, require, sha, version


def checksum(head):
    fields = {k: head['Checksum' + k] for k in ('SHA256', 'SHA1', 'CRC32', 'CRC32C', 'CRC64NVME')
              if 'Checksum' + k in head}
    if not fields:
        require('ChecksumType' not in head, 'INVALID_CHECKSUM')
        return None
    require(len(fields) == 1 and head.get('ChecksumType') in ('FULL_OBJECT', 'COMPOSITE'), 'INVALID_CHECKSUM')
    algorithm, value = next(iter(fields.items()))
    require(isinstance(value, str), 'INVALID_CHECKSUM')
    descriptor = {'algorithm': algorithm, 'type': head['ChecksumType']}
    if descriptor['type'] == 'COMPOSITE':
        body, separator, parts = value.rpartition('-')
        require(separator and parts.isdigit() and 1 <= int(parts) <= 10000
                and algorithm != 'CRC64NVME', 'INVALID_CHECKSUM')
        descriptor['part_count'] = int(parts)
        value = body
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise SealFailure('INVALID_CHECKSUM') from None
    require(len(decoded) == {'SHA256': 32, 'SHA1': 20, 'CRC32': 4, 'CRC32C': 4, 'CRC64NVME': 8}[algorithm]
            and base64.b64encode(decoded).decode() == value, 'INVALID_CHECKSUM')
    descriptor['value_base64'] = value
    return descriptor


def list_versions(api, bucket, prefix='', bounds=Bounds()):
    """Injected API returns decoded exact keys, no automatic URL/Unicode normalization."""
    records, seen, cursor, cursors, last_key = [], set(), None, set(), None
    budget = Budget(bounds)
    for _ in range(bounds.pages):
        budget.check()
        try:
            page = api.list_versions(bucket, prefix, cursor)
        except Exception:
            raise SealFailure('INVENTORY_LIST_FAILED') from None
        budget.check()
        require(isinstance(page, dict) and type(page.get('IsTruncated')) is bool, 'INCOMPLETE_LISTING')
        require('Error' not in page and not page.get('Delimiter') and not page.get('CommonPrefixes'),
                'GROUPED_OR_INVALID_LISTING')
        require(isinstance(page.get('Versions', []), list) and isinstance(page.get('DeleteMarkers', []), list),
                'INVALID_LISTING')
        require(len(page.get('Versions', [])) + len(page.get('DeleteMarkers', [])) <= 1000, 'INVENTORY_LIMIT')
        page_keys = []
        for field in ('Versions', 'DeleteMarkers'):
            values = page.get(field, [])
            require(isinstance(values, list), 'INVALID_LISTING')
            for item in values:
                require(isinstance(item, dict), 'INVALID_LISTING')
                key, vid = item.get('Key'), version(item.get('VersionId'))
                require(isinstance(key, str) and 0 < len(key) <= 1024 and key.startswith(prefix)
                        and type(item.get('IsLatest')) is bool, 'INVALID_LISTING')
                try:
                    encoded = key.encode('utf-8')
                except UnicodeError:
                    raise SealFailure('INVALID_LISTING') from None
                require(len(encoded) <= 1024, 'INVALID_LISTING')
                require(last_key is None or encoded >= last_key, 'PAGINATION_ORDER_INVALID')
                page_keys.append(encoded)
                require((key, vid) not in seen, 'DUPLICATE_VERSION_RECORD')
                seen.add((key, vid))
                require(len(records) < bounds.records, 'INVENTORY_LIMIT')
                record = copy.deepcopy(item)
                record['delete_marker'] = field == 'DeleteMarkers'
                if not record['delete_marker']:
                    require(type(item.get('Size')) is int and item['Size'] >= 0
                            and isinstance(item.get('ETag'), str) and item['ETag'], 'INVALID_LISTING')
                records.append(record)
        if page_keys:
            last_key = max(page_keys)
        if not page['IsTruncated']:
            require(not page.get('NextKeyMarker') and not page.get('NextVersionIdMarker'), 'INCOMPLETE_LISTING')
            return records
        key, vid = page.get('NextKeyMarker'), page.get('NextVersionIdMarker')
        require(isinstance(key, str) and key and key.startswith(prefix), 'INCOMPLETE_LISTING')
        version(vid)
        cursor = (key, vid)
        require(cursor not in cursors, 'PAGINATION_LOOP')
        cursors.add(cursor)
        require(page_keys, 'INCOMPLETE_LISTING')
        try:
            require(len(key.encode('utf-8')) <= 1024 and key.encode('utf-8') >= last_key, 'PAGINATION_ORDER_INVALID')
        except UnicodeError:
            raise SealFailure('INVALID_LISTING') from None
    raise SealFailure('INVENTORY_LIMIT')


def projection(document, delete_markers):
    # v1 projection is an internal producer convention, not a schema modification.
    return json_bytes({'projection_version': 1, 'source': document['source'],
                       'objects': sorted(document['objects'], key=lambda r: r['key'].encode('utf-8')),
                       'absent_current': sorted(delete_markers, key=lambda r: r['key'].encode('utf-8'))})


def inventory(api, source, bid, consistency_id, bounds=Bounds()):
    records = list_versions(api, source['data_bucket'], bounds=bounds)
    groups = {}
    for record in records:
        groups.setdefault(record['Key'], []).append(record)
    require(len(groups) <= 10000, 'INVENTORY_LIMIT')
    objects, absent = [], []
    for key, values in groups.items():
        latest = [r for r in values if r['IsLatest']]
        require(len(latest) == 1, 'AMBIGUOUS_CURRENT_VERSION')
        record = latest[0]
        if record['delete_marker']:
            absent.append({'key': key, 'version_id': record['VersionId']})
            continue
        try:
            head = api.head(source['data_bucket'], key, record['VersionId'])
        except Exception:
            raise SealFailure('DATA_VERSION_UNAVAILABLE') from None
        require(isinstance(head, dict) and head.get('VersionId') == record['VersionId']
                and type(head.get('ContentLength')) is int and head['ContentLength'] == record['Size']
                and head.get('ETag') == record['ETag'], 'DATA_VERSION_CHANGED')
        objects.append({'key': key, 'version_id': record['VersionId'], 'size_bytes': record['Size'],
                        'etag': record['ETag'], 'checksum': checksum(head)})
    document = {'schema_version': 1, 'backup_id': bid, 'consistency_id': consistency_id,
                'source': copy.deepcopy(source), 'selection': 'visible_current_objects_at_quiesced_capture',
                'listing_complete': True, 'object_count': len(objects),
                'objects': sorted(objects, key=lambda r: r['key'].encode('utf-8')),
                'absent_current_keys': sorted(r['key'] for r in absent)}
    SchemaCatalog().validate('inventory', document)
    return {'document': document, 'delete_markers': absent, 'projection_sha256': sha(projection(document, absent))}


def verify_data_versions(api, document, delete_markers=None):
    for record in document['objects']:
        try:
            head = api.head(document['source']['data_bucket'], record['key'], record['version_id'])
        except Exception:
            raise SealFailure('DATA_VERSION_UNAVAILABLE') from None
        require(isinstance(head, dict) and head.get('VersionId') == record['version_id']
                and type(head.get('ContentLength')) is int and head['ContentLength'] == record['size_bytes']
                and head.get('ETag') == record['etag'] and checksum(head) == record['checksum'], 'DATA_VERSION_CHANGED')
    if delete_markers:
        # HEAD on a specific delete marker has different S3 semantics; confirm it in version listing.
        records = list_versions(api, document['source']['data_bucket'])
        existing = {(r['Key'], r['VersionId']) for r in records if r['delete_marker']}
        require(all((r['key'], r['version_id']) in existing for r in delete_markers), 'DELETE_MARKER_UNAVAILABLE')
