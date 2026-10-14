"""Publication algorithm only, using synthetic records; never an SDK request.

__new__ deliberately isolates control flow from the concrete SDK constructor.
These tests cannot provide a live receipt or evidence of an operational backup.
"""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from scripts.backup.operational.completion import CompletionWriter
from scripts.backup.operational.transport import ArtifactTransport
from scripts.backup.sealing.common import SealFailure

ROOT=Path(__file__).resolve().parents[4]


class Publication(unittest.TestCase):
    def setUp(self):
        fixture=ROOT/'contracts/recovery/fixtures/sse-s3-v2'
        self.docs={kind:json.loads((fixture/name).read_text()) for kind,name in
                   (('manifest','complete-unproven.json'),('inventory','object-inventory.json'),('completion','completion.json'))}
        self.transport=object.__new__(ArtifactTransport)
        self.transport.prefix='backups/'+self.docs['manifest']['backup_id']+'/'
        objects={a['key'].rsplit('/',1)[1]:{k:a[k] for k in ('version_id','size_bytes','sha256')}
                 for role,a in self.docs['manifest']['artifacts'].items()}
        objects['manifest.json']={'version_id':self.docs['completion']['manifest_version_id'],
            'sha256':self.docs['completion']['manifest_sha256'],'size_bytes':100}
        for record in objects.values():record['phase']='verified'
        self.transport.state={'backup_complete':False,'objects':objects,'status':'incomplete'}
        self.transport.api=Mock();self.transport.save=Mock();self.trace=[]
        self.transport.verify=lambda name,record:self.trace.append(('verify',name))
        self.writer=object.__new__(CompletionWriter)
        self.writer.bucket='fixture-platform-backups';self.writer.account='123456789012'
        self.writer.key=self.transport.prefix+'completion.json';self.writer.client=Mock()
        self.writer.client.put_object.side_effect=self.put
        self.writer.client.get_object.side_effect=self.get
        self.histories=[[],[{'VersionId':'synthetic-completion','delete_marker':False}]]
    def put(self,**kwargs):
        self.trace.append(('put',kwargs['Key']));self.body=kwargs['Body'];return {'VersionId':'synthetic-completion'}
    def get(self,**kwargs):return {'VersionId':'synthetic-completion','ContentLength':len(self.body),'Body':io.BytesIO(self.body),'ServerSideEncryption':'AES256'}
    def publish(self):
        with patch('scripts.backup.operational.completion.list_versions',side_effect=self.histories):
            return self.writer.publish(self.docs,self.transport)
    def reject(self,code):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):self.publish()
        self.writer.client.put_object.assert_not_called()
    def test_completion_after_six_exact_byte_verifications(self):
        self.assertEqual(self.publish(),'synthetic-completion')
        self.assertEqual([x[0] for x in self.trace],['verify']*6+['put'])
        self.assertEqual(self.transport.state['completion']['phase'],'verified')
        self.assertFalse(self.transport.state['backup_complete'])
        self.assertFalse(self.docs['manifest']['verification']['restore_proven'])
    def test_missing_artifact(self):
        self.transport.state['objects'].pop('database.dump');self.reject('COMPLETION_ARTIFACT_SET_MISMATCH')
    def test_unverified_artifact(self):
        self.transport.state['objects']['database.dump']['phase']='uploaded';self.reject('COMPLETION_ARTIFACT_SET_MISMATCH')
    def test_blocked_transfer(self):
        self.transport.state['blocked']=True;self.reject('LIVE_TRANSPORT_REQUIRED')
    def test_marker_without_sse_requires_reconciliation(self):
        self.writer.client.get_object.side_effect=lambda **kw:{'VersionId':'synthetic-completion','ContentLength':len(self.body),'Body':io.BytesIO(self.body)}
        with self.assertRaisesRegex(SealFailure,'^COMPLETION_OUTCOME_REQUIRES_RECONCILIATION$'):self.publish()
        self.assertFalse(self.transport.state['backup_complete'])
    def test_extra_artifact(self):
        self.transport.state['objects']['extra.age']={};self.reject('COMPLETION_ARTIFACT_SET_MISMATCH')
    def test_mixed_manifest_version(self):
        self.transport.state['objects']['manifest.json']['version_id']='other';self.reject('COMPLETION_MANIFEST_BINDING_INVALID')
    def test_mixed_manifest_digest(self):
        self.transport.state['objects']['manifest.json']['sha256']='0'*64;self.reject('COMPLETION_MANIFEST_BINDING_INVALID')
    def test_mixed_artifact_version(self):
        self.transport.state['objects']['database.dump']['version_id']='other';self.reject('COMPLETION_ARTIFACT_BINDING_INVALID')
    def test_historical_collision(self):
        self.histories=[[{'VersionId':'old','delete_marker':True}]];self.reject('HISTORICAL_PREFIX_COLLISION')
    def test_corrupted_remote_bytes_prevent_marker(self):
        def reject(*args):raise SealFailure('REMOTE_CONTENT_MISMATCH')
        self.transport.verify=reject;self.reject('REMOTE_CONTENT_MISMATCH')
    def test_lost_ack_is_not_retried(self):
        self.writer.client.put_object.side_effect=RuntimeError('synthetic-sensitive')
        with self.assertRaisesRegex(SealFailure,'^COMPLETION_OUTCOME_REQUIRES_RECONCILIATION$'):self.publish()
        self.assertEqual(self.writer.client.put_object.call_count,1)
        self.assertEqual(self.transport.state['completion']['phase'],'put_intent')
    def test_marker_readback_corruption_requires_reconciliation(self):
        self.writer.client.get_object.side_effect=lambda **kw:{'VersionId':'other','ContentLength':0,'Body':io.BytesIO(b'corrupt')}
        with self.assertRaisesRegex(SealFailure,'^COMPLETION_OUTCOME_REQUIRES_RECONCILIATION$'):self.publish()
        self.assertEqual(self.transport.state['completion']['phase'],'verify_intent')
        self.assertFalse(self.transport.state['backup_complete'])


if __name__=='__main__':unittest.main()
