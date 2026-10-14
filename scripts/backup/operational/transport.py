"""Separate SSE-S3 artifact transport against the scoped real-SDK adapter.

Reuses PR5's transfer/verification algorithm without opening its simulated
constructor. Completion publication is delegated exclusively to CompletionWriter,
after live capture receipts, fencing and exact remote bytes have been checked.
"""
from ..capture.storage import Store
from ..sealing.transport import Transport
from ..sealing.common import Bounds, SealFailure, backup_id, require, json_bytes
from ..sealing.inventory import list_versions
from .s3 import BackupS3


class ArtifactTransport:
    names = {'database.dump', 'configuration.tar', 'local_files.tar', 'recovery_parameters.json', 'object_inventory.json', 'manifest.json'}
    hash_field = 'sha256'
    require_age = False
    # Algorithm methods shared; the original Transport SIMULATION_ONLY remains.
    save = Transport.save
    exact_history = Transport.exact_history
    upload = Transport.upload
    _upload = Transport._upload
    verify = Transport.verify

    def __init__(self, api, store, bid, bucket, bounds=Bounds(), cancelled=lambda: False):
        require(type(api) is BackupS3 and type(store) is Store and store.synthetic is False,
                'OPERATIONAL_ADAPTERS_REQUIRED')
        backup_id(bid)
        require(api.bucket == bucket and api.prefix == f'backups/{bid}/', 'TRANSFER_SCOPE_MISMATCH')
        self.api, self.store, self.bid, self.bucket = api, store, bid, bucket
        self.bounds, self.cancelled = bounds, cancelled
        self.prefix = api.prefix
        self.journal_path = bid + '/operational-transfer-journal.json'
        self.state = None

    def begin(self):
        path = self.store.root / self.journal_path
        require(not path.exists() and not path.is_symlink(), 'EXPLICIT_VERIFICATION_REQUIRED')
        require(not list_versions(self.api, self.bucket, self.prefix, self.bounds), 'HISTORICAL_PREFIX_COLLISION')
        state = {'internal_version': 1, 'kind': 'operational-sse-s3-transfer-journal',
                 'backup_id': self.bid, 'bucket': self.bucket, 'status': 'incomplete',
                 'objects': {}, 'backup_complete': False}
        self.store.artifact(self.store.operation(self.bid), 'operational-transfer-journal.json',
                            lambda out: out.write(json_bytes(state)), 1024 * 1024)
        self.state = state

    def retry_part(self, operation, budget):
        # No blind retry of ambiguous SDK writes. Known transient-part retry
        # classification remains pending pinned botocore service model review.
        budget.check()
        try:
            return operation()
        except Exception:
            raise SealFailure('MULTIPART_PART_FAILED') from None

    def publish_completion(self, *args, **kwargs):
        raise SealFailure('LIVE_CAPTURE_RECEIPT_REQUIRED')
