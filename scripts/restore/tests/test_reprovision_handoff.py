"""Protocol and independent discovery tests; no host/AWS access."""
import base64
from contextlib import contextmanager
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import Mock,patch
from scripts.restore.initial_lease import serve
from scripts.restore.discover_target import discover
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import SealFailure,json_bytes


class Control:
    def __init__(self):self.phase=None
    @contextmanager
    def hold(self,owner):
        self.phase='held'
        try:yield
        except BaseException:self.phase='intervention_required';raise
        else:self.phase='released'
    def assert_held(self):
        if self.phase!='held':raise CaptureFailure('LOST_TEST_LEASE')


class Handoff(unittest.TestCase):
    def setUp(self):
        self.c=Control();self.d={'owner':'123:1','host':{'account':'000000000000','region':'us-east-1','instance':'i-'+'3'*17}}
        self.request={'version':1,'command':'acquire','owner':'123:1','nonce':'b'*64,**self.d['host']}
    def run_protocol(self,rows):
        incoming=io.BytesIO(b''.join(json_bytes(r)+b'\n' for r in rows));out=io.BytesIO()
        with patch('scripts.restore.initial_lease.write_new') as claim:serve(self.c,self.d,incoming,out)
        return [json.loads(r) for r in out.getvalue().splitlines()],claim
    def test_claim_before_authority_reply_and_release(self):
        rows,claim=self.run_protocol([self.request,{**self.request,'command':'assert'},{**self.request,'command':'release'}])
        self.assertEqual([r['phase'] for r in rows],['held','held','released'])
        self.assertEqual(self.c.phase,'released');claim.assert_called_once()
    def test_peer_eof_preserves_intervention(self):
        with self.assertRaises(SealFailure) as caught:self.run_protocol([self.request])
        self.assertEqual(caught.exception.code,'CREATION_HOST_PROTOCOL_REJECTED')
        self.assertEqual(self.c.phase,'intervention_required')
    def test_owner_nonce_identity_changes_do_not_release(self):
        for field,value in [('owner','124:1'),('nonce','c'*64),('instance','i-'+'4'*17),('command','unlock')]:
            with self.subTest(field=field):
                self.setUp()
                with self.assertRaises(SealFailure) as caught:self.run_protocol([self.request,{**self.request,field:value}])
                self.assertEqual(caught.exception.code,'CREATION_HOST_PROTOCOL_REJECTED')
                self.assertEqual(self.c.phase,'intervention_required')
    def test_claim_collision_blocks_and_preserves_intervention(self):
        with patch('scripts.restore.initial_lease.write_new',side_effect=CaptureFailure('PRIVATE_FILE_ALREADY_EXISTS')):
            with self.assertRaises(CaptureFailure):serve(self.c,self.d,io.BytesIO(json_bytes(self.request)+b'\n'),io.BytesIO())
        self.assertEqual(self.c.phase,'intervention_required')
    def test_release_cannot_precede_acquire(self):
        with self.assertRaises(SealFailure):self.run_protocol([{**self.request,'command':'release'}])
        self.assertIsNone(self.c.phase)
    def test_signal_timer_is_bounded_and_restored(self):
        import scripts.restore.initial_lease as m
        fake=Mock(SIGINT=2,SIGTERM=15,SIGALRM=14,ITIMER_REAL=0)
        fake.getitimer.return_value=(0.0,0.0)
        with patch.object(m,'signal',fake),m.bounded_session():pass
        self.assertEqual(fake.setitimer.call_args_list[0].args,(0,900))
        self.assertEqual(fake.setitimer.call_args_list[-1].args,(0,0))
        self.assertEqual(fake.signal.call_count,6)
    def test_existing_timer_is_not_overwritten(self):
        import scripts.restore.initial_lease as m
        fake=Mock(SIGINT=2,SIGTERM=15,SIGALRM=14,ITIMER_REAL=0);fake.getitimer.return_value=(1.0,0.0)
        with patch.object(m,'signal',fake),self.assertRaises(SealFailure):
            with m.bounded_session():pass
        fake.setitimer.assert_not_called()


