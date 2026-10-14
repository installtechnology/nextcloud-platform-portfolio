"""Offline orchestration. Success is scoped to the selected verification level."""
from .errors import ValidationFailure
from .integrity import verify_artifacts
from .json_input import load_document
from .limits import Limits
from .safe_files import SafeRoot
from .schemas import SchemaCatalog
from .semantics import validate_semantics


def validate(root_path, mode='local', manifest='manifest.json', inventory='inventory.json',
             completion='completion.json', evidence=None, limits=None):
    if mode not in ('local', 'documents'):
        raise ValidationFailure('INVALID_MODE')
    limits = limits or Limits()
    catalog = SchemaCatalog()
    evidence_absent = False
    with SafeRoot(root_path) as root:
        bundle = {role: load_document(root, path, role, limits)
                  for role, path in (('manifest', manifest), ('inventory', inventory), ('completion', completion))}
        if evidence is not None:
            bundle['evidence'] = load_document(root, evidence, 'evidence', limits)
        else:
            try:
                bundle['evidence'] = load_document(root, 'evidence.json', 'evidence', limits)
            except ValidationFailure as error:
                if error.code != 'FILE_MISSING':
                    raise
                evidence_absent = True
        for role, document in bundle.items():
            catalog.validate(role, document)
        if len(bundle['inventory']['objects']) > limits.inventory_objects:
            raise ValidationFailure('INVENTORY_LIMIT', document='inventory')
        validate_semantics(bundle)
        verified = verify_artifacts(root, bundle, limits) if mode == 'local' else []
        if evidence_absent:
            try:
                with root.open('evidence.json'):
                    raise ValidationFailure('FILE_CHANGED', document='evidence')
            except ValidationFailure as error:
                if error.code != 'FILE_MISSING':
                    raise
        root.assert_unchanged()
    return {'ok': True, 'conformity': 'passed',
            'local_integrity': 'verified' if mode == 'local' else 'not_checked',
            'verified_artifacts': verified,
            'restore_evidence': 'conformant_declaration' if 'evidence' in bundle else 'not_provided',
            'functional_restore': 'not_verified',
            'limitations': (['S3_VERSIONS_NOT_VERIFIED', 'SSE_S3_NOT_VERIFIED_OFFLINE', 'QUIESCENCE_NOT_VERIFIED']
                           if bundle['manifest']['schema_version'] == 2 else
                           ['S3_VERSIONS_NOT_VERIFIED', 'DECRYPTION_NOT_VERIFIED',
                            'PLAINTEXT_CIPHERTEXT_BINDING_NOT_VERIFIED', 'QUIESCENCE_NOT_VERIFIED'])}
