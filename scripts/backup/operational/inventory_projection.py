"""Real-adapter projection binding; PR5 performs versions/pagination validation."""
import copy
import hashlib
from .fence_policy import require
from .s3 import DataInventory
from ..sealing.inventory import inventory
from ..schemas import SchemaCatalog


def validate_source(source):
    require(isinstance(source, dict), 'INVENTORY_SOURCE_INVALID')
    catalog = SchemaCatalog()
    validator = catalog.validators['inventory']
    require(next(validator.descend(source, validator.schema['properties']['source']), None) is None,
            'INVENTORY_SOURCE_INVALID')


class InventoryProjection:
    def __init__(self, api, source, operation):
        validate_source(source)
        require(type(api) is DataInventory and api.bucket == source['data_bucket']
                and api.account == source['account_id'] and api.client.meta.region_name == source['region'],
                'INVENTORY_SCOPE_MISMATCH')
        from ..sealing.common import backup_id
        backup_id(operation)
        self.api, self.source, self.operation = api, copy.deepcopy(source), operation

    def projection_hash(self, bucket):
        require(bucket == self.source['data_bucket'], 'INVENTORY_SCOPE_MISMATCH')
        return inventory(self.api, self.source, self.operation,
                         'fence-' + hashlib.sha256(self.operation.encode()).hexdigest()[:32])['projection_sha256']
