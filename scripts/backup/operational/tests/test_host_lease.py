import io
import json
import unittest
from scripts.backup.operational.tests import test_fencing as f
from scripts.backup.operational.tests.test_policy_ownership import API
from scripts.backup.operational.policy_ownership import PolicyOwner
from scripts.backup.operational.host_lease import serve_lease
from scripts.backup.capture.storage import CaptureFailure

class HostLeaseTests(unittest.TestCase):
    def setUp(self):
        self.store=f.Store();api=API(self.store);owner=PolicyOwner(self.store,api,f.SCOPE,lambda:None)
        with owner.hold(f.OP) as lease:owner.adopt(lease)
        self.identity={'account':f.SCOPE.account,'region':'us-east-1','instance':'i-'+'a'*17}
        self.first={'version':1,'command':'acquire','owner':'1:1','nonce':'a'*64,**self.identity}
        self.output=io.BytesIO()
    def invoke(self,requests):
        raw=b''.join(json.dumps(r).encode()+b'\n' for r in requests)
        serve_lease(self.store,lambda:self.identity,lambda:[],io.BytesIO(raw),self.output)
    def test_clean_release(self):
        self.invoke([self.first,{**self.first,'command':'assert'},{**self.first,'command':'release'}])
        self.assertEqual(self.store.data['deploy-lease.json']['phase'],'released')
        self.assertEqual([json.loads(l)['phase'] for l in self.output.getvalue().splitlines()],['held','held','released'])
    def test_eof_does_not_release(self):
        with self.assertRaisesRegex(CaptureFailure,'^DEPLOY_INTERRUPTED$'):self.invoke([self.first])
        self.assertEqual(self.store.data['deploy-lease.json']['phase'],'intervention_required')
    def test_other_owner_rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^DEPLOY_INTERRUPTED$'):
            self.invoke([self.first,{**self.first,'command':'release','owner':'2:1'}])
    def test_guard_absent_blocks(self):
        del self.store.data['policy-owner-active.json']
        with self.assertRaisesRegex(CaptureFailure,'^POLICY_OWNER_NOT_REGISTERED$'):self.invoke([self.first])
        self.assertEqual(self.output.getvalue(),b'')
    def test_capture_active_blocks(self):
        self.store.write('active.json',{'operation_id':f.CAPTURE,'terminal':False})
        with self.assertRaisesRegex(CaptureFailure,'^RECOVERY_REQUIRED$'):self.invoke([self.first])
