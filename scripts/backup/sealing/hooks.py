"""Optional paired observations while PR3 still controls its quiescent window."""
import copy
from ..capture.engine import utc

from .common import Bounds, SealFailure, require, json_bytes
from .inventory import inventory, projection
from .common import sha

CONTROLS = ('application_writers_fenced', 'background_jobs_fenced', 'external_writers_fenced',
            'inflight_requests_drained', 'fence_continuously_held')


class InventoryHooks:
    def __init__(self, api, source, attestor, bounds=Bounds()):
        require(getattr(api, 'simulation', False) is True, 'SIMULATION_ONLY')
        self.api, self.source, self.attestor, self.bounds = api, copy.deepcopy(source), attestor, bounds
        self.first = None

    def controls(self, journal, stage):
        try:
            evidence = self.attestor.check(journal['operation_id'], journal['consistency_id'], stage)
        except Exception:
            raise SealFailure('WRITER_EVIDENCE_UNAVAILABLE') from None
        require(isinstance(evidence, dict) and evidence.get('simulation') is True
                and evidence.get('operation_id') == journal['operation_id']
                and evidence.get('consistency_id') == journal['consistency_id']
                and all(evidence.get(k) is True for k in CONTROLS), 'WRITER_CONTROLS_UNPROVEN')
        require(isinstance(evidence.get('fence_id'), str) and evidence['fence_id'], 'WRITER_CONTROLS_UNPROVEN')
        return copy.deepcopy(evidence)

    def before(self, journal):
        require(self.first is None, 'INVENTORY_HOOK_REUSED')
        controls = self.controls(journal, 'before')
        started = utc()
        first = inventory(self.api, self.source, journal['operation_id'], journal['consistency_id'], self.bounds)
        final = self.controls(journal, 'before_complete')
        first.update(started_at=started, finished_at=utc())
        require(final['fence_id'] == controls['fence_id'], 'WRITER_FENCE_CHANGED')
        self.first = {'observation': first, 'controls': controls}
        result = {'simulation': True, 'before': first, 'controls_before': controls}
        require(len(json_bytes(result)) <= 256 * 1024, 'INVENTORY_JOURNAL_LIMIT')
        return result

    def after(self, journal):
        require(self.first is not None, 'INVENTORY_BEFORE_MISSING')
        require(self.first['controls']['operation_id'] == journal['operation_id']
                and self.first['controls']['consistency_id'] == journal['consistency_id'], 'INVENTORY_HOOK_REUSED')
        controls = self.controls(journal, 'after')
        require(controls['fence_id'] == self.first['controls']['fence_id'], 'WRITER_FENCE_CHANGED')
        started = utc()
        after = inventory(self.api, self.source, journal['operation_id'], journal['consistency_id'], self.bounds)
        final = self.controls(journal, 'after_complete')
        after.update(started_at=started, finished_at=utc())
        require(final['fence_id'] == controls['fence_id'], 'WRITER_FENCE_CHANGED')
        require(self.first['observation']['projection_sha256'] == after['projection_sha256'], 'INVENTORY_CHANGED')
        result = {'simulation': True, 'before': self.first['observation'],
                'controls_before': self.first['controls'], 'after': after, 'controls_after': final,
                'observation_integrity': sha(projection(after['document'], after['delete_markers']))}
        require(len(json_bytes(result)) <= 512 * 1024, 'INVENTORY_JOURNAL_LIMIT')
        return result
