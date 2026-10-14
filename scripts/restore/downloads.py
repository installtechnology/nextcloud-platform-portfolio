"""Stream exact SSE-S3 artifact versions into private destination staging only."""
import hashlib
from scripts.backup.schemas import SchemaCatalog
from scripts.backup.semantics import validate_semantics
from scripts.backup.sealing.common import Bounds,Budget,SealFailure,require


def download_selected(reader,selected,store,operation,bounds=Bounds()):
    for role,doc in selected.bundle.items():SchemaCatalog().validate(role,doc)
    validate_semantics(selected.bundle)
    m=selected.bundle['manifest'];require(m['backup_id']==selected.backup_id,'RESTORE_SET_MISMATCH')
    require(sum(a['size_bytes'] for a in m['artifacts'].values())<=bounds.total_artifact_bytes,'RESTORE_TOTAL_SIZE_LIMIT')
    directory=store.operation(operation);require(directory.is_dir(),'RESTORE_STAGING_REQUIRED')
    results={};budget=Budget(bounds)
    for role,a in m['artifacts'].items():
        budget.check();require(a['bucket']==reader.bucket,'RESTORE_BUCKET_MISMATCH')
        try:r=reader.get(reader.bucket,a['key'],a['version_id'])
        except Exception:raise SealFailure('RESTORE_READ_FAILED') from None
        body=r.get('Body') if isinstance(r,dict) else None
        require(body is not None and callable(getattr(body,'close',None)),'RESTORE_RESPONSE_INVALID')
        try:
            require(r.get('VersionId')==a['version_id'] and r.get('ServerSideEncryption')=='AES256'
                    and type(r.get('ContentLength')) is int and r['ContentLength']==a['size_bytes'],
                    'RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
            def write(out):
                h=hashlib.sha256();count=0
                while True:
                    budget.check()
                    try:chunk=body.read(bounds.chunk_bytes)
                    except Exception:raise SealFailure('RESTORE_STREAM_FAILED') from None
                    if not chunk:break
                    require(isinstance(chunk,bytes) and len(chunk)<=bounds.chunk_bytes,'RESTORE_RESPONSE_INVALID')
                    count+=len(chunk);require(count<=a['size_bytes'],'RESTORE_SIZE_MISMATCH')
                    h.update(chunk);out.write(chunk)
                require(count==a['size_bytes'],'RESTORE_SIZE_MISMATCH')
                require(h.hexdigest()==a['sha256'],'RESTORE_HASH_MISMATCH')
            name=a['key'].rsplit('/',1)[1]
            results[role]=store.artifact(directory,name,write,min(bounds.artifact_bytes,a['size_bytes']))
        finally:body.close()
    return results
