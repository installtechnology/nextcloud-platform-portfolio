"""Manifest + completion producer. Pure simulator constructors remain closed."""
import hashlib
import json
from ..capture.storage import Store
from ..capture.engine import utc
from ..safe_files import SafeRoot,signature
from ..schemas import SchemaCatalog
from ..semantics import validate_semantics
from ..sealing.common import Bounds,Budget,json_bytes,require,sha,version
from ..sealing.completion import FILES
from ..sealing.inventory import list_versions,verify_data_versions
from .receipt import live_receipt
from .s3 import BackupS3,DataInventory
from .transport import ArtifactTransport


ARTIFACT_NAMES = {**{role: spec[0] for role, spec in FILES.items()}, 'object_inventory': 'object_inventory.json'}


class CompletionWriter:
    def __init__(self,client,bucket,account,bid,region):
        # A concrete SDK factory, not a simulator/injected ready boolean, is
        # required on the only method that can create completion.json.
        from botocore.client import BaseClient
        require(isinstance(client,BaseClient) and client.meta.service_model.service_name=='s3', 'LIVE_SDK_REQUIRED')
        self.adapter=BackupS3(client,bucket,account,region,bid)
        self.client,self.bucket,self.account,self.key=client,bucket,account,f'backups/{bid}/completion.json'

    def publish(self,documents,transport):
        require(type(transport) is ArtifactTransport and transport.state is not None
                and transport.state.get('backup_complete') is False
                and transport.state.get('status')=='incomplete' and not transport.state.get('blocked'), 'LIVE_TRANSPORT_REQUIRED')
        catalog=SchemaCatalog()
        for kind in ('manifest','inventory','completion'):catalog.validate(kind,documents[kind])
        validate_semantics(documents)
        require(documents['manifest']['schema_version']==2, 'OPERATIONAL_CONTRACT_V2_REQUIRED')
        expected={spec[0] for spec in FILES.values()}|{'object_inventory.json','manifest.json'}
        require(set(transport.state['objects'])==expected
                and all(r.get('phase')=='verified' for r in transport.state['objects'].values()), 'COMPLETION_ARTIFACT_SET_MISMATCH')
        manifest_record=transport.state['objects']['manifest.json']
        require(documents['completion']['manifest_version_id']==manifest_record['version_id']
                and documents['completion']['manifest_sha256']==manifest_record['sha256'],
                'COMPLETION_MANIFEST_BINDING_INVALID')
        for role,descriptor in documents['manifest']['artifacts'].items():
            record=transport.state['objects'].get(ARTIFACT_NAMES[role],{})
            require(descriptor['bucket']==self.bucket and descriptor['key']==transport.prefix+ARTIFACT_NAMES[role]
                    and all(descriptor[k]==record.get(k) for k in ('version_id','size_bytes','sha256')),
                    'COMPLETION_ARTIFACT_BINDING_INVALID')
        require(documents['manifest']['verification']['restore_proven'] is False, 'RESTORE_NOT_PROVEN')
        require(not list_versions(transport.api,self.bucket,self.key), 'HISTORICAL_PREFIX_COLLISION')
        for name,record in transport.state['objects'].items():transport.verify(name,record)
        data=json_bytes(documents['completion'])
        # Record intent before the last write. Lost ACK is never retried blindly.
        transport.state['completion']={'phase':'put_intent','size_bytes':len(data),'sha256':sha(data)};transport.save()
        try:
            response=self.client.put_object(Bucket=self.bucket,Key=self.key,Body=data,
                ExpectedBucketOwner=self.account,IfNoneMatch='*',ServerSideEncryption='AES256')
            vid=version(response.get('VersionId'))
            transport.state['completion'].update(phase='verify_intent',version_id=vid);transport.save()
            downloaded=self.client.get_object(Bucket=self.bucket,Key=self.key,VersionId=vid,ExpectedBucketOwner=self.account)
            body=downloaded['Body']
            try:
                raw=body.read(len(data)+1)
                require(downloaded.get('VersionId')==vid and raw==data and downloaded.get('ContentLength')==len(data)
                        and downloaded.get('ServerSideEncryption')=='AES256',
                        'COMPLETION_BYTES_MISMATCH')
            finally:body.close()
            records=list_versions(transport.api,self.bucket,self.key)
            require(len(records)==1 and records[0]['VersionId']==vid and not records[0]['delete_marker'], 'COMPLETION_HISTORY_COLLISION')
            transport.state['completion']['phase']='verified';transport.save()
            return vid
        except BaseException:
            from ..sealing.common import SealFailure
            raise SealFailure('COMPLETION_OUTCOME_REQUIRES_RECONCILIATION') from None


