"""Read-only selection and byte verification; never restore or authorize Destroy."""
from dataclasses import dataclass, field
import hashlib
import re

from scripts.backup.errors import ValidationFailure
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.schemas import SchemaCatalog
from scripts.backup.semantics import validate_semantics
from scripts.backup.sealing.common import Bounds, Budget, SealFailure, backup_id, instant, require, version
from scripts.backup.sealing.inventory import list_versions, verify_data_versions


class ReadFailure(SealFailure):
    """Transport failures abort selection; never silently select an older backup."""


@dataclass(frozen=True, repr=False)
class SelectedBackup:
    backup_id: str
    completion_version: str
    manifest_sha256: str
    bundle: dict = field(repr=False)
    rejected_candidates: tuple = ()


class Selector:
    def __init__(self, reader, data_reader, source, bounds=Bounds()):
        require(reader.bucket != source['data_bucket'], 'RESTORE_STORAGE_SEPARATION')
        self.reader,self.data_reader,self.source,self.bounds=reader,data_reader,source,bounds
        self.catalog=SchemaCatalog()

    def read(self, key, vid, *, size=None, digest=None, document=False):
        if hasattr(self,'_budget'):self._budget.check()
        version(vid)
        maximum=Limits().json_bytes if document else self.bounds.artifact_bytes
        if size is not None:require(type(size) is int and 0 < size <= maximum,'RESTORE_SIZE_LIMIT')
        try:r=self.reader.get(self.reader.bucket,key,vid)
        except BaseException:raise ReadFailure('RESTORE_READ_FAILED') from None
        body=r.get('Body') if isinstance(r,dict) else None
        require(body is not None and callable(getattr(body,'read',None))
                and callable(getattr(body,'close',None)), 'RESTORE_RESPONSE_INVALID')
        chunks=[];h=hashlib.sha256();count=0;budget=Budget(self.bounds)
        try:
            require(r.get('VersionId')==vid and r.get('ServerSideEncryption')=='AES256'
                    and type(r.get('ContentLength')) is int and 0 < r['ContentLength'] <= maximum,
                    'RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
            if size is not None:require(size==r['ContentLength'],'RESTORE_SIZE_MISMATCH')
            while True:
                budget.check()
                if hasattr(self,'_budget'):self._budget.check()
                try:chunk=body.read(self.bounds.chunk_bytes)
                except BaseException:raise ReadFailure('RESTORE_STREAM_FAILED') from None
                if not chunk:break
                require(isinstance(chunk,bytes) and len(chunk)<=self.bounds.chunk_bytes,'RESTORE_RESPONSE_INVALID')
                count+=len(chunk);require(count<=r['ContentLength'],'RESTORE_SIZE_MISMATCH')
                h.update(chunk)
                if document:chunks.append(chunk)
            require(count==r['ContentLength'],'RESTORE_SIZE_MISMATCH')
            if digest is not None:require(h.hexdigest()==digest,'RESTORE_HASH_MISMATCH')
            return (parse_json(b''.join(chunks),Limits()),h.hexdigest()) if document else h.hexdigest()
        finally:body.close()

    def candidates(self, requested=None):
        prefix='backups/' if requested is None else 'backups/'+backup_id(requested)+'/'
        records=list_versions(self.reader,self.reader.bucket,prefix,self.bounds)
        groups={}
        for r in records:
            if not r['Key'].endswith('/completion.json'):continue
            match=re.fullmatch(r'backups/([^/]+)/completion\.json',r['Key'])
            require(match is not None,'RESTORE_COMPLETION_KEY_INVALID')
            bid=backup_id(match[1]);groups.setdefault(bid,[]).append(r)
        result=[]
        for bid,values in groups.items():
            latest=[v for v in values if v['IsLatest']]
            require(len(latest)==1,'AMBIGUOUS_CURRENT_VERSION')
            if not latest[0]['delete_marker']:result.append((bid,latest[0]['VersionId']))
        return result

    def marker(self,bid,completion_vid):
        marker,_=self.read('backups/'+bid+'/completion.json',completion_vid,document=True)
        self.catalog.validate('completion',marker)
        require(marker['schema_version']==2,'RESTORE_CONTRACT_UNSUPPORTED')
        require(marker['backup_id']==bid and marker['manifest_key']=='backups/'+bid+'/manifest.json',
                'RESTORE_SET_MISMATCH')
        return marker

    def load(self,bid,completion_vid):
        marker=self.marker(bid,completion_vid)
        manifest,manifest_hash=self.read(marker['manifest_key'],marker['manifest_version_id'],
                                         digest=marker['manifest_sha256'],document=True)
        self.catalog.validate('manifest',manifest)
        require(manifest['schema_version']==2 and manifest['source']==self.source,'RESTORE_SOURCE_MISMATCH')
        require(all(a['bucket']==self.reader.bucket for a in manifest['artifacts'].values()),
                'RESTORE_BUCKET_MISMATCH')
        a=manifest['artifacts']['object_inventory']
        inventory,_=self.read(a['key'],a['version_id'],size=a['size_bytes'],digest=a['sha256'],document=True)
        self.catalog.validate('inventory',inventory)
        bundle={'manifest':manifest,'inventory':inventory,'completion':marker}
        validate_semantics(bundle)
        return SelectedBackup(bid,completion_vid,manifest_hash,bundle)

    def verify(self,selected):
        manifest=selected.bundle['manifest']
        require(sum(a['size_bytes'] for a in manifest['artifacts'].values())<=self.bounds.total_artifact_bytes,
                'RESTORE_TOTAL_SIZE_LIMIT')
        for a in manifest['artifacts'].values():
            self.read(a['key'],a['version_id'],size=a['size_bytes'],digest=a['sha256'])
        verify_data_versions(self.data_reader,selected.bundle['inventory'])
        # Detect replacement/deletion of completion during selection, including
        # versioned overwrite. Pin the exact marker for the subsequent restore.
        require((selected.backup_id,selected.completion_version) in self.candidates(selected.backup_id),
                'RESTORE_SELECTION_CHANGED')

    def select(self,mode,requested=None):
        require(mode in ('specific','latest_valid'),'RESTORE_SELECTION_MODE_INVALID')
        require((mode=='specific' and requested is not None) or
                (mode=='latest_valid' and requested is None),'RESTORE_SELECTION_INPUT_INVALID')
        self._budget=Budget(self.bounds)
        candidates=self.candidates(requested);ordered=[];rejected=[]
        if mode=='specific':require(len(candidates)==1,'RESTORE_SPECIFIC_NOT_COMPLETE')
        for bid,vid in candidates:
            # Retain only ordering metadata, not every manifest/inventory. Each
            # bounded bundle is validated and discarded before trying the next.
            try:ordered.append((instant(self.marker(bid,vid)['completed_at']),bid,vid))
            except ReadFailure:raise
            except (SealFailure,ValidationFailure) as e:
                if mode=='specific':raise
                if e.code in ('TRANSFER_TIMEOUT','TRANSFER_CANCELLED'):raise
                rejected.append((bid,e.code))
        ordered.sort(reverse=True)
        for _,bid,vid in ordered:
            self._budget.check()
            try:
                selected=self.load(bid,vid)
                self.verify(selected)
            except ReadFailure:raise
            except (SealFailure,ValidationFailure) as e:
                if mode=='specific':raise
                # Dependency outages are ambiguous, not proof that an older set
                # is safer. Only deterministic document/byte failures can skip.
                if e.code in ('DATA_VERSION_UNAVAILABLE','INVENTORY_LIST_FAILED',
                              'RESTORE_SELECTION_CHANGED','TRANSFER_TIMEOUT','TRANSFER_CANCELLED'):raise
                rejected.append((bid,e.code));continue
            return SelectedBackup(selected.backup_id,selected.completion_version,
                                  selected.manifest_sha256,selected.bundle,tuple(rejected))
        raise SealFailure('RESTORE_NO_VALID_BACKUP')
