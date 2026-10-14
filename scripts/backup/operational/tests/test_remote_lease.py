import unittest
from scripts.backup.operational.remote_lease import RemoteDeployLease,AuthenticatedHostChannel
from scripts.backup.operational.ssm import Target
from scripts.backup.capture.storage import CaptureFailure

class Channel(AuthenticatedHostChannel):
    def __init__(self):self.commands=[];self.fail=False;self.mismatch=False
    def request(self,r):
        self.commands.append(r['command'])
        if self.fail:raise TimeoutError('synthetic-sensitive')
        result={**r,'phase':'released' if r['command']=='release' else 'held'}
        if self.mismatch:result['owner']='other'
        return result

class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.channel=Channel();self.lease=RemoteDeployLease(self.channel,Target('123456789012','us-east-1','i-'+'a'*17,'dedicated-deploy'),'1:1')
    def test_held_across_entire_context(self):
        with self.lease.hold():
            self.lease.assert_held()
        self.assertEqual(self.channel.commands,['acquire','assert','assert','release'])
    def test_failure_does_not_release(self):
        with self.assertRaises(RuntimeError):
            with self.lease.hold():raise RuntimeError('synthetic')
        self.assertEqual(self.channel.commands,['acquire'])
    def test_lost_channel_blocks_without_release(self):
        with self.assertRaisesRegex(CaptureFailure,'^REMOTE_LEASE_OUTCOME_UNKNOWN$'):
            with self.lease.hold():self.channel.fail=True;self.lease.assert_held()
        self.assertNotIn('release',self.channel.commands)
    def test_bad_identity_blocks_acquire(self):
        self.channel.mismatch=True
        with self.assertRaisesRegex(CaptureFailure,'^REMOTE_LEASE_ATTESTATION_INVALID$'):
            with self.lease.hold():self.fail('Unexpected acquired lease')
    def test_no_default_channel(self):
        lease=RemoteDeployLease(AuthenticatedHostChannel(),self.lease.target,'1:1')
        with self.assertRaisesRegex(CaptureFailure,'^REMOTE_LEASE_OUTCOME_UNKNOWN$'):
            with lease.hold():self.fail('Missing implementation accepted')
