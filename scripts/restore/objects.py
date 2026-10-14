"""Exact-version copy algorithm. No automatic clients, IAM changes or activation."""
import hashlib
from scripts.backup.sealing.common import Bounds,Budget,SealFailure,require,version
from scripts.backup.sealing.inventory import list_versions,verify_data_versions
from scripts.backup.schemas import SchemaCatalog


def object_hash(api,bucket,key,vid,size,bounds):
    try:r=api.get(bucket,key,vid)
    except Exception:raise SealFailure('RESTORE_OBJECT_READ_FAILED') from None
    body=r.get('Body') if isinstance(r,dict) else None
    require(body is not None and callable(getattr(body,'close',None)),'RESTORE_OBJECT_RESPONSE_INVALID')
    count=0;h=hashlib.sha256();budget=Budget(bounds)
    try:
        require(r.get('VersionId')==vid and r.get('ContentLength')==size
                and type(r.get('ContentLength')) is int,'RESTORE_OBJECT_VERSION_MISMATCH')
        while True:
            budget.check()
            try:chunk=body.read(bounds.chunk_bytes)
            except Exception:raise SealFailure('RESTORE_OBJECT_READ_FAILED') from None
            if not chunk:break
            require(isinstance(chunk,bytes) and len(chunk)<=bounds.chunk_bytes,'RESTORE_OBJECT_RESPONSE_INVALID')
            count+=len(chunk);require(count<=size,'RESTORE_OBJECT_SIZE_MISMATCH');h.update(chunk)
        require(count==size,'RESTORE_OBJECT_SIZE_MISMATCH');return h.hexdigest()
    finally:body.close()


def retained_objects(source,destination,inventory,backups_bucket,state_bucket,bounds=Bounds()):
    """Read-only reuse of a complete byte-identical selected object set.

    This never repairs partial copies, overwrites keys or deletes history. The
    isolated host must still have empty database/volumes and exclusive control.
    Equal listings alone are not a proof of quiescence or functional recovery.
    """
    src=inventory['source']['data_bucket'];dst=destination.bucket
    require(src==source.bucket and dst not in (src,backups_bucket,state_bucket),'RESTORE_ORIGIN_TARGET_REJECTED')
    SchemaCatalog().validate('inventory',inventory)
    keys=[o['key'] for o in inventory['objects']];absent=set(inventory['absent_current_keys'])
    require(len(keys)==inventory['object_count'] and len(keys)==len(set(keys)) and not set(keys)&absent,
            'RESTORE_INVENTORY_INCONSISTENT')
    records=list_versions(destination,dst,bounds=bounds)
    if not records:return None
    groups={}
    for row in records:groups.setdefault(row['Key'],[]).append(row)
    require(set(keys)<=set(groups),'RESTORE_OBJECT_DESTINATION_NOT_EMPTY')
    current={};known=set(keys)|absent
    for key,rows in groups.items():
        latest=[r for r in rows if r['IsLatest']]
        require(len(latest)==1,'AMBIGUOUS_CURRENT_VERSION')
        # Keep unrelated keys and their histories untouched. This is logical
        # recovery of the selected backup, not garbage collection of the bucket.
        if key not in known:continue
        if latest[0]['delete_marker']:
            require(key in absent,'RESTORE_RETAINED_CURRENT_SET_MISMATCH')
        else:current[key]=latest[0]
    require(set(current)==set(keys),'RESTORE_RETAINED_CURRENT_SET_MISMATCH')
    verify_data_versions(source,inventory)
    mappings=[];budget=Budget(bounds)
    for o in inventory['objects']:
        budget.check();row=current[o['key']];vid=row['VersionId']
        require(row['Size']==o['size_bytes'],'RESTORE_OBJECT_SIZE_MISMATCH')
        try:head=destination.head(dst,o['key'],vid)
        except Exception:raise SealFailure('RESTORE_OBJECT_READ_FAILED') from None
        require(isinstance(head,dict) and head.get('VersionId')==vid
                and head.get('ServerSideEncryption')=='AES256','RESTORE_OBJECT_ENCRYPTION_MISMATCH')
        before=object_hash(source,src,o['key'],o['version_id'],o['size_bytes'],bounds)
        after=object_hash(destination,dst,o['key'],vid,o['size_bytes'],bounds)
        require(before==after,'RESTORE_OBJECT_HASH_MISMATCH')
        mappings.append({'key':o['key'],'source_version_id':o['version_id'],
                         'destination_version_id':vid,'sha256':after,'size_bytes':o['size_bytes']})
    def view(rows):return sorted((r['Key'],r['VersionId'],r['IsLatest'],r['delete_marker'],
                                 r.get('Size'),r.get('ETag')) for r in rows)
    require(view(list_versions(destination,dst,bounds=bounds))==view(records),
            'RESTORE_OBJECT_DESTINATION_CHANGED')
    budget.check()
    return {'objects':mappings,'restore_proven':False,'destroy_authorized':False}


def copy_selected(source,destination,inventory,backups_bucket,state_bucket,bounds=Bounds()):
    """Isolated empty destination, or exact read-only reuse of a complete set.

    Deliberately no automatic retries or reuse of a partially populated bucket.
    A failure preserves copies for explicit reconciliation. Large-object support
    requires a separately tested multipart-copy adapter, not a silent fallback.
    """
    src=inventory['source']['data_bucket'];dst=destination.bucket
    require(src==source.bucket and dst not in (src,backups_bucket,state_bucket),'RESTORE_ORIGIN_TARGET_REJECTED')
    SchemaCatalog().validate('inventory',inventory)
    keys=[o['key'] for o in inventory['objects']]
    require(len(keys)==inventory['object_count'] and len(keys)==len(set(keys))
            and not set(keys)&set(inventory['absent_current_keys']),'RESTORE_INVENTORY_INCONSISTENT')
    reused=retained_objects(source,destination,inventory,backups_bucket,state_bucket,bounds)
    if reused is not None:return reused
    verify_data_versions(source,inventory)
    require(all(o['size_bytes']<=5*1000**3 for o in inventory['objects']),'RESTORE_MULTIPART_COPY_REQUIRED')
    mappings=[];budget=Budget(bounds)
    for o in inventory['objects']:
        budget.check();before=object_hash(source,src,o['key'],o['version_id'],o['size_bytes'],bounds)
        try:r=destination.copy_version(src,o['key'],o['version_id'])
        except Exception:raise SealFailure('RESTORE_OBJECT_COPY_AMBIGUOUS') from None
        require(isinstance(r,dict) and r.get('ServerSideEncryption')=='AES256','RESTORE_OBJECT_ENCRYPTION_MISMATCH')
        vid=version(r.get('VersionId'))
        after=object_hash(destination,dst,o['key'],vid,o['size_bytes'],bounds)
        require(before==after,'RESTORE_OBJECT_HASH_MISMATCH')
        mappings.append({'key':o['key'],'source_version_id':o['version_id'],
                         'destination_version_id':vid,'sha256':after,'size_bytes':o['size_bytes']})
    records=list_versions(destination,dst,bounds=bounds)
    require(len(records)==len(mappings) and all(not r['delete_marker'] and r['IsLatest'] for r in records)
            and {(r['Key'],r['VersionId']) for r in records}=={(r['key'],r['destination_version_id']) for r in mappings},
            'RESTORE_OBJECT_DESTINATION_CHANGED')
    require(not set(inventory['absent_current_keys']) & {r['Key'] for r in records},'RESTORE_ABSENT_KEY_REACTIVATED')
    return {'objects':mappings,'restore_proven':False,'destroy_authorized':False}
