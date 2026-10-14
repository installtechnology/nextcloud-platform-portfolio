"""Small synthetic TAR/SQL/S3 inputs only; no services, AWS or real backup."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace

from scripts.restore.archives import inspect_archive,extract_private
from scripts.restore.parameters import load_parameters,RUNTIME,SECRETS
from scripts.restore.objects import copy_selected
from scripts.restore.database import restore_database
from scripts.backup.sealing.common import SealFailure
from scripts.restore.tests.test_selection import ROOT


def archive(entries):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as tar:
        for name,kind,value in entries:
            item=tarfile.TarInfo(name);item.mode=0o600;item.uid=item.gid=33
            if kind=='file':item.size=len(value);tar.addfile(item,io.BytesIO(value))
            else:
                item.type={'dir':tarfile.DIRTYPE,'link':tarfile.SYMTYPE,'hard':tarfile.LNKTYPE,'fifo':tarfile.FIFOTYPE}[kind]
                if kind in ('link','hard'):item.linkname=value
                tar.addfile(item)
    output.seek(0);return output


class Archives(unittest.TestCase):
    def reject(self,entries,code):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):inspect_archive(archive(entries))
    def test_valid_links_and_metadata(self):
        members=inspect_archive(archive([('.', 'dir',None),('js','dir',None),('js/code','file',b'x'),('js/license','link','code')]))
        self.assertEqual(members['js/code'].uid,33);self.assertEqual(members['js/license'].linkname,'code')
    def test_valid_same_directory_chain(self):
        self.assertEqual(len(inspect_archive(archive([('a','link','b'),('b','link','c'),('c','file',b'x')]))),3)
    def test_traversal(self):self.reject([('../config.php','file',b'x')],'RESTORE_ARCHIVE_PATH_REJECTED')
    def test_absolute(self):self.reject([('/etc/config','file',b'x')],'RESTORE_ARCHIVE_PATH_REJECTED')
    def test_windows_drive(self):self.reject([('C:/config','file',b'x')],'RESTORE_ARCHIVE_PATH_REJECTED')
    def test_backslash(self):self.reject([('a\\config','file',b'x')],'RESTORE_ARCHIVE_PATH_REJECTED')
    def test_duplicate(self):self.reject([('config','file',b'x'),('./config','file',b'x')],'RESTORE_ARCHIVE_DUPLICATE')
    def test_fifo(self):self.reject([('pipe','fifo',None)],'RESTORE_ARCHIVE_TYPE_REJECTED')
    def test_large_pax_metadata_rejected(self):
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w',format=tarfile.PAX_FORMAT) as tar:
            member=tarfile.TarInfo('a');member.pax_headers={'comment':'x'*65537};tar.addfile(member)
        raw.seek(0)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_METADATA_LIMIT$'):inspect_archive(raw)
    def test_setuid_rejected(self):
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as tar:
            member=tarfile.TarInfo('a');member.mode=0o4755;tar.addfile(member)
        raw.seek(0)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_PERMISSIONS_REJECTED$'):inspect_archive(raw)
    def permission_archive(self,mode,kind=tarfile.DIRTYPE):
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as tar:
            item=tarfile.TarInfo('tmp');item.type=kind;item.mode=mode;tar.addfile(item)
        raw.seek(0);return raw
    def test_sticky_directory_metadata_accepted(self):
        members=inspect_archive(self.permission_archive(0o1777))
        self.assertTrue(members['tmp'].isdir());self.assertEqual(members['tmp'].mode,0o1777)
    def test_sticky_directory_extracted_as_private(self):
        data=self.permission_archive(0o1777).getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'data.tar';source.write_bytes(data);destination=Path(tmp)/'new'
            with source.open('rb') as stream:extract_private(stream,destination,hashlib.sha256(data).hexdigest())
            self.assertTrue((destination/'tmp').is_dir())
            if __import__('os').name=='posix':
                self.assertEqual((destination/'tmp').stat().st_mode & 0o7777,0o700)
    def test_setgid_directory_and_combined_sticky_rejected(self):
        for mode in (0o2777,0o3777,0o4777,0o5777,0o7777):
            with self.subTest(mode=mode),self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_PERMISSIONS_REJECTED$'):
                inspect_archive(self.permission_archive(mode))
    def test_sticky_non_directory_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_PERMISSIONS_REJECTED$'):
            inspect_archive(self.permission_archive(0o1600,tarfile.REGTYPE))
    def test_hardlink(self):self.reject([('a','hard','b')],'RESTORE_ARCHIVE_TYPE_REJECTED')
    def test_link_escape(self):self.reject([('a','link','../b')],'RESTORE_ARCHIVE_LINK_REJECTED')
    def test_dangling_link(self):self.reject([('a','link','b')],'RESTORE_ARCHIVE_LINK_REJECTED')
    def test_directory_link(self):self.reject([('a','link','b'),('b','dir',None)],'RESTORE_ARCHIVE_LINK_REJECTED')
    def test_link_cycle(self):self.reject([('a','link','b'),('b','link','a')],'RESTORE_ARCHIVE_LINK_REJECTED')
    def test_parent_link(self):self.reject([('a','link','b'),('b','file',b'x'),('a/child','file',b'x')],'RESTORE_ARCHIVE_PARENT_REJECTED')
    def test_private_extract(self):
        data=archive([('config','dir',None),('config/config.php','file',b'synthetic-php')]).getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'configuration.tar';source.write_bytes(data)
            destination=Path(tmp)/'new'
            with source.open('rb') as stream:report=extract_private(stream,destination,hashlib.sha256(data).hexdigest())
            self.assertEqual((destination/'config/config.php').read_bytes(),b'synthetic-php')
            self.assertFalse(report['ownership_applied']);self.assertFalse(report['application_started'])
    def test_wrong_hash_before_creating_destination(self):
        data=archive([('a','file',b'x')]).getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data.tar';p.write_bytes(data);dest=Path(tmp)/'new'
            with p.open('rb') as stream,self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_HASH_MISMATCH$'):
                extract_private(stream,dest,'0'*64)
            self.assertFalse(dest.exists())
    def test_existing_destination_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_DESTINATION_NOT_NEW$'):
                extract_private(io.BytesIO(),Path(tmp),'0'*64)


class Parameters(unittest.TestCase):
    def setUp(self):
        self.manifest=json.loads((ROOT/'contracts/recovery/fixtures/sse-s3-v2/complete-unproven.json').read_text())
        c=self.manifest['compatibility'];b64=lambda b:base64.b64encode(b).decode()
        self.p={'internal_version':1,'kind':'local-recovery-parameters','database':{'database':c['database_name'],'owner':c['database_owner'],'extensions':[]},
                'globals_sql_without_passwords':'-- synthetic SQL; not executed',
                'images':{'nextcloud':c['nextcloud_image'],'postgres':c['postgres_image']},
                'capture_binding':{'operation_id':self.manifest['backup_id'],'consistency_id':self.manifest['consistency']['id']},
                'secrets_base64':{k:b64(b'synthetic-private') for k in SECRETS},
                'compose_env_base64':b64(b'SYNTHETIC=1'),
                'runtime_code_base64':{k:b64(b'synthetic') for k in RUNTIME},
                'recovery_requirement':'synthetic','restore_proven':False}
    def load(self):return load_parameters(json.dumps(self.p).encode(),self.manifest)
    def test_valid_protected_repr(self):
        p=self.load();self.assertNotIn('synthetic-private',repr(p));self.assertNotIn('synthetic SQL',repr(p))
    def test_large_code_is_not_a_journal(self):
        self.p['runtime_code_base64']['storage-guard.php']=base64.b64encode(b'x'*20000).decode()
        self.assertEqual(len(self.load().runtime_code['storage-guard.php']),20000)
    def reject(self,code):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):self.load()
    def test_missing_secret(self):
        self.p['secrets_base64'].pop('redis_password');self.reject('RESTORE_RECOVERY_INPUTS_INCOMPLETE')
    def test_extra_secret(self):
        self.p['secrets_base64']['extra']='eA==';self.reject('RESTORE_RECOVERY_INPUTS_INCOMPLETE')
    def test_invalid_encoding(self):
        self.p['compose_env_base64']='!';self.reject('RESTORE_PARAMETER_ENCODING')
    def test_owner_mismatch(self):
        self.p['database']['owner']='other';self.reject('RESTORE_DATABASE_PARAMETERS_MISMATCH')
    def test_mixed_capture(self):
        self.p['capture_binding']['operation_id']='other';self.reject('RESTORE_CAPTURE_BINDING_MISMATCH')
    def test_mixed_image(self):
        self.p['images']['postgres']='other';self.reject('RESTORE_IMAGE_MISMATCH')
    def test_no_restore_proof_accepted(self):
        self.p['restore_proven']=True;self.reject('RESTORE_PARAMETERS_INVALID')


class Objects(unittest.TestCase):
    def setUp(self):
        self.inventory=json.loads((ROOT/'contracts/recovery/fixtures/sse-s3-v2/object-inventory.json').read_text())
        self.inventory['objects'][0]['size_bytes']=1
        self.source=MemoryObjects(self.inventory['source']['data_bucket']);self.source.entries[('urn:oid:fixture-1','fixture-data-version-1')]=b'x'
        self.dest=MemoryObjects('fixture-restore');self.dest.source=self.source
    def copy(self):return copy_selected(self.source,self.dest,self.inventory,'fixture-backups','fixture-state')
    def test_exact_version_and_hash(self):
        r=self.copy();self.assertEqual(r['objects'][0]['source_version_id'],'fixture-data-version-1')
        self.assertEqual(r['objects'][0]['sha256'],hashlib.sha256(b'x').hexdigest());self.assertFalse(r['restore_proven'])
    def test_nonempty_dest_rejected(self):
        self.dest.entries[('existing','old')]=b'x'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_DESTINATION_NOT_EMPTY$'):self.copy()
    def populate(self):self.dest.entries[('urn:oid:fixture-1','preserved-destination-version')]=b'x'
    def test_complete_retained_set_reuses_exact_versions_without_copy(self):
        self.populate();r=self.copy()
        self.assertEqual(self.dest.calls,0)
        self.assertEqual(r['objects'][0]['destination_version_id'],'preserved-destination-version')
        self.assertFalse(r['restore_proven']);self.assertFalse(r['destroy_authorized'])
    def test_retained_corruption_never_overwritten(self):
        self.populate();self.dest.entries[('urn:oid:fixture-1','preserved-destination-version')]=b'z'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_HASH_MISMATCH$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_retained_extra_key_is_preserved_without_copy(self):
        self.populate();self.dest.entries[('foreign','old')]=b'x'
        before=self.dest.entries.copy();result=self.copy()
        self.assertEqual(self.dest.entries,before)
        self.assertEqual(len(result['objects']),self.inventory['object_count'])
        self.assertEqual(self.dest.calls,0)
    def test_known_absent_key_cannot_be_present_among_extras(self):
        self.populate();self.inventory['absent_current_keys']=['known-deleted']
        self.dest.entries[('known-deleted','old')]=b'x'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RETAINED_CURRENT_SET_MISMATCH$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_retained_missing_current_version_blocks(self):
        self.populate();original=self.dest.list_versions
        def listing(*args):
            result=original(*args);result['Versions'][0]['IsLatest']=False
            result['DeleteMarkers']=[{'Key':'urn:oid:fixture-1','VersionId':'deleted-current','IsLatest':True}]
            return result
        self.dest.list_versions=listing
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RETAINED_CURRENT_SET_MISMATCH$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_retained_ambiguous_latest_blocks(self):
        self.populate();self.dest.entries[('urn:oid:fixture-1','another-version')]=b'x'
        with self.assertRaisesRegex(SealFailure,'^AMBIGUOUS_CURRENT_VERSION$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_retained_history_preserved(self):
        self.populate();self.dest.entries[('urn:oid:fixture-1','old-version')]=b'old'
        original=self.dest.list_versions
        def listing(*args):
            result=original(*args)
            for row in result['Versions']:row['IsLatest']=row['VersionId']=='preserved-destination-version'
            return result
        self.dest.list_versions=listing;r=self.copy()
        self.assertEqual(self.dest.calls,0);self.assertEqual(len(self.dest.entries),2)
        self.assertEqual(r['objects'][0]['destination_version_id'],'preserved-destination-version')
    def test_retained_listing_change_blocks(self):
        self.populate();original=self.dest.list_versions;calls=[0]
        def listing(*args):
            calls[0]+=1;result=original(*args)
            if calls[0]>1:result['Versions'][0]['VersionId']='changed-version'
            return result
        self.dest.list_versions=listing
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_DESTINATION_CHANGED$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_retained_non_sse_s3_object_blocks(self):
        self.populate();original=self.dest.head
        self.dest.head=lambda *args:{**original(*args),'ServerSideEncryption':'aws:kms'}
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_ENCRYPTION_MISMATCH$'):self.copy()
        self.assertEqual(self.dest.calls,0)
    def test_observed_bucket_cannot_switch_empty_or_exact_versions(self):
        from scripts.restore.destination import Destination
        for initially_populated in (False,True):
            with self.subTest(initially_populated=initially_populated):
                self.setUp()
                if initially_populated:self.populate()
                target=Destination.__new__(Destination)
                target.runtime=SimpleNamespace(host=SimpleNamespace(config={
                    'backups_bucket':'fixture-backups','state_bucket':'fixture-state'}))
                target.source=self.source;target.objects=self.dest
                target.selected=SimpleNamespace(bundle={'inventory':self.inventory})
                target.objects_observed=False;target.object_selection=None
                target.assert_selected_objects()
                self.dest.entries={('urn:oid:fixture-1','new-current-version'):b'x'}
                with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_DESTINATION_CHANGED$'):
                    target.assert_selected_objects()
                self.assertEqual(self.dest.calls,0)
    def test_source_dest_rejected(self):
        self.dest.bucket=self.source.bucket
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ORIGIN_TARGET_REJECTED$'):self.copy()
    def test_corruption_preserves_copy_without_success(self):
        self.dest.corrupt=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_HASH_MISMATCH$'):self.copy()
        self.assertTrue(self.dest.entries)
    def test_ambiguous_copy_no_retry(self):
        def lost(*args):self.dest.calls+=1;raise RuntimeError('synthetic-private')
        self.dest.copy_version=lost
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECT_COPY_AMBIGUOUS$'):self.copy()
        self.assertEqual(self.dest.calls,1)
    def test_absent_keys_remain_absent(self):
        self.copy();self.assertNotIn('urn:oid:fixture-deleted',{k for k,v in self.dest.entries})
    def test_large_copy_rejected_before_write(self):
        self.inventory['objects'][0]['size_bytes']=5000000001
        self.source.head=lambda bucket,key,vid:{'VersionId':vid,'ContentLength':5000000001,'ETag':'fixture-etag-not-a-sha256'}
        with self.assertRaisesRegex(SealFailure,'^RESTORE_MULTIPART_COPY_REQUIRED$'):self.copy()
        self.assertEqual(self.dest.calls,0)


class MemoryObjects:
    def __init__(self,bucket):self.bucket=bucket;self.entries={};self.source=None;self.corrupt=False;self.calls=0
    def list_versions(self,bucket,prefix,cursor):
        return {'IsTruncated':False,'Versions':[{'Key':k,'VersionId':v,'IsLatest':True,'Size':len(b),'ETag':'fixture-etag-not-a-sha256'} for (k,v),b in sorted(self.entries.items())]}
    def head(self,bucket,key,vid):return {'VersionId':vid,'ContentLength':len(self.entries[(key,vid)]),
        'ETag':'fixture-etag-not-a-sha256','ServerSideEncryption':'AES256'}
    def get(self,bucket,key,vid):
        raw=self.entries[(key,vid)];return {'VersionId':vid,'ContentLength':len(raw),'Body':io.BytesIO(raw)}
    def copy_version(self,src,key,vid):
        self.calls+=1;raw=self.source.entries[(key,vid)];self.entries[(key,'destination-version')]=b'z' if self.corrupt else raw
        return {'VersionId':'destination-version','ServerSideEncryption':'AES256'}


class Database(unittest.TestCase):
    def setUp(self):
        from scripts.restore.parameters import ProtectedParameters
        self.p=ProtectedParameters({'database':'nextcloud','owner':'nextcloud','extensions':[]},'',{},b'',{})
        self.adapter=DatabaseAdapter();self.dump=io.BytesIO(b'PGDMPsynthetic')
    def restore(self):return restore_database(self.adapter,self.p,self.dump)
    def test_single_transaction_no_clean_and_no_secret_args(self):
        r=self.restore();self.assertEqual(self.adapter.argv,['pg_restore','--exit-on-error','--single-transaction','--dbname','nextcloud'])
        self.assertFalse(r['restore_proven'])
    def test_nonempty_database_rejected(self):
        self.adapter.initial=['oc_filecache']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_POSTGRES_NOT_EMPTY_OR_IDLE$'):self.restore()
        self.assertIsNone(self.adapter.argv)
    def test_wrong_version(self):
        self.adapter.major=16
        with self.assertRaisesRegex(SealFailure,'^RESTORE_POSTGRES_TARGET_MISMATCH$'):self.restore()
    def test_roles_not_ready_rejected(self):
        self.adapter.roles=False
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_NOT_READY$'):self.restore()
    def test_dump_not_custom(self):
        self.dump=io.BytesIO(b'not SQL dump')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_DUMP_FORMAT_INVALID$'):self.restore()
    def test_failed_restore_no_retry_or_cleanup(self):
        self.adapter.fail=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_POSTGRES_FAILED_PRESERVE_DESTINATION$'):self.restore()
        self.assertEqual(self.adapter.executions,1)


class DatabaseAdapter:
    def __init__(self):self.major=17;self.initial=[];self.roles=True;self.fail=False;self.argv=None;self.executions=0
    def inspect_database(self,name):
        return {'server_major':self.major,'database':name,'owner':'nextcloud','tables':['oc_filecache'] if self.argv else self.initial,'sessions':[],'extensions':[]}
    def assert_roles(self,parameters):
        if not self.roles:raise SealFailure('RESTORE_ROLES_NOT_READY')
    def check_catalog(self,dump):pass
    def hold_writers(self):pass
    def release_writers_after_verified_restore(self):pass
    def execute(self,args,dump):
        self.argv=args;self.executions+=1
        if self.fail:raise RuntimeError('synthetic-private')


if __name__=='__main__':unittest.main()
