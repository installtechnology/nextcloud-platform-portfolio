"""Offline tests: no subprocess, SSH or AWS API is called."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from scripts.backup.operational.remote_lease import RemoteDeployLease, AuthenticatedHostChannel
from scripts.backup.operational.ssm import Target
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('deploy', ROOT / 'scripts/ci/deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = dict(AWS_ACCOUNT_ID='000000000000', AWS_REGION='us-east-1', PROJECT_NAME='fixture',
                   DEPLOY_ENVIRONMENT='lab', DATA_BUCKET_NAME='fixture-data', BACKUPS_BUCKET_NAME='fixture-backups',
                   STATE_BUCKET_NAME='fixture-state', DEPLOY_MODE='plan', GITHUB_REF='refs/heads/main',
                   DEPLOY_BRANCH='main', RUNNER_TEMP=self.tmp.name, GITHUB_REPOSITORY='fixture/platform',
                   GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')
        self.patch = patch.dict(os.environ, env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_apply_gate(self):
        deploy.context()
        os.environ['DEPLOY_MODE'] = 'apply'
        for confirmation in ('', 'apply', 'APPLY; echo unsafe'):
            os.environ['CONFIRMATION'] = confirmation
            with self.assertRaises(ValueError): deploy.context()
        os.environ['CONFIRMATION'] = 'APPLY'
        deploy.context()

    def test_identity_bucket_branch_guards(self):
        for name, value in (('DATA_BUCKET_NAME', 'fixture-state'), ('GITHUB_REF', 'refs/heads/evil'), ('AWS_ACCOUNT_ID', '*'), ('AWS_REGION', 'us-east-1; echo unsafe')):
            with patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError): deploy.context()

    def test_backend_keys_and_default_inheritance(self):
        directory=Path(self.tmp.name)/'checkout'
        for root in ('persistent','runtime'):
            path=directory/f'terraform/aws/{root}'
            path.mkdir(parents=True)
            (path/'backend.tf.example').write_text('terraform { backend "s3" {} }')
        with patch.object(deploy,'ROOT',directory),patch.dict(os.environ,dict(AWS_ROLE_ARN='arn:aws:iam::000000000000:role/fixture-lab-github',
                SSH_PUBLIC_KEY='ssh-ed25519 AAAA fixture',SSH_ALLOWED_CIDRS='["192.0.2.1/32"]',HTTP_ALLOWED_CIDRS='["192.0.2.1/32"]')):
            deploy.prepare()
            path=deploy.paths()
            runtime=json.loads((path/'runtime.tfvars.json').read_text())
            self.assertNotIn('instance_type',runtime)
            self.assertNotIn('root_volume_size_gib',runtime)
            for root in ('persistent','runtime'):
                backend=(path/f'{root}.tfbackend').read_text()
                self.assertIn('use_lockfile = true',backend)
                self.assertIn(f'states/000000000000/fixture/lab/{root}/terraform.tfstate',backend)
                self.assertNotIn('access_key',backend)
                self.assertTrue((directory/f'terraform/aws/{root}/backend.tf').exists())

    def test_secret_preflight_rejects_mismatch_and_removes_key(self):
        values={name:'x'*32 for name in ('NEXTCLOUD_ADMIN_PASSWORD','POSTGRES_PASSWORD','REDIS_PASSWORD','GRAFANA_ADMIN_PASSWORD')}
        values.update(POSTGRES_ADMIN_PASSWORD='y'*32, NEXTCLOUD_ADMIN_USER='operator', GRAFANA_ADMIN_USER='operator',
                      SSH_PRIVATE_KEY='offline private fixture', SSH_PUBLIC_KEY='ssh-ed25519 expected')
        with patch.dict(os.environ,values),patch.object(deploy,'run',return_value='ssh-ed25519 other'):
            with self.assertRaises(ValueError): deploy.preflight()
        self.assertFalse((deploy.paths()/'ssh-key').exists())

    def test_wrong_account_aborts_before_terraform(self):
        with patch.object(deploy,'aws',return_value={'Account':'111111111111'}),patch.object(deploy,'run') as commands:
            with self.assertRaises(ValueError): deploy.terraform()
            commands.assert_not_called()

    def rule(self):
        return dict(SecurityGroupRuleId='sgr-test', GroupId='sg-test', IsEgress=False, IpProtocol='tcp', FromPort=22, ToPort=22,
                    CidrIpv4='192.0.2.1/32', Description='nextcloud-deploy:123:1', Tags=[dict(Key=k, Value=v) for k,v in
                    dict(Workflow='nextcloud-deploy',Repository='fixture/platform',Project='fixture',Environment='lab',Run='123-1',CreatedAt='1').items()])

    def test_rule_ownership_keeps_admin_and_other_projects(self):
        rule=self.rule()
        self.assertTrue(deploy.owned_rule(rule, 'sg-test','fixture/platform','fixture','lab'))
        for field,value in [('Tags',[]),('Description','SSH administrativo'),('CidrIpv4','0.0.0.0/0'),('GroupId','sg-other'),('ToPort',443),('IsEgress',True)]:
            altered=dict(rule, **{field:value})
            self.assertFalse(deploy.owned_rule(altered,'sg-test','fixture/platform','fixture','lab'))

    def test_cleanup_targets_current_or_expired_only(self):
        current=self.rule()
        old=self.rule(); old['SecurityGroupRuleId']='sgr-old'
        old['Tags']=[dict(tag,Value='999-1') if tag['Key']=='Run' else tag for tag in old['Tags']]
        live=self.rule(); live['SecurityGroupRuleId']='sgr-live'
        live['Tags']=[dict(tag,Value='999-1') if tag['Key']=='Run' else dict(tag,Value='9999999999') if tag['Key']=='CreatedAt' else tag for tag in live['Tags']]
        admin=self.rule(); admin['Tags']=[]
        def fake(*args):
            return {'SecurityGroupRules':[current,old,live,admin]} if args[1]=='describe-security-group-rules' else {}
        with patch.object(deploy,'aws',side_effect=fake) as api:
            deploy.revoke_owned('sg-test',True)
            self.assertEqual([call.args[-1] for call in api.call_args_list if call.args[1]=='revoke-security-group-ingress'], ['sgr-test'])
        with patch.object(deploy,'aws',side_effect=fake) as api:
            deploy.revoke_owned('sg-test',False)
            self.assertEqual([call.args[-1] for call in api.call_args_list if call.args[1]=='revoke-security-group-ingress'], ['sgr-test','sgr-old'])

    def test_host_key_requires_authenticated_fingerprint(self):
        blob=b'offline public-key fixture'
        key=base64.b64encode(blob).decode()
        fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
        console=f'ec2: BEGIN SSH HOST KEY FINGERPRINTS\nec2: 256 {fingerprint} no comment (ED25519)\nec2: END SSH HOST KEY FINGERPRINTS'
        fps=deploy.console_fingerprints(console)
        self.assertEqual(fps,{fingerprint})
        self.assertEqual(deploy.verify_scan(f'192.0.2.1 ssh-ed25519 {key}', '192.0.2.1', fps),f'192.0.2.1 ssh-ed25519 {key}\n')
        for scan,proof in [(f'192.0.2.1 ssh-ed25519 {key}',set()),(f'192.0.2.2 ssh-ed25519 {key}',fps),(f'192.0.2.1 ssh-rsa {key}',fps)]:
            with self.assertRaises(ValueError): deploy.verify_scan(scan,'192.0.2.1',proof)

    def test_secret_file_removed_even_if_aws_cleanup_fails(self):
        directory=deploy.paths()
        deploy.private_file(directory/'runtime.json',json.dumps({'security_group_id':'sg-test'}))
        deploy.private_file(directory/'ssh-key','offline fixture')
        deploy.private_file(directory/'runtime.tfplan','offline fixture')
        with patch.object(deploy,'revoke_owned',side_effect=RuntimeError('offline denied')):
            with self.assertRaises(RuntimeError): deploy.cleanup()
        self.assertFalse((directory/'ssh-key').exists())
        self.assertFalse((directory/'runtime.tfplan').exists())

    def test_plan_never_applies_or_connects(self):
        calls=[]
        def fake_run(args,**kwargs):
            calls.append(args)
            if 'show' in args: return json.dumps({'format_version':'1.2','resource_changes':[]})
        def fake_aws(*args):
            return {'Account':'000000000000'} if args[0]=='sts' else {'LocationConstraint':None}
        # Plan files are fixtures; no real Terraform is invoked.
        directory=deploy.paths()
        for root in ('persistent','runtime'): (directory/f'{root}.tfplan').touch()
        with patch.object(deploy,'run',side_effect=fake_run),patch.object(deploy,'aws',side_effect=fake_aws): deploy.terraform()
        self.assertEqual(sum('plan' in call for call in calls),2)
        self.assertFalse(any('apply' in call or 'ansible-playbook' in call for call in calls))

    def test_runtime_ip_changes_propagate_to_inventory_env_and_browser_urls(self):
        for instance, public_ip in (('i-'+'a'*17, '192.0.2.10'), ('i-'+'b'*17, '198.51.100.42')):
            values = dict(instance_id=instance, public_ip=public_ip, security_group_id='sg-fixture',
                          ssh_user='ubuntu', data_bucket_name='fixture-data')
            outputs = {key: {'value': value} for key, value in values.items()}
            def fake_run(args, **kwargs):
                for arg in args:
                    if arg.startswith('-out='): Path(arg.split('=', 1)[1]).touch()
                if 'show' in args:return json.dumps({'format_version':'1.2','resource_changes':([{'address':'aws_instance.host','mode':'managed','change':{'actions':['no-op'],'before':{'id':instance},'after':{'id':instance}}}] if any('runtime' in a for a in args) else [])})
                return json.dumps(outputs) if 'output' in args else ''
            github_env = Path(self.tmp.name) / (instance + '.env')
            github_summary = Path(self.tmp.name) / (instance + '.md')
            with patch.dict(os.environ, {'DEPLOY_MODE': 'apply', 'CONFIRMATION': 'APPLY',
                                         'GITHUB_ENV': str(github_env), 'GITHUB_STEP_SUMMARY': str(github_summary)}), \
                 patch.object(deploy, 'run', side_effect=fake_run), \
                 patch.object(deploy, 'aws', side_effect=lambda *args: {'Account': '000000000000'} if args[0] == 'sts' else {'LocationConstraint': None}):
                class Channel(AuthenticatedHostChannel):
                    def request(self,r):return {**{k:v for k,v in r.items() if k!='credentials'},'phase':'released' if r['command']=='release' else 'held'}
                lease=RemoteDeployLease(Channel(),Target('000000000000','us-east-1',instance,'fixture-lab-deploy-guard'),'123:1',dict(project='fixture',environment='lab',data_bucket='fixture-data',backups_bucket='fixture-backups',state_bucket='fixture-state'))
                with lease.hold():deploy.terraform(lease) # All operations mocked.
                stored = json.loads((deploy.paths() / 'runtime.json').read_text())
                inventory = deploy.runtime_inventory(stored)
                self.assertEqual(inventory['all']['children']['platform']['hosts']['application_host']['ansible_host'], public_ip)
                self.assertIn('PLATFORM_HOST=' + public_ip, github_env.read_text())
                with patch.dict(os.environ, {'PLATFORM_HOST': public_ip}): deploy.summary()
                summary = github_summary.read_text()
                self.assertIn('Nextcloud: http://' + public_ip, summary)
                self.assertIn('Grafana: http://' + public_ip + ':3000', summary)
                self.assertNotIn('ssh -N', summary)

    def test_apply_without_remote_lease_fails_before_any_call(self):
        with patch.dict(os.environ, {'DEPLOY_MODE':'apply','CONFIRMATION':'APPLY'}), patch.object(deploy,'aws') as aws, patch.object(deploy,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'^REMOTE_DEPLOY_GUARD_REQUIRED$'):deploy.terraform()
            aws.assert_not_called();run.assert_not_called()

    def test_connect_without_remote_lease_fails_before_any_call(self):
        with patch.object(deploy,'aws') as aws, patch.object(deploy,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'^REMOTE_DEPLOY_GUARD_REQUIRED$'):deploy.connect()
            aws.assert_not_called();run.assert_not_called()

    def test_urls_and_inventory_reject_ipv6_or_injected_host(self):
        for value in ('::1', '', '192.0.2.1;echo unsafe'):
            with self.assertRaises(ValueError): deploy.service_urls(value)
            with self.assertRaises(ValueError): deploy.runtime_inventory({'public_ip': value, 'ssh_user': 'ubuntu'})

    def test_prepare_rejects_public_or_ipv6_http_grafana_cidrs(self):
        base = dict(AWS_ROLE_ARN='arn:aws:iam::000000000000:role/fixture-lab-github',
                    SSH_PUBLIC_KEY='ssh-ed25519 AAAA fixture', SSH_ALLOWED_CIDRS='["192.0.2.1/32"]')
        for cidr in ('0.0.0.0/0', '::/0'):
            with patch.dict(os.environ, dict(base, HTTP_ALLOWED_CIDRS=json.dumps([cidr]))):
                with self.assertRaises(ValueError): deploy.prepare()


if __name__=='__main__': unittest.main()