class Discovery(unittest.TestCase):
    def setUp(self):
        self.key=base64.b64encode(b'synthetic-host-public-key').decode()
        self.node={'InstanceId':'i-'+'3'*17,'State':{'Name':'running'},'Tags':[
            {'Key':k,'Value':v} for k,v in {'Project':'fixture','Environment':'restore',
            'RecoveryPreparedCommit':'a'*40,'RecoveryCreationOwner':'123:1','RecoveryHostKey':'ssh-ed25519 '+self.key}.items()]}
        self.ec2=Mock();self.ec2.describe_instances.return_value={'Reservations':[{'OwnerId':'000000000000','Instances':[self.node]}]}
        fp='SHA256:'+base64.b64encode(hashlib.sha256(b'synthetic-host-public-key').digest()).decode().rstrip('=')
        self.ec2.get_console_output.return_value={'Output':'-----BEGIN SSH HOST KEY FINGERPRINTS-----\n256 '+fp+' root (ED25519)\n-----END SSH HOST KEY FINGERPRINTS-----'}
    def call(self):return discover(self.ec2,'000000000000','us-east-1','fixture','restore','i-'+'1'*17,'a'*40,'fixture-lab-restore')
    def test_unique_verified_target(self):
        target,key=self.call();self.assertEqual(target.instance,self.node['InstanceId']);self.assertIn(self.key,key)
    def test_source_rejected_before_console(self):
        self.node['InstanceId']='i-'+'1'*17
        with self.assertRaises(SealFailure) as caught:self.call()
        self.assertEqual(caught.exception.code,'RESTORE_SOURCE_INSTANCE_REJECTED');self.ec2.get_console_output.assert_not_called()
    def test_multiple_instances_blocked(self):
        self.ec2.describe_instances.return_value['Reservations'][0]['Instances'].append(copy.deepcopy(self.node))
        with self.assertRaises(SealFailure) as caught:self.call()
        self.assertEqual(caught.exception.code,'RESTORE_DISCOVERY_AMBIGUOUS')
    def test_foreign_account_blocked(self):
        self.ec2.describe_instances.return_value['Reservations'][0]['OwnerId']='111111111111'
        with self.assertRaises(SealFailure):self.call()
        self.ec2.get_console_output.assert_not_called()
    def test_truncated_discovery_blocked(self):
        self.ec2.describe_instances.return_value['NextToken']='synthetic'
        with self.assertRaises(SealFailure):self.call()
    def test_stale_commit_or_wrong_environment_rejected(self):
        for key in ('RecoveryPreparedCommit','Environment','RecoveryCreationOwner'):
            with self.subTest(key=key):
                self.setUp()
                next(t for t in self.node['Tags'] if t['Key']==key)['Value']='wrong'
                with self.assertRaises(SealFailure) as caught:self.call()
                self.assertEqual(caught.exception.code,'RESTORE_PREPARED_TARGET_REJECTED')
    def test_unverified_host_key_rejected(self):
        self.ec2.get_console_output.return_value={'Output':'-----BEGIN SSH HOST KEY FINGERPRINTS-----\n-----END SSH HOST KEY FINGERPRINTS-----'}
        with self.assertRaises(SealFailure) as caught:self.call()
        self.assertEqual(caught.exception.code,'RESTORE_INDEPENDENT_HOST_KEY_REQUIRED')
    def test_oversized_console_blocked(self):
        self.ec2.get_console_output.return_value={'Output':'x'*(256*1024+1)}
        with self.assertRaises(SealFailure) as caught:self.call()
        self.assertEqual(caught.exception.code,'RESTORE_CONSOLE_UNVERIFIED')


if __name__=='__main__':unittest.main()
