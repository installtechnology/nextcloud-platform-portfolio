"""Validate durable live receipts. No simulated evidence promotion or IO writes."""
from ..capture.engine import Capture
from ..schemas import SchemaCatalog
from ..sealing.inventory import projection
from ..sealing.common import instant,sha,require


def reconciled_processes(store,fid,bid):
    """Execs belong to both IDs; the shared RealHostProbe owns helpers at fid."""
    for operation in (fid,bid):
        registry=store.read(operation+'/exec-journal.json')
        require(registry.get('operation_id')==operation and isinstance(registry.get('execs'),list)
                and all(e.get('phase')=='finished' and type(e.get('exit_code')) is int for e in registry['execs']),
                'LIVE_EXEC_RECONCILIATION_REQUIRED')
    helper_operations=[fid]
    # A capture-scoped registry is not produced by the current shared probe.
    # If present, still validate it; never hide a pending or unsafe extra file.
    extra=store.root/bid/'state-helper-journal.json'
    if extra.exists() or extra.is_symlink():helper_operations.append(bid)
    for operation in helper_operations:
        helpers=store.read(operation+'/state-helper-journal.json')
        require(helpers.get('operation_id')==operation and isinstance(helpers.get('helpers'),list)
                and all(h.get('phase')=='removed' for h in helpers['helpers']), 'LIVE_HELPER_RECONCILIATION_REQUIRED')


def live_receipt(store,bid,source):
    require(store.synthetic is False, 'OPERATIONAL_STORE_REQUIRED')
    journal=store.read(bid+'/journal.json')
    require(Capture.valid_journal(journal,bid) and journal.get('status')=='local_candidate_only'
            and journal.get('service_state_restored') is True
            and store.read('active.json')=={'operation_id':bid,'terminal':True}, 'CAPTURE_NOT_READY')
    require(journal['original']['identity']==source['installation_identity_sha256'], 'SOURCE_IDENTITY_MISMATCH')
    pair=journal.get('inventory_observation')
    require(isinstance(pair,dict) and pair.get('kind')=='operational-paired-inventory'
            and pair.get('capture_id')==bid and pair.get('consistency_id')==journal['consistency_id']
            and set(pair)=={'kind','fence_id','capture_id','consistency_id','before','after','backup_complete'}
            and pair['backup_complete'] is False, 'LIVE_INVENTORY_PAIR_REQUIRED')
    fid=pair['fence_id'];store.operation(fid)
    fence=store.read(fid+'/fence-journal.json')
    from .fence_policy import FenceScope
    from .fencing import Coordinator
    # Reuse the full control-journal validation without invoking an API or
    # weakening any factory guard. This object is only a local validation view.
    view=object.__new__(Coordinator);view.store=store;view.scope=FenceScope(**fence['scope'])
    fence=view.load(fid)
    require(fence.get('phase')=='restored' and fence.get('capture_id')==bid
            and fence.get('operation_id')==fid and fence.get('scope',{}).get('bucket')==source['data_bucket']
            and fence['scope'].get('account')==source['account_id']
            and fence.get('restored_hash')==fence.get('before_hash')
            and store.read('fence-active.json')=={'operation_id':fid,'terminal':True}, 'LIVE_FENCE_NOT_RECONCILED')
    observations=fence.get('observations');require(isinstance(observations,list) and len(observations)==2,
                                              'LIVE_FENCE_OBSERVATIONS_REQUIRED')
    hashes=[]
    for index,stage in enumerate(('before','after')):
        observation=pair[stage];snapshot=observation['snapshot'];document=snapshot['document']
        SchemaCatalog().validate('inventory',document)
        digest=sha(projection(document,snapshot['delete_markers']));hashes.append(digest)
        evidence=observations[index];receipt=observation['receipt']
        require(observation.get('kind')=='operational-inventory-observation'
                and observation.get('capture_id')==bid and observation.get('fence_id')==fid
                and observation.get('consistency_id')==journal['consistency_id']
                and document['source']==source and document['backup_id']==bid
                and document['consistency_id']==journal['consistency_id']
                and digest==snapshot['projection_sha256']==evidence['inventory_sha256']==receipt['inventory_sha256']
                and evidence['phase']==stage and evidence['capture_id']==bid
                and receipt['writer_evidence_sha256']==evidence['writer_evidence_sha256']
                and receipt['policy_sha256']==fence['fenced_hash'], 'LIVE_EVIDENCE_MISMATCH')
    require(hashes[0]==hashes[1], 'INVENTORY_CHANGED')
    database=store.read(bid+'/postgres-fence.json')
    require(database.get('phase')=='restored' and database.get('operation_id')==bid, 'DATABASE_FENCE_NOT_RESTORED')
    reconciled_processes(store,fid,bid)
    phases=[e['phase'] for e in journal['events']]
    ordered=['quiesced','inventory_before_intent','inventory_before_observed','dump_intent','dump_catalog_checked',
             'configuration_captured','local_files_captured','recovery_parameters_captured',
             'inventory_after_intent','inventory_after_observed','local_capture_finished','service_state_restored','candidate_ready']
    require(all(phases.count(p)==1 for p in ordered) and [phases.index(p) for p in ordered]==sorted(phases.index(p) for p in ordered),
            'CAPTURE_SEQUENCE_INVALID')
    times=[instant(e['at']) for e in journal['events']];require(times==sorted(times), 'CAPTURE_TIME_INCONSISTENT')
    at={p:instant(journal['events'][phases.index(p)]['at']) for p in ordered}
    timeline=[instant(journal['quiesced_at']),at['quiesced'],at['inventory_before_intent'],
              instant(pair['before']['started_at']),instant(pair['before']['finished_at']),at['inventory_before_observed'],
              at['dump_intent'],at['dump_catalog_checked'],at['configuration_captured'],at['local_files_captured'],
              at['recovery_parameters_captured'],at['inventory_after_intent'],instant(pair['after']['started_at']),
              instant(pair['after']['finished_at']),at['inventory_after_observed'],instant(journal['capture_finished_at']),
              at['local_capture_finished'],instant(fence['restored_at']),at['service_state_restored'],at['candidate_ready']]
    require(timeline==sorted(timeline), 'CAPTURE_TIME_INCONSISTENT')
    return journal,pair['after']['snapshot'],hashes[0]
