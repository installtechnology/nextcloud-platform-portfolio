"""Bounded internal HBA journal: no relaxation of offline contract JSON limits."""
import base64,hashlib,json,os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from scripts.backup.capture.storage import Store,CaptureFailure
from scripts.backup.errors import ValidationFailure
OP='20260101T000000Z-01234567-89ab-4cde-8fab-0123456789ab'
class PostgreSQLJournalReadTests(unittest.TestCase):
 def setUp(self):
  parent=Path(__file__).resolve().parents[4]/'.validation-tools'/'pg-journal-tests';parent.mkdir(parents=True,exist_ok=True)
  self.temp=TemporaryDirectory(dir=parent);self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);os.chmod(self.root,0o700)
  self.store=Store(self.root,synthetic=True);self.store.operation(OP).mkdir(mode=0o700)
 def record(self,size=5745):
  raw=b'#'+b'x'*(size-1)
  return {'kind':'postgres-auth-fence','operation_id':OP,'phase':'held','before_base64':base64.b64encode(raw).decode(),'before_sha256':hashlib.sha256(raw).hexdigest(),'fenced_sha256':'a'*64}
 def read(self,value):
  self.store.write(OP+'/postgres-fence.json',value)
  return self.store.read(OP+'/postgres-fence.json')
 def test_actual_size_and_maximum_roundtrip(self):
  for size in [5745,65536]:
   with self.subTest(size=size):
    v=self.record(size);self.assertEqual(self.read(v),v)
 def test_decoded_size_above_bound_rejected(self):
  with self.assertRaises((CaptureFailure,ValidationFailure)):self.read(self.record(65537))
 def test_other_journals_keep_string_limit(self):
  self.store.write(OP+'/journal.json',{'value':'x'*4097})
  with self.assertRaises(ValidationFailure) as e:self.store.read(OP+'/journal.json')
  self.assertEqual(e.exception.code,'JSON_LIMIT')
 def test_other_fields_keep_string_limit(self):
  v=self.record();v['unexpected']='x'*4097
  with self.assertRaises(ValidationFailure) as e:self.read(v)
  self.assertEqual(e.exception.code,'JSON_LIMIT')
 def test_invalid_base64_or_hash_rejected(self):
  for field,bad in [('before_base64','invalid!'),('before_sha256','b'*64)]:
   v=self.record();v[field]=bad
   with self.assertRaises(CaptureFailure) as e:self.read(v)
   self.assertEqual(e.exception.code,'POSTGRES_FENCE_JOURNAL_INVALID')
 def test_wrong_operation_and_kind_rejected(self):
  for field,bad in [('operation_id',OP.replace('01234567','11234567')),('kind','foreign')]:
   v=self.record();v[field]=bad
   with self.assertRaises(CaptureFailure):self.read(v)
 def test_duplicate_keys_still_rejected(self):
  v=self.record();raw=json.dumps(v)[:-1]+',"phase":"held"}'
  (self.store.operation(OP)/'postgres-fence.json').write_text(raw)
  with self.assertRaises(ValidationFailure) as e:self.store.read(OP+'/postgres-fence.json')
  self.assertEqual(e.exception.code,'DUPLICATE_JSON_KEY')