class OperationalSeal:
    def __init__(self,store,data,backups,completion,source,compatibility,scope,bounds=Bounds()):
        require(type(store) is Store and store.synthetic is False
                and type(data) is DataInventory and type(backups) is BackupS3
                and type(completion) is CompletionWriter, 'LIVE_SEAL_ADAPTERS_REQUIRED')
        self.store,self.data,self.backups,self.completion=store,data,backups,completion
        self.source,self.compatibility,self.scope=source,compatibility,scope
        self.bounds=bounds
        require(data.bucket==source['data_bucket'] and data.bucket!=backups.bucket, 'STORAGE_SEPARATION')

    def local(self,bid,name,data):
        return self.store.artifact(self.store.operation(bid),name,lambda out:out.write(data),8*1024**2)

    def seal(self,bid):
        with self.store.lock():
            journal,snapshot,inventory_hash=live_receipt(self.store,bid,self.source)
            inventory=snapshot['document'];markers=snapshot['delete_markers']
            require(all(journal['original'][key]==self.compatibility[other] for key,other in
                        (('database','database_name'),('owner','database_owner'),('table_prefix','table_prefix'))), 'CAPTURE_DATABASE_MISMATCH')
            verify_data_versions(self.data,inventory,markers)
            require(set(journal['artifacts'])==set(FILES), 'CAPTURE_ARTIFACTS_INCOMPLETE')
            transport=ArtifactTransport(self.backups,self.store,bid,self.backups.bucket,self.bounds)
            transport.begin();self.local(bid,'object_inventory.json',json_bytes(inventory))
            roles={**FILES,'object_inventory':('object_inventory.json','json')};artifacts={};budget=Budget(self.bounds,lambda:False)
            for role,(name,fmt) in roles.items():
                with SafeRoot(self.store.operation(bid)) as safe,safe.open(name) as stream:
                    before=signature(stream);digest=hashlib.sha256()
                    require(0<before[2]<=self.bounds.artifact_bytes, 'PLAINTEXT_LIMIT')
                    while chunk:=stream.read(self.bounds.chunk_bytes):budget.check();digest.update(chunk)
                    require(before==signature(stream), 'PLAINTEXT_CHANGED');safe.assert_unchanged()
                plain=digest.hexdigest()
                if role in FILES:require(journal['artifacts'][role]=={'size_bytes':before[2],'plaintext_sha256':plain}, 'CAPTURE_ARTIFACT_CHANGED')
                artifact={'size_bytes':before[2], 'sha256':plain}
                require(sum(a['size_bytes'] for a in artifacts.values())+artifact['size_bytes']<=self.bounds.total_artifact_bytes,'TOTAL_ARTIFACT_LIMIT')
                uploaded=transport.upload(ARTIFACT_NAMES[role],bid+'/'+ARTIFACT_NAMES[role],artifact)
                artifacts[role]={'kind':role,'format':fmt,'backup_id':bid,'consistency_id':journal['consistency_id'],
                    'bucket':self.backups.bucket,'key':self.backups.prefix+ARTIFACT_NAMES[role],'version_id':uploaded['version_id'],
                    **artifact,'encryption':{'method':'SSE-S3','algorithm':'AES256'}}
            manifest={'schema_version':2,'kind':'recovery-manifest','backup_id':bid,'status':'complete_integrity_verified',
                'created_at':utc(),'mode':'version_referenced_data_bucket','source':self.source,
                'dependencies':{'data_bucket_and_versions_required':True,'protects_against_data_bucket_loss':False,
                    'secret_recovery_required':True,'private_decryption_key_required':False},'compatibility':self.compatibility,
                'consistency':{'id':journal['consistency_id'],'quiesced_at':journal['quiesced_at'],
                    'capture_finished_at':journal['capture_finished_at'],'application_writers_quiesced':True,
                    'background_writers_quiesced':True,'inflight_requests_drained':True,
                    'inventory_before_sha256':inventory_hash,'inventory_after_sha256':inventory_hash,
                    'maintenance_initially_enabled':journal['original']['maintenance'],'original_service_state_restored':True},
                'scope':self.scope,'artifacts':artifacts,'verification':{'dump_catalog_readable':True,
                    'artifact_checksums_verified_after_download':True,'all_artifact_versions_available':True,
                    'all_referenced_data_versions_available_at_verification':True,'restore_proven':False}}
            SchemaCatalog().validate('manifest',manifest)
            plain=self.local(bid,'manifest.json',json_bytes(manifest))
            artifact={'size_bytes':plain['size_bytes'], 'sha256':plain['plaintext_sha256']}
            require(sum(a['size_bytes'] for a in artifacts.values())+artifact['size_bytes']<=self.bounds.total_artifact_bytes,
                    'TOTAL_ARTIFACT_LIMIT')
            uploaded=transport.upload('manifest.json',bid+'/manifest.json',artifact)
            marker={'schema_version':2,'backup_id':bid,'manifest_key':self.backups.prefix+'manifest.json',
                'manifest_version_id':uploaded['version_id'],'manifest_sha256':artifact['sha256'], 'completed_at':utc()}
            latest,_,_=live_receipt(self.store,bid,self.source)
            require(json_bytes(latest)==json_bytes(journal),'CAPTURE_RECEIPT_CHANGED')
            verify_data_versions(self.data,inventory,markers)
            vid=self.completion.publish({'manifest':manifest,'inventory':inventory,'completion':marker},transport)
            transport.state.update(status='complete_integrity_verified',backup_complete=True);transport.save()
            return {'code':'BACKUP_INTEGRITY_VERIFIED','backup_id':bid,'completion_version_id':vid,
                    'backup_complete':True,'restore_proven':False,'destroy_authorized':False}
