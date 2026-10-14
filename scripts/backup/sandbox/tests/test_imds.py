"""Protocol unit tests. No listener or socket is created."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from scripts.backup.sandbox.imds import Protocol, ROLE

class ImdsTests(unittest.TestCase):
    def setUp(self):
        self.now=100000;self.p=Protocol(clock=lambda:self.now)
    def token(self,ttl='2'):
        status,token=self.p.request('PUT','/latest/api/token',{'X-aws-ec2-metadata-token-ttl-seconds':ttl})
        self.assertEqual(status,200);return token.decode()
    def get(self,token,path='/latest/meta-data/iam/security-credentials/'):
        return self.p.request('GET',path,{'X-aws-ec2-metadata-token':token})
    def test_token_role_credentials(self):
        t=self.token();status,body=self.get(t);self.assertEqual(status,200);self.assertTrue(body==(ROLE+'\n').encode(),'Unexpected role response')
        status,body=self.get(t,'/latest/meta-data/iam/security-credentials/'+ROLE)
        self.assertEqual(status,200);data=json.loads(body)
        self.assertTrue(data['AccessKeyId'].startswith('SYNTHETIC'));self.assertEqual(data['Code'],'Success')
    def test_v1_is_rejected(self): self.assertEqual(self.p.request('GET','/latest/meta-data/iam/security-credentials/',{})[0],401)
    def test_expired_token(self):
        t=self.token();self.now+=2;self.assertEqual(self.get(t)[0],401)
    def test_ttl_bounds(self):
        for ttl in ('','0','21601','-1','abc','１２','9'*500):
            with self.subTest(ttl=ttl): self.assertEqual(self.p.request('PUT','/latest/api/token',{'x-aws-ec2-metadata-token-ttl-seconds':ttl})[0],400)
    def test_unknown_path(self): self.assertEqual(self.get(self.token(),'/proxy/aws')[0],404)
    def test_invalid_method(self): self.assertEqual(self.p.request('POST','/latest/api/token',{})[0],405)
    def test_body_rejected(self): self.assertEqual(self.p.request('PUT','/latest/api/token',{},1)[0],400)
    def test_large_headers(self): self.assertEqual(self.p.request('PUT','/latest/api/token',{'x':'x'*513})[0],400)
    def test_capacity_under_concurrent_requests(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.p.request('PUT','/latest/api/token',{'x-aws-ec2-metadata-token-ttl-seconds':'10'})[0],range(100)))
        self.assertEqual(results.count(200),64);self.assertEqual(results.count(429),36)
    def test_credentials_rotate_without_persistence(self):
        t=self.token('21600');path='/latest/meta-data/iam/security-credentials/'+ROLE
        a=json.loads(self.get(t,path)[1]);self.now+=3550;b=json.loads(self.get(t,path)[1]);self.assertTrue(a['Token']!=b['Token'],'Synthetic credentials should rotate')
    def test_import_has_no_server_start(self):
        import scripts.backup.sandbox.moto_server
        self.assertFalse(hasattr(scripts.backup.sandbox.moto_server,'listener'))
