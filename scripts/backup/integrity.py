"""Hash fixed v1 ciphertext/v2 artifact paths, never paths from S3 keys."""
import hashlib

from .errors import ValidationFailure, EXIT_INTEGRITY
from .safe_files import signature


def hash_stream(stream, limits):
    before = signature(stream)
    if before[2] > limits.artifact_bytes:
        raise ValidationFailure('ARTIFACT_LIMIT', EXIT_INTEGRITY)
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = stream.read(limits.chunk_bytes)
        if not chunk:
            break
        size += len(chunk)
        if size > limits.artifact_bytes:
            raise ValidationFailure('ARTIFACT_LIMIT', EXIT_INTEGRITY)
        digest.update(chunk)
    if before != signature(stream) or size != before[2]:
        raise ValidationFailure('FILE_CHANGED', EXIT_INTEGRITY)
    return size, digest.hexdigest()


def verify_artifacts(root, bundle, limits):
    if bundle['manifest']['schema_version'] == 2:
        from .sealing.completion import FILES
        names = {**{role: spec[0] for role, spec in FILES.items()}, 'object_inventory': 'object_inventory.json'}
        records = [('manifest', 'artifacts/manifest.json', None, bundle['completion']['manifest_sha256'])]
        for role, artifact in bundle['manifest']['artifacts'].items():
            records.append((role, 'artifacts/' + names[role], artifact['size_bytes'], artifact['sha256']))
        if 'evidence' in bundle:
            records.append(('restore_report', 'artifacts/restore-report.json', None, bundle['evidence']['report']['sha256']))
    else:
        records = [('manifest', 'ciphertext/manifest.json.age', None,
                    bundle['completion']['manifest_ciphertext_sha256'])]
        for role, artifact in bundle['manifest']['artifacts'].items():
            records.append((role, f'ciphertext/{role}.age', artifact['size_bytes'], artifact['ciphertext_sha256']))
        if 'evidence' in bundle:
            records.append(('restore_report', 'ciphertext/restore-report.age', None,
                            bundle['evidence']['report']['ciphertext_sha256']))
    total, verified = 0, []
    for role, path, expected_size, expected_hash in records:
        try:
            with root.open(path) as stream:
                size = signature(stream)[2]
                if size < 1:
                    raise ValidationFailure('ARTIFACT_EMPTY', EXIT_INTEGRITY)
                if expected_size is not None and size != expected_size:
                    raise ValidationFailure('ARTIFACT_SIZE_MISMATCH', EXIT_INTEGRITY)
                total += size
                if total > limits.total_artifact_bytes:
                    raise ValidationFailure('ARTIFACT_TOTAL_LIMIT', EXIT_INTEGRITY)
                actual_size, digest = hash_stream(stream, limits)
                if actual_size != size:
                    raise ValidationFailure('FILE_CHANGED', EXIT_INTEGRITY)
                if digest != expected_hash:
                    raise ValidationFailure('ARTIFACT_HASH_MISMATCH', EXIT_INTEGRITY)
            verified.append(role)
        except ValidationFailure as error:
            raise ValidationFailure(error.code, EXIT_INTEGRITY, document=role) from None
    return verified
