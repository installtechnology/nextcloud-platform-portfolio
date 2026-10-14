"""Synthetic PR8 fixtures. No AWS calls, restored data or functional evidence."""
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
from scripts.restore.selection import Selector, ReadFailure
from scripts.restore.plan import Destination,prepare
from scripts.restore.s3_reader import BackupReader
from scripts.backup.sealing.common import SealFailure

ROOT=Path(__file__).resolve().parents[3]
BID='20261009T120000Z-11111111-2222-4333-8444-555555555555'
NEW='20261010T120000Z-11111111-2222-4333-8444-555555555555'


def encoded(doc):return json.dumps(doc,sort_keys=True).encode()
def digest(raw):return hashlib.sha256(raw).hexdigest()


class MemoryReader:
    bucket='fixture-platform-backups'
    def __init__(self):self.bodies={};self.records=[];self.calls=[];self.broken=False
    def list_versions(self,bucket,prefix,cursor):
        if self.broken:raise RuntimeError('synthetic-private')
        return {'IsTruncated':False,'Versions':sorted([copy.deepcopy(r) for r in self.records if r['Key'].startswith(prefix)],key=lambda r:r['Key'])}
    def get(self,bucket,key,vid):
        self.calls.append((key,vid))
        if self.broken:raise RuntimeError('synthetic-private')
        body=self.bodies[(key,vid)]
        return {'VersionId':vid,'ServerSideEncryption':'AES256','ContentLength':len(body),'Body':io.BytesIO(body)}


class DataReader:
    def head(self,bucket,key,vid):return {'VersionId':vid,'ContentLength':256,'ETag':'fixture-etag-not-a-sha256'}


