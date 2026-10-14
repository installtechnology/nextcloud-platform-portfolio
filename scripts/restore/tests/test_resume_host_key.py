"""Administrative SSM host-key binding; synthetic clients, no AWS or SSH IO."""
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from scripts.restore.tests import test_creation_resume as fixture
from scripts.restore.reprovision import validate_resume_host_key
from scripts.restore.reprovision_grant import issue,observed_ssm_host_key
from scripts.backup.sealing.common import SealFailure
from scripts.ci import deploy


class HostKey(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Resume();self.f.setUp()
        blob=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'+b'x'*32
        self.key='ssh-ed25519 '+base64.b64encode(blob).decode()
        self.proof={'instance':self.f.host['instance'],'public_key':self.key,
                    'ssm_command_id':'12345678-1234-1234-1234-123456789abc'}
    def issue(self,proof=None):
        f=self.f
        return issue(f.s3,f.sts,json.dumps(f.c),f.new.owner,f.new.commit,f.observer,
                     host_key_observation=self.proof if proof is None else proof)
    def response(self):
        return {'CommandId':self.proof['ssm_command_id'],'InstanceId':self.proof['instance'],
                'DocumentName':'AWS-RunShellScript','Status':'Success','ResponseCode':0,
                'StandardErrorContent':'','StandardOutputContent':json.dumps({
                    'instance':self.proof['instance'],'public_key':self.key,'sshd_host_key_configured':True})}
    def test_grant_binds_public_key_without_changing_prior_history(self):
        self.issue();self.f.new.authorize()
        self.assertEqual(self.f.new.resume['host_key'],self.proof)
        self.assertEqual(self.f.s3.data[self.f.old.key],self.f.history)
    def test_foreign_instance_blocks_before_grant_write(self):
        with self.assertRaisesRegex(SealFailure,'CREATION_RESUME_HOST_KEY_INVALID'):
            self.issue({**self.proof,'instance':'i-'+'4'*17})
        self.assertNotIn(self.f.new.grant_key,self.f.s3.data)
    def test_invalid_public_key_wire_format_blocks(self):
        for value in ('ssh-rsa AAAA','ssh-ed25519 !!!','ssh-ed25519 AAAA',self.key+' extra'):
            with self.subTest(value=value),self.assertRaisesRegex(SealFailure,'CREATION_RESUME_HOST_KEY_INVALID'):
                validate_resume_host_key({**self.proof,'public_key':value},self.proof['instance'])
    def test_unbound_or_malformed_command_evidence_blocks(self):
        for value in ('','unknown','1234'):
            with self.subTest(value=value),self.assertRaisesRegex(SealFailure,'CREATION_RESUME_HOST_KEY_INVALID'):
                validate_resume_host_key({**self.proof,'ssm_command_id':value},self.proof['instance'])
    def test_ssm_requires_terminal_success_and_exact_command_target(self):
        client=Mock()
        for field,value in (('InstanceId','i-'+'4'*17),('CommandId','other'),('Status','InProgress'),
                            ('DocumentName','other'),('ResponseCode',1),('StandardErrorContent','error')):
            client.get_command_invocation.return_value={**self.response(),field:value}
            with self.subTest(field=field),self.assertRaisesRegex(SealFailure,'CREATION_SSM_HOST_KEY_UNVERIFIED'):
                observed_ssm_host_key(client,self.proof['ssm_command_id'],self.proof['instance'])
        client.send_command.assert_not_called()
    def test_authenticated_ssm_observation_is_recorded_as_ssm(self):
        client=Mock();client.get_command_invocation.return_value=self.response()
        self.assertEqual(observed_ssm_host_key(client,self.proof['ssm_command_id'],self.proof['instance']),self.proof)
        client.get_command_invocation.assert_called_once_with(CommandId=self.proof['ssm_command_id'],InstanceId=self.proof['instance'])
        client.send_command.assert_not_called()
    def test_only_authorized_creation_connection_uses_grant_key(self):
        self.issue();self.f.new.acquire()
        with patch.object(deploy,'_connect_verified') as connect:
            deploy.connect_created_host(self.f.new,SimpleNamespace(**self.f.host))
        connect.assert_called_once_with(host_key=self.key)
        with patch.object(deploy,'_connect_verified') as connect:
            with self.assertRaisesRegex(RuntimeError,'CREATION_AUTHORITY_REQUIRED'):
                deploy.connect_created_host(Mock(),SimpleNamespace(**self.f.host))
            connect.assert_not_called()
    def connection(self,scan_key):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);env={'RUNNER_TEMP':tmp,'SSH_PRIVATE_KEY':'synthetic fixture',
                'SSH_PUBLIC_KEY':self.key,'GITHUB_RUN_ID':'124','GITHUB_RUN_ATTEMPT':'1',
                'GITHUB_REPOSITORY':'fixture/platform','GITHUB_ENV':str(p/'env')}
            values={'security_group_id':'sg-fixture','instance_id':self.proof['instance'],
                    'public_ip':'8.8.8.8','ssh_user':'ubuntu','data_bucket_name':'fixture-data'}
            (p/'runtime.json').write_text(json.dumps(values))
            def aws(*args):
                if args[1]=='describe-instances':return {'Reservations':[{'OwnerId':'000000000000','Instances':[{
                    'Tags':[{'Key':'Project','Value':'fixture'},{'Key':'Environment','Value':'restore'}],
                    'PublicIpAddress':'8.8.8.8','SecurityGroups':[{'GroupId':'sg-fixture'}]}]}]}
                if args[1]=='describe-security-group-rules':return {'SecurityGroupRules':[{
                    'IsEgress':False,'IpProtocol':'tcp','FromPort':22,'ToPort':22,'CidrIpv4':'8.8.4.4/32'}]}
                if args[1]=='wait':return {}
                raise AssertionError('Unexpected AWS operation: '+args[1])
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.return_value=b'8.8.4.4'
            with patch.dict(os.environ,env,clear=True),patch.object(deploy,'context',return_value=(
                    '000000000000','us-east-1','fixture','restore',['fixture-data','fixture-backups','fixture-state'])),\
                 patch.object(deploy,'paths',return_value=p),patch.object(deploy,'aws',side_effect=aws),\
                 patch.object(deploy,'revoke_owned'),patch.object(deploy,'run',return_value=self.key),\
                 patch.object(deploy.urllib.request,'urlopen',return_value=response),\
                 patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='8.8.8.8 '+scan_key)) as scan:
                deploy._connect_verified(host_key=self.key)
                self.assertEqual((p/'known_hosts').read_text(),'8.8.8.8 '+self.key+'\n')
                scan.assert_called_once()
                self.assertIn('StrictHostKeyChecking yes',(p/'ssh_config').read_text())
    def test_matching_scan_uses_admin_key_without_console(self):self.connection(self.key)
    def test_mismatched_network_key_never_falls_back(self):
        other='ssh-ed25519 '+base64.b64encode(b'different network key').decode()
        with self.assertRaisesRegex(ValueError,'SSH key not authenticated'):
            self.connection(other)
    def test_handoff_enters_private_repository_after_sudo(self):
        from scripts.restore.reprovision_operations import ReprovisionOperations
        channel=Mock();channel.open.return_value.__enter__=Mock(return_value=channel)
        channel.open.return_value.__exit__=Mock(return_value=False)
        remote=Mock();remote.hold.return_value.__enter__=Mock(return_value=remote)
        remote.hold.return_value.__exit__=Mock(return_value=False)
        ci=Mock();ci.paths.return_value=Path('/synthetic')
        ops=ReprovisionOperations({},ci,self.f.c,'124:1','b'*40);ops.bound=Mock()
        with patch('scripts.restore.reprovision_operations.InitialSSHChannel',return_value=channel) as ssh,\
             patch('scripts.restore.reprovision_operations.RemoteDeployLease',return_value=remote):
            with ops.remote_lease(self.f.c,{'public_ip':{'value':'8.8.8.8'}},Mock()):pass
        command=ssh.call_args.args[0][-1]
        self.assertTrue(command.startswith('exec sudo -n /usr/bin/env --chdir='))
        self.assertNotIn('&&',command)
        self.assertTrue(command.endswith('/venv/bin/python -m scripts.restore.initial_lease'))


if __name__=='__main__':unittest.main()
