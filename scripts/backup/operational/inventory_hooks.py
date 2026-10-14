"""PR3 hooks bound to the administrative window, never simulated receipt flags."""
import copy
from .fencing import HeldWindow, require
from .s3 import DataInventory
from ..sealing.inventory import inventory
from ..capture.engine import utc
from .inventory_projection import validate_source


class OperationalInventoryHooks:
    def __init__(self, api, source, window):
        validate_source(source)
        require(type(api) is DataInventory and type(window) is HeldWindow
                and source.get('data_bucket') == window.coordinator.scope.bucket
                and api.bucket == source['data_bucket'] and api.account == window.coordinator.scope.account,
                'INVENTORY_SCOPE_MISMATCH')
        require(source['account_id'] == api.account and source['region'] == api.client.meta.region_name,
                'INVENTORY_SCOPE_MISMATCH')
        self.api, self.source, self.window = api, copy.deepcopy(source), window
        self.first = None

    def observe(self, journal, phase):
        self.window.assert_held()
        operation = journal['operation_id']
        require(journal.get('original', {}).get('identity') == self.source['installation_identity_sha256'],
                'INVENTORY_INSTALLATION_MISMATCH')
        self.window.coordinator.bind(self.window, operation)
        started = utc()
        # Coordinator requires complete writer barrier and MPU checks in addition
        # to policy readback and stable inventories. No completion result here.
        receipt = self.window.coordinator.checkpoint(self.window, operation, phase)
        snapshot = inventory(self.api, self.source, operation, journal['consistency_id'])
        require(snapshot['projection_sha256'] == receipt['inventory_sha256'], 'FENCE_INVENTORY_CHANGED')
        # Detect policy changes after the potentially long listing as well.
        self.window.coordinator.compare(receipt['policy_sha256'])
        self.window.assert_held()
        return {'kind': 'operational-inventory-observation', 'fence_id': self.window.operation,
                'capture_id': operation, 'consistency_id': journal['consistency_id'],
                'started_at': started, 'finished_at': utc(), 'snapshot': snapshot,
                'receipt': receipt, 'backup_complete': False}

    def before(self, journal):
        require(self.first is None, 'INVENTORY_HOOK_REUSED')
        first = self.observe(journal, 'before')
        self.first = copy.deepcopy(first)
        return {'kind': 'operational-paired-inventory', 'fence_id': first['fence_id'],
                'capture_id': first['capture_id'], 'consistency_id': first['consistency_id'],
                'before': first, 'backup_complete': False}

    def after(self, journal):
        require(self.first is not None, 'INVENTORY_BEFORE_MISSING')
        require(self.first['capture_id'] == journal.get('operation_id')
                and self.first['consistency_id'] == journal.get('consistency_id'), 'INVENTORY_HOOK_REUSED')
        # PR3 replaces inventory_observation after this hook. Return the pair,
        # not only the final listing, to preserve both durable observations.
        saved = journal.get('inventory_observation')
        require(isinstance(saved, dict) and saved.get('kind') == 'operational-paired-inventory'
                and saved.get('before') == self.first and saved.get('backup_complete') is False
                and saved.get('capture_id') == self.first['capture_id']
                and saved.get('consistency_id') == self.first['consistency_id']
                and saved.get('fence_id') == self.first['fence_id'], 'INVENTORY_RECEIPT_CHANGED')
        after = self.observe(journal, 'after')
        require(after['fence_id'] == self.first['fence_id']
                and after['snapshot']['projection_sha256'] == self.first['snapshot']['projection_sha256'],
                'FENCE_INVENTORY_CHANGED')
        return {'kind': 'operational-paired-inventory', 'fence_id': after['fence_id'],
                'capture_id': after['capture_id'], 'consistency_id': after['consistency_id'],
                'before': copy.deepcopy(self.first), 'after': after, 'backup_complete': False}