class Selection(unittest.TestCase):
    def setUp(self):
        self.reader=MemoryReader();self.manifest=self.add(BID);self.source=self.manifest['source']
        self.selector=Selector(self.reader,DataReader(),self.source)
    def add(self,bid):
        fixture=ROOT/'contracts/recovery/fixtures/sse-s3-v2'
        docs={k:json.loads((fixture/n).read_text().replace(BID,bid).replace('2026-10-09','2026-10-'+bid[6:8]))
              for k,n in (('manifest','complete-unproven.json'),('inventory','object-inventory.json'),('completion','completion.json'))}
        m=docs['manifest'];marker=docs['completion']
        for role,a in m['artifacts'].items():
            raw=encoded(docs['inventory']) if role=='object_inventory' else b'synthetic-'+role.encode()
            a['size_bytes']=len(raw);a['sha256']=digest(raw);self.reader.bodies[(a['key'],a['version_id'])]=raw
        raw=encoded(m);marker['manifest_sha256']=digest(raw)
        self.reader.bodies[(marker['manifest_key'],marker['manifest_version_id'])]=raw
        key='backups/'+bid+'/completion.json';vid='completion-'+bid
        self.reader.bodies[(key,vid)]=encoded(marker)
        self.reader.records.append({'Key':key,'VersionId':vid,'IsLatest':True,'Size':len(encoded(marker)),'ETag':'opaque'})
        return m
    def reject(self,code,mode='specific',bid=BID):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):self.selector.select(mode,bid)
    def test_specific_exact_versions(self):
        s=self.selector.select('specific',BID);self.assertEqual(s.backup_id,BID)
        self.assertFalse(s.bundle['manifest']['verification']['restore_proven'])
        self.assertIn((self.manifest['artifacts']['database']['key'],self.manifest['artifacts']['database']['version_id']),self.reader.calls)
    def test_explicit_latest_selects_newest_complete(self):
        self.add(NEW);self.assertEqual(self.selector.select('latest_valid').backup_id,NEW)
    def test_specific_never_falls_back(self):
        self.add(NEW);self.reader.records=[r for r in self.reader.records if NEW in r['Key']]
        self.reject('RESTORE_SPECIFIC_NOT_COMPLETE')
    def test_missing_completion(self):
        self.reader.records=[];self.reject('RESTORE_SPECIFIC_NOT_COMPLETE')
    def test_deleted_completion_not_eligible(self):
        self.reader.list_versions=lambda *args:{'IsTruncated':False,'DeleteMarkers':[{'Key':'backups/'+BID+'/completion.json','VersionId':'deleted','IsLatest':True}]}
        self.reject('RESTORE_SPECIFIC_NOT_COMPLETE')
    def test_ambiguous_current_completion(self):
        r=copy.deepcopy(self.reader.records[0]);r['VersionId']='another';self.reader.records.append(r)
        self.reject('AMBIGUOUS_CURRENT_VERSION')
    def test_malformed_listing(self):
        self.reader.list_versions=lambda *args:{'IsTruncated':True,'Versions':[]}
        self.reject('INCOMPLETE_LISTING')
    def test_hash_corruption(self):
        a=self.manifest['artifacts']['database'];self.reader.bodies[(a['key'],a['version_id'])]=b'x'*a['size_bytes']
        self.reject('RESTORE_HASH_MISMATCH')
    def test_truncated_artifact(self):
        a=self.manifest['artifacts']['database'];self.reader.bodies[(a['key'],a['version_id'])]=b'x'
        self.reject('RESTORE_SIZE_MISMATCH')
    def test_wrong_sse_rejected(self):
        old=self.reader.get
        def get(*args):r=old(*args);r['ServerSideEncryption']='aws:kms';return r
        self.reader.get=get;self.reject('RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
    def test_wrong_version_rejected(self):
        old=self.reader.get
        def get(*args):r=old(*args);r['VersionId']='other';return r
        self.reader.get=get;self.reject('RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
    def test_source_identity_mismatch(self):
        self.selector.source={**self.source,'installation_identity_sha256':'f'*64}
        self.reject('RESTORE_SOURCE_MISMATCH')
    def test_latest_records_invalid_candidates_explicitly(self):
        m=self.add(NEW);a=m['artifacts']['database'];self.reader.bodies[(a['key'],a['version_id'])]=b'x'*a['size_bytes']
        s=self.selector.select('latest_valid');self.assertEqual(s.backup_id,BID)
        self.assertIn((NEW,'RESTORE_HASH_MISMATCH'),s.rejected_candidates)
    def test_network_failure_never_falls_back(self):
        self.add(NEW);old=self.reader.get
        def get(bucket,key,vid):
            if NEW in key:raise RuntimeError('synthetic-secret')
            return old(bucket,key,vid)
        self.reader.get=get
        with self.assertRaisesRegex(ReadFailure,'^RESTORE_READ_FAILED$'):self.selector.select('latest_valid')
    def test_stream_failure_sanitized(self):
        class Bad(io.BytesIO):
            def read(self,*args):raise RuntimeError('synthetic-secret')
        old=self.reader.get
        def get(*args):r=old(*args);r['Body']=Bad();return r
        self.reader.get=get
        with self.assertRaisesRegex(ReadFailure,'^RESTORE_STREAM_FAILED$'):self.selector.select('specific',BID)
    def test_latest_requires_no_explicit_id(self):self.reject('RESTORE_SELECTION_INPUT_INVALID','latest_valid')
    def test_specific_requires_id(self):self.reject('RESTORE_SELECTION_INPUT_INVALID','specific',None)
    def test_unknown_mode(self):self.reject('RESTORE_SELECTION_MODE_INVALID','latest')
    def test_read_only_actions(self):
        self.assertEqual(BackupReader.actions,{'list_object_versions','head_object','get_object'})
    def test_completion_replaced_during_selection_aborts(self):
        old=self.selector.candidates
        calls=[0]
        def candidates(requested=None):
            calls[0]+=1
            return old(requested) if calls[0]==1 else []
        self.selector.candidates=candidates
        self.reject('RESTORE_SELECTION_CHANGED')
    def test_data_unavailable_does_not_choose_older(self):
        self.add(NEW)
        def unavailable(*args):raise RuntimeError('synthetic-private')
        self.selector.data_reader.head=unavailable
        self.reject('DATA_VERSION_UNAVAILABLE','latest_valid',None)
    def test_response_body_closed_on_rejection(self):
        old=self.reader.get;streams=[]
        def get(*args):
            r=old(*args);streams.append(r['Body']);r['VersionId']='wrong';return r
        self.reader.get=get;self.reject('RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
        self.assertTrue(all(s.closed for s in streams))
    def test_oversized_stream_response_rejected(self):
        old=self.reader.get
        def get(*args):r=old(*args);r['ContentLength']=9*1024**3;return r
        self.reader.get=get;self.reject('RESTORE_VERSION_OR_ENCRYPTION_MISMATCH')
    def test_destination_source_instance_rejected(self):
        s=self.selector.select('specific',BID)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ORIGIN_TARGET_REJECTED$'):
            prepare(s,Destination(self.source['account_id'],'us-east-1','i-source','fixture-restore','/var/lib/restore'),'i-source','fixture-state')
    def test_destination_original_bucket_rejected(self):
        s=self.selector.select('specific',BID)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ORIGIN_TARGET_REJECTED$'):
            prepare(s,Destination(self.source['account_id'],'us-east-1','i-target',self.source['data_bucket'],'/var/lib/restore'),'i-source','fixture-state')
    def test_isolated_plan_does_not_grant_evidence(self):
        s=self.selector.select('specific',BID)
        p=prepare(s,Destination(self.source['account_id'],'us-east-1','i-target','fixture-restore','/var/lib/restore'),'i-source','fixture-state')
        self.assertFalse(p['operational_execution_available']);self.assertFalse(p['restore_proven']);self.assertFalse(p['destroy_authorized'])
        self.assertEqual(p['must_remain_absent'],s.bundle['inventory']['absent_current_keys'])


if __name__=='__main__':unittest.main()
