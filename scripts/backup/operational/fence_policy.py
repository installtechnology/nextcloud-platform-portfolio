"""Pure, additive S3 fence construction. No credentials, SDK or IO.

API operations are not IAM action names: PutObject authorizes multipart
initiation, parts and completion, including uploads initiated before fencing.
No time condition or exemption from object denies, including the coordinator.
"""
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from ..json_input import parse_json
from ..limits import Limits
from ..capture.storage import CaptureFailure


OBJECT_MUTATIONS = (
    's3:PutObject', 's3:DeleteObject', 's3:DeleteObjectVersion',
    's3:AbortMultipartUpload', 's3:PutObjectAcl', 's3:PutObjectVersionAcl',
    's3:PutObjectTagging', 's3:PutObjectVersionTagging',
    's3:DeleteObjectTagging', 's3:DeleteObjectVersionTagging',
    's3:RestoreObject', 's3:ReplicateObject', 's3:ReplicateDelete', 's3:ReplicateTags',
)
CONFIGURATION_MUTATIONS = ('s3:PutLifecycleConfiguration', 's3:PutBucketVersioning',
                           's3:PutReplicationConfiguration', 's3:PutBucketObjectLockConfiguration',
                           's3:DeleteBucket')


def require(value, code):
    if not value:
        raise CaptureFailure(code)


@dataclass(frozen=True, repr=False)
class FenceScope:
    account: str
    bucket: str
    administrative_role: str
    emergency_role: str

    def __post_init__(self):
        require(isinstance(self.account, str) and re.fullmatch(r'[0-9]{12}', self.account), 'FENCE_SCOPE_INVALID')
        require(isinstance(self.bucket, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', self.bucket), 'FENCE_SCOPE_INVALID')
        for role in (self.administrative_role, self.emergency_role):
            require(isinstance(role, str) and re.fullmatch(
                r'arn:aws:iam::' + self.account + r':role/[A-Za-z0-9_+=,.@/-]{1,128}', role), 'FENCE_ROLE_INVALID')
        require(self.administrative_role != self.emergency_role, 'FENCE_EMERGENCY_ROLE_REQUIRED')


def policy_document(raw):
    require(isinstance(raw, str), 'FENCE_POLICY_REQUIRED')
    try:
        document = parse_json(raw.encode('utf-8'), Limits(json_bytes=20 * 1024))
    except Exception:
        raise CaptureFailure('FENCE_POLICY_INVALID') from None
    require(isinstance(document, dict) and document.get('Version') in ('2012-10-17', '2008-10-17')
            and isinstance(document.get('Statement'), list) and document['Statement']
            and all(isinstance(s, dict) for s in document['Statement']), 'FENCE_POLICY_INVALID')
    return document


def canonical(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def digest(document):
    return hashlib.sha256(canonical(document).encode()).hexdigest()


def build_policy(raw, scope, operation):
    require(type(scope) is FenceScope, 'FENCE_SCOPE_REQUIRED')
    require(isinstance(operation, str) and re.fullmatch(
        r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', operation), 'INVALID_OPERATION_ID')
    original = policy_document(raw)
    suffix = hashlib.sha256(operation.encode()).hexdigest()
    sids = ('BackupObjectFence' + suffix, 'BackupPolicyFence' + suffix, 'BackupConfigurationFence' + suffix)
    require(not any(str(s.get('Sid', '')).startswith(('BackupObjectFence', 'BackupPolicyFence', 'BackupConfigurationFence'))
                    for s in original['Statement']), 'EXISTING_FENCE_REQUIRES_RECONCILIATION')
    fenced = copy.deepcopy(original)
    fenced['Statement'].extend([
        {'Sid': sids[2], 'Effect': 'Deny', 'Principal': '*',
         'Action': list(CONFIGURATION_MUTATIONS), 'Resource': f'arn:aws:s3:::{scope.bucket}'},
        {'Sid': sids[0], 'Effect': 'Deny', 'Principal': '*',
         'Action': list(OBJECT_MUTATIONS), 'Resource': f'arn:aws:s3:::{scope.bucket}/*'},
        {'Sid': sids[1], 'Effect': 'Deny', 'Principal': '*',
         'Action': ['s3:PutBucketPolicy', 's3:DeleteBucketPolicy'],
         'Resource': f'arn:aws:s3:::{scope.bucket}',
         'Condition': {'ArnNotEquals': {'aws:PrincipalArn': [scope.administrative_role, scope.emergency_role]}}},
    ])
    encoded = canonical(fenced)
    require(len(encoded.encode()) <= 20 * 1024, 'FENCE_POLICY_LIMIT')
    return fenced
