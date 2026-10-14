"""Synthetic integration contracts; never executes Docker, SDK or host commands."""
import ast
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.operational import runtime_config,host_worker,database_fence,runner,host_setup
from scripts.backup.operational.admin_broker import AdministrativeBroker
from scripts.backup.operational.backup_lease import backup_lease,assert_backup_clear
from scripts.backup.operational.deploy_plan import validate_runtime_target
from scripts.backup.operational.remote_lease import RemoteDeployLease,AuthenticatedHostChannel
from scripts.backup.operational.ssm import Target
from scripts.backup.operational.sdk import TemporaryCredentials
from scripts.backup.operational.host_lease import serve_lease
from scripts.backup.operational.policy_ownership import guard_statement
from scripts.backup.operational.fence_policy import digest,build_policy
from scripts.backup.operational.tests import test_fencing as f

ROOT=Path(__file__).resolve().parents[4]


def config():
    import yaml
    compose=yaml.safe_load((ROOT/'docker/compose.yml').read_text())
    base='arn:aws:iam::123456789012:role/synthetic-lab'
    return dict(version=2,account='123456789012',region='us-east-1',instance='i-'+'a'*17,project='synthetic',
        environment='lab',data_bucket='synthetic-data',backups_bucket='synthetic-backups',state_bucket='synthetic-state',
        installation_identity='a'*64,deployment_root='/opt/nextcloud-platform',staging_root='/var/lib/nextcloud-recovery',
        images={s:compose['services'][s]['image'] for s in ('nextcloud','postgres','redis')},
        admin_role=base+'-data-policy-admin',emergency_role='arn:aws:iam::123456789012:role/emergency',
        backup_role=base+'-backup',guard_role=base+'-deploy-guard',storage_encryption='SSE-S3',repository='synthetic/platform')


def session(seconds=3500):
    # Deliberately synthetic, never used for an SDK request.
    return dict(access_key='ASIA'+'A'*16,secret_key='synthetic'.ljust(40,'x'),token='synthetic-token-xxxxxxxx',
        expiration=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat())


class Contracts(unittest.TestCase):
    def test_configuration_exact(self):self.assertEqual(runtime_config.validate(config()).project,'synthetic')
    def test_configuration_cannot_enable_simulation(self):
        for key in ('runtime_allowed','backup_complete','simulation','operational_allowed'):
            with self.subTest(key=key),self.assertRaisesRegex(CaptureFailure,'^HOST_CONFIGURATION_INVALID$'):
                runtime_config.validate({**config(),key:True})
    def test_state_separation(self):
        c=config();c['state_bucket']=c['data_bucket']
        with self.assertRaisesRegex(CaptureFailure,'^HOST_CONFIGURATION_INVALID$'):runtime_config.validate(c)
    def test_backup_cannot_be_deploy_role(self):
        c=config();c['backup_role']=c['guard_role']
        with self.assertRaisesRegex(CaptureFailure,'^HOST_ROLE_BINDING_INVALID$'):runtime_config.validate(c)
    def test_private_age_identity_rejected(self):
        c=config();c['recipient']='AGE-SECRET-KEY-synthetic'
        with self.assertRaisesRegex(CaptureFailure,'^HOST_CONFIGURATION_INVALID$'):runtime_config.validate(c)
    def test_host_paths_not_configurable(self):
        for k in ('staging_root',):
            c=config();c[k]='/tmp/unsafe'
            with self.subTest(k=k),self.assertRaisesRegex(CaptureFailure,'^HOST_PRIVATE_PATH_REJECTED$'):runtime_config.validate(c)
    def test_credentials_repr_has_no_fields(self):
        c=session();shown=repr(TemporaryCredentials(c['access_key'],c['secret_key'],c['token']))
        for key in ('access_key','secret_key','token'):self.assertNotIn(c[key],shown)
    def test_credentials_lifetime(self):
        for seconds in (30,2999,8000):
            with self.subTest(seconds=seconds),self.assertRaisesRegex(CaptureFailure,'^SESSION_LIFETIME_INSUFFICIENT$'):
                host_worker.credentials(session(seconds))
    def test_credentials_permanent_rejected(self):
        c=session();c['access_key']='AKIA'+'A'*16
        with self.assertRaisesRegex(CaptureFailure,'^TEMPORARY_CREDENTIALS_REQUIRED$'):host_worker.credentials(c)
    def request(self):
        c=config();return {'version':1,'owner':'123:1','commit':'a'*40,'repository':c['repository'],'confirmation':'BACKUP',
            'scope':{k:c[k] for k in ('account','region','instance','project','environment','data_bucket','backups_bucket')},
            'backup_credentials':session()}
    def test_host_request_scope(self):self.assertEqual(host_worker.parse_request(json.dumps(self.request()).encode(),config())['owner'],'123:1')
    def test_admin_credentials_never_accepted(self):
        r=self.request();r['administrative_credentials']=session()
        with self.assertRaisesRegex(CaptureFailure,'^BACKUP_REQUEST_REJECTED$'):host_worker.parse_request(json.dumps(r).encode(),config())
    def test_mixed_target_rejected(self):
        r=self.request();r['scope']['data_bucket']='different-data'
        with self.assertRaisesRegex(CaptureFailure,'^BACKUP_SCOPE_MISMATCH$'):host_worker.parse_request(json.dumps(r).encode(),config())
    def test_confirmation_injection(self):
        r=self.request();r['confirmation']='BACKUP;unsafe'
        with self.assertRaisesRegex(CaptureFailure,'^BACKUP_REQUEST_REJECTED$'):host_worker.parse_request(json.dumps(r).encode(),config())
    def test_duplicate_host_json(self):
        with self.assertRaises(Exception):host_worker.parse_request(b'{"version":1,"version":1}',config())


class Leases(unittest.TestCase):
    def test_backup_held_during_upload_gap(self):
        store=f.Store()
        with backup_lease(store,f.OP,'1:1'):
            self.assertFalse(store.locked)
            with self.assertRaisesRegex(CaptureFailure,'^BACKUP_LEASE_RECOVERY_REQUIRED$'):assert_backup_clear(store)
        assert_backup_clear(store)
    def test_loss_leaves_intervention(self):
        store=f.Store()
        with self.assertRaises(ConnectionError):
            with backup_lease(store,f.OP,'1:1'):raise ConnectionError('synthetic-private')
        self.assertEqual(store.data['backup-lease.json']['phase'],'intervention_required')
    def test_other_backup_rejected(self):
        store=f.Store()
        with backup_lease(store,f.OP,'1:1'):
            with self.assertRaisesRegex(CaptureFailure,'^BACKUP_LEASE_RECOVERY_REQUIRED$'):
                with backup_lease(store,f.CAPTURE,'2:1'):self.fail('overlap')
    def test_v2_attestation_does_not_echo_credentials(self):
        class Channel(AuthenticatedHostChannel):
            def request(self,r):
                self.last=r
                return {**{k:v for k,v in r.items() if k!='credentials'},'phase':'released' if r['command']=='release' else 'held'}
        channel=Channel();lease=RemoteDeployLease(channel,Target('123456789012','us-east-1','i-'+'a'*17,'synthetic-lab-deploy-guard'),
            '1:1',{'project':'synthetic'},session())
        with lease.hold():
            self.assertEqual(channel.last['version'],2);self.assertIn('credentials',channel.last)
            lease.assert_held();self.assertNotIn('credentials',channel.last)
    def test_v2_server_strips_credentials(self):
        from scripts.backup.operational.tests.test_host_lease import HostLeaseTests
        test=HostLeaseTests();test.setUp();first={**test.first,'version':2,'scope':{},'credentials':session()}
        release={k:v for k,v in first.items() if k!='credentials'};release['command']='release'
        output=io.BytesIO();auth=Mock()
        serve_lease(test.store,lambda:test.identity,lambda:[],io.BytesIO(json.dumps(first).encode()+b'\n'+json.dumps(release).encode()+b'\n'),output,auth)
        self.assertEqual(auth.call_count,1);self.assertNotIn(b'secret_key',output.getvalue())
    def test_live_server_refuses_legacy_protocol(self):
        with self.assertRaisesRegex(CaptureFailure,'^LEASE_PROTOCOL_INVALID$'):
            serve_lease(f.Store(),lambda:{},lambda:[],io.BytesIO(b'{"version":1}\n'),io.BytesIO(),Mock())


class Plans(unittest.TestCase):
    def plan(self,actions=['no-op']):return {'resource_changes':[{'address':'aws_instance.host','mode':'managed',
        'change':{'actions':actions,'before':{'id':'i-'+'a'*17},'after':{'id':'i-'+'a'*17}}}]}
    def test_existing_target_only(self):validate_runtime_target(self.plan(),'i-'+'a'*17)
    def test_replacement_rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^GUARDED_INSTANCE_CHANGE_REJECTED$'):validate_runtime_target(self.plan(['delete','create']),'i-'+'a'*17)
    def test_instance_update_requires_separate_review(self):
        with self.assertRaisesRegex(CaptureFailure,'^GUARDED_INSTANCE_CHANGE_REJECTED$'):validate_runtime_target(self.plan(['update']),'i-'+'a'*17)
    def test_other_instance_rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^GUARDED_INSTANCE_CHANGE_REJECTED$'):validate_runtime_target(self.plan(),'i-'+'b'*17)
    def test_volume_delete_rejected(self):
        p=self.plan();p['resource_changes'].append({'address':'aws_ebs_volume.unsafe','change':{'actions':['delete']}})
        with self.assertRaisesRegex(CaptureFailure,'^RUNTIME_DESTRUCTIVE_CHANGE_REJECTED$'):validate_runtime_target(p,'i-'+'a'*17)
    def test_missing_target_rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^GUARDED_INSTANCE_MISSING$'):validate_runtime_target({'resource_changes':[]},'i-'+'a'*17)


class Database(unittest.TestCase):
    def fence(self):
        store=f.Store();pg=SimpleNamespace(database='synthetic',container='a'*64,supervisor=Mock(),sessions=lambda:[])
        obj=database_fence.DatabaseFence(store,f.CAPTURE,pg,'/unused')
        obj.read=lambda:database_fence.RULES+b'original'
        store.write(obj.relative,dict(operation_id=f.CAPTURE,phase='held',fenced_sha256=hashlib.sha256(obj.read()).hexdigest()))
        return obj,store,pg
    def test_tcp_success_rejects_fence(self):
        obj,_,_=self.fence()
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_TCP_FENCE_NOT_EFFECTIVE$'):obj.assert_fenced()
    def test_no_password_error_is_not_auth_denial(self):
        obj,_,pg=self.fence()
        pg.supervisor.execute.side_effect=CaptureFailure('EXEC_FAILED')
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_AUTH_PROBE_AMBIGUOUS$'):obj.assert_fenced()
    def test_connection_loss_is_not_auth_denial(self):
        obj,_,pg=self.fence();pg.supervisor.execute.side_effect=CaptureFailure('EXEC_INSPECTION_REQUIRED')
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_AUTH_PROBE_AMBIGUOUS$'):obj.assert_fenced()
    def test_exact_denial_validated(self):
        obj,_,pg=self.fence()
        def fail(*args):args[4].write_stderr(b'FATAL: pg_hba.conf rejects connection for host synthetic');raise CaptureFailure('EXEC_FAILED')
        pg.supervisor.execute.side_effect=fail;obj.assert_fenced()
    def test_file_changed_rejected(self):
        obj,_,_=self.fence();obj.read=lambda:b'changed'
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_FENCE_LOST$'):obj.assert_fenced()
    def test_existing_sessions_rejected(self):
        obj,_,pg=self.fence();pg.sessions=lambda:[99]
        with self.assertRaisesRegex(CaptureFailure,'^DATABASE_WRITER_NOT_FENCED$'):obj.assert_fenced()
    def test_restore_does_not_overwrite_other_edit(self):
        import base64
        obj,store,_=self.fence();r=store.read(obj.relative);r.update(before_base64=base64.b64encode(b'original').decode(),
            before_sha256=hashlib.sha256(b'original').hexdigest());store.write(obj.relative,r);obj.locate=lambda:(b'legitimate-edit',None)
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_HBA_CONCURRENT_CHANGE$'):obj.restore()


class StoragePreflight(unittest.TestCase):
    def api(self):
        api=Mock();api.get_bucket_location.return_value={'LocationConstraint':None}
        api.get_bucket_versioning.return_value={'Status':'Enabled'}
        api.get_bucket_encryption.return_value={'ServerSideEncryptionConfiguration':{'Rules':[
            {'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}}
        return api
    def test_both_buckets_checked_without_writes(self):
        api=self.api();host_worker.storage_preflight(api,config())
        self.assertEqual(api.get_bucket_versioning.call_count,2)
        for call in api.mock_calls:self.assertTrue(call[0].startswith('get_bucket_'))
    def test_versioning_suspended_blocks(self):
        api=self.api();api.get_bucket_versioning.return_value={'Status':'Suspended'}
        with self.assertRaisesRegex(CaptureFailure,'^STORAGE_VERSIONING_REQUIRED$'):host_worker.storage_preflight(api,config())
    def test_unexpected_kms_blocks(self):
        api=self.api();api.get_bucket_encryption.return_value['ServerSideEncryptionConfiguration']['Rules'][0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']='aws:kms'
        with self.assertRaisesRegex(CaptureFailure,'^STORAGE_ENCRYPTION_MISMATCH$'):host_worker.storage_preflight(api,config())
    def test_wrong_region_blocks(self):
        api=self.api();api.get_bucket_location.return_value={'LocationConstraint':'eu-west-1'}
        with self.assertRaisesRegex(CaptureFailure,'^STORAGE_REGION_MISMATCH$'):host_worker.storage_preflight(api,config())
    def test_permission_failure_sanitized(self):
        api=self.api();api.get_bucket_versioning.side_effect=RuntimeError('synthetic-sensitive')
        with self.assertRaisesRegex(CaptureFailure,'^STORAGE_PREFLIGHT_FAILED$'):host_worker.storage_preflight(api,config())


class RunnerContext(unittest.TestCase):
    def test_backup_returns_only_validated_public_identity(self):
        from contextlib import contextmanager
        @contextmanager
        def connected(*args):
            yield Mock(request=lambda request:{'code':'BACKUP_INTEGRITY_VERIFIED','backup_id':f.OP,
                'backup_complete':True,'restore_proven':False,'destroy_authorized':False,'untrusted_detail':'synthetic-sensitive'}),\
                SimpleNamespace(account='123456789012',region='us-east-1',instance='i-'+'a'*17),{},'1:1',{}
        with patch.object(runner,'connection',connected):result=runner.backup()
        self.assertEqual(result['backup_id'],f.OP);self.assertNotIn('untrusted_detail',result)
    def test_missing_backup_identity_is_not_success(self):
        from contextlib import contextmanager
        from scripts.backup.sealing.common import SealFailure
        @contextmanager
        def connected(*args):
            yield Mock(request=lambda request:{'code':'BACKUP_INTEGRITY_VERIFIED','backup_complete':True,
                'restore_proven':False,'destroy_authorized':False}),\
                SimpleNamespace(account='123456789012',region='us-east-1',instance='i-'+'a'*17),{},'1:1',{}
        with patch.object(runner,'connection',connected),self.assertRaisesRegex(SealFailure,'^INVALID_BACKUP_ID$'):runner.backup()

    def context(self):return {'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','DEPLOY_BRANCH':'main',
        'DEPLOY_ENVIRONMENT':'lab','PROJECT_NAME':'synthetic','AWS_ACCOUNT_ID':'123456789012','AWS_REGION':'us-east-1',
        'RECOVERY_INSTANCE_ID':'i-'+'a'*17,'DATA_BUCKET_NAME':'synthetic-data','BACKUPS_BUCKET_NAME':'synthetic-backups',
        'STATE_BUCKET_NAME':'synthetic-state','BACKUP_ROLE_ARN':'arn:aws:iam::123456789012:role/synthetic-lab-backup',
        'CONFIRMATION':'BACKUP','GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}
    def invoke(self,changes):
        with patch.dict(os.environ,{**self.context(),**changes},clear=True),patch.object(runner.sys,'platform','linux'),patch.object(runner.sys,'version_info',(3,12)):
            return runner.configuration('backup')
    def test_manual_context(self):self.assertEqual(self.invoke({})[0].role_name,'synthetic-lab-backup')
    def test_wrong_branch(self):
        with self.assertRaisesRegex(CaptureFailure,'^MANUAL_BRANCH_REQUIRED$'):self.invoke({'GITHUB_REF':'refs/heads/other'})
    def test_push_forbidden(self):
        with self.assertRaisesRegex(CaptureFailure,'^MANUAL_BRANCH_REQUIRED$'):self.invoke({'GITHUB_EVENT_NAME':'push'})
    def test_deploy_role_forbidden(self):
        with self.assertRaisesRegex(CaptureFailure,'^DEDICATED_ROLE_REQUIRED$'):self.invoke({'BACKUP_ROLE_ARN':'arn:aws:iam::123456789012:role/synthetic-lab-github'})
    def test_confirmation_forbidden(self):
        with self.assertRaisesRegex(CaptureFailure,'^EXPLICIT_CONFIRMATION_REQUIRED$'):self.invoke({'CONFIRMATION':'APPLY'})
    def test_state_alias_forbidden(self):
        with self.assertRaisesRegex(CaptureFailure,'^STORAGE_SEPARATION$'):self.invoke({'STATE_BUCKET_NAME':'synthetic-data'})


class Broker(unittest.TestCase):
    def setUp(self):
        self.store=f.Store();self.api=f.API(self.store);self.broker=AdministrativeBroker(self.store,self.api,f.SCOPE)
    def invoke(self,command,document=None):return self.broker.invoke(42,{'version':1,'command':command,'operation':f.OP,'document':document})
    def test_no_kernel_lock_no_mutation(self):
        with patch('scripts.backup.operational.admin_broker.kernel_lock_owned',return_value=False),self.assertRaisesRegex(CaptureFailure,'^ADMIN_KERNEL_LOCK_REQUIRED$'):
            self.invoke('acquire')
        self.assertEqual(self.api.writes,[])
    def test_arbitrary_command_forbidden(self):
        for command in ('delete_policy','put_object','assume_role','start_capture'):
            with self.subTest(command=command),self.assertRaisesRegex(CaptureFailure,'^ADMIN_COMMAND_REJECTED$'):self.invoke(command)
    def test_second_peer_blocked(self):
        self.broker.owner=(43,f.OP)
        with patch('scripts.backup.operational.admin_broker.kernel_lock_owned',return_value=True),self.assertRaisesRegex(CaptureFailure,'^ADMIN_AUTHORITY_OWNER_MISMATCH$'):
            self.invoke('assert')
    def test_read_interface_has_no_mutation(self):
        self.invoke('read_policy');self.assertEqual(self.api.writes,[])
    def test_read_command_cannot_smuggle_document(self):
        with self.assertRaisesRegex(CaptureFailure,'^ADMIN_PROTOCOL_INVALID$'):self.invoke('read_policy',{'unsafe':True})


class StaticBoundaries(unittest.TestCase):
    def test_qualification_dependency_closure_separate_from_runtime(self):
        from scripts.backup.operational.dependencies import audit
        runtime={r['name']:r for r in audit()};qualified={r['name']:r for r in audit('qualification')}
        self.assertEqual(len(runtime),20);self.assertEqual(len(qualified),28)
        self.assertEqual({name:qualified[name] for name in runtime},runtime)
        self.assertEqual(runtime['pyyaml']['version'],'6.0.3');self.assertNotIn('ansible-core',runtime);self.assertEqual(qualified['ansible-core']['version'],'2.19.14')
    def test_unknown_dependency_profile_rejected(self):
        from scripts.backup.operational.dependencies import audit
        with self.assertRaisesRegex(CaptureFailure,'^DEPENDENCY_PROFILE_REJECTED$'):audit('operational_allowed')
    def test_host_setup_bootstraps_venv_before_audit(self):
        from pathlib import PurePosixPath
        with patch.object(host_setup.os,'name','posix'),patch.object(host_setup.os,'geteuid',return_value=0,create=True),\
             patch.object(host_setup.sys,'version_info',(3,12)),patch.object(host_setup,'Path',PurePosixPath),\
             patch.object(host_setup,'audit',side_effect=AssertionError('system pip must not be imported')),\
             patch.object(host_setup.subprocess,'run') as run:
            host_setup.install()
        self.assertEqual(len(run.call_args_list),2)
        self.assertEqual(run.call_args_list[0].args[0][1:4],['-I','-m','venv'])
        self.assertIn('/venv/bin/python',run.call_args_list[1].args[0][0])
        self.assertEqual(run.call_args_list[1].args[0][-1],'verified-install')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',run.call_args_list[1].kwargs['env'])
    def test_venv_creation_failure_has_no_install_fallback(self):
        import subprocess
        from pathlib import PurePosixPath
        with patch.object(host_setup.os,'name','posix'),patch.object(host_setup.os,'geteuid',return_value=0,create=True),\
             patch.object(host_setup.sys,'version_info',(3,12)),patch.object(host_setup,'Path',PurePosixPath),\
             patch.object(host_setup.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['synthetic-venv'])) as run:
            with self.assertRaises(subprocess.CalledProcessError):host_setup.install()
        self.assertEqual(run.call_count,1)
    def test_readonly_helper_code_readable_without_exposing_host_configuration(self):
        import yaml
        tasks=yaml.safe_load((ROOT/'ansible/roles/recovery_operational/tasks/main.yml').read_text())
        collector=[t['ansible.builtin.file'] for t in tasks if t.get('ansible.builtin.file',{}).get('path','').endswith('/read-state.php')]
        self.assertEqual(len(collector),1);self.assertEqual(collector[0]['mode'],'0644')
        config=[t['ansible.builtin.copy'] for t in tasks if t.get('ansible.builtin.copy',{}).get('dest')=='/etc/nextcloud-recovery/config.json']
        self.assertEqual(config[0]['mode'],'0600')
        helper=(ROOT/'scripts/backup/operational/host_probe.py').read_text()
        self.assertIn("user='www-data'",helper);self.assertIn("network_mode='none'",helper)

    def test_backup_and_deploy_concurrency_literal_equal(self):
        import yaml
        workflow=yaml.safe_load((ROOT/'.github/workflows/backup-operational.yml').read_text())
        deploy=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())
        self.assertEqual(workflow['jobs']['backup']['concurrency'],deploy['jobs']['deploy']['concurrency'])
        self.assertFalse(workflow['jobs']['backup']['concurrency']['cancel-in-progress'])
    def test_backup_manual_only(self):
        import yaml
        value=yaml.safe_load((ROOT/'.github/workflows/backup-operational.yml').read_text())
        self.assertEqual(set(value[True]),{'workflow_dispatch'})
        self.assertEqual(value['permissions'],{'contents':'read'})
    def test_backup_never_gets_admin_session(self):
        text=(ROOT/'.github/workflows/backup-operational.yml').read_text()+(ROOT/'scripts/backup/operational/runner.py').read_text()
        for item in ('assume_admin','ADMIN_ROLE_ARN','PutBucketPolicy','configure-aws-credentials','upload-artifact'):
            self.assertNotIn(item,text)
    def test_forced_ssh_has_no_admin_dispatch(self):
        text=(ROOT/'scripts/backup/operational/nextcloud-recovery-ssh').read_text()
        self.assertNotIn('admin_broker',text);self.assertEqual(text.count('exec sudo -n'),2)
    def test_systemd_controls_group_not_capture_success(self):
        text=(ROOT/'scripts/backup/operational/host_dispatch.py').read_text()
        self.assertIn('KillMode=control-group',text);self.assertIn('RuntimeMaxSec=2700',text)
        self.assertNotIn('backup_complete=True',text)
    def test_no_private_age_key_paths(self):
        for name in ('runner.py','host_worker.py','completion.py'):
            tree=ast.parse((ROOT/'scripts/backup/operational'/name).read_text())
            values=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
            self.assertFalse(any('AGE-SECRET-KEY-' in v or v=='age-keygen' for v in values))
    def test_deploy_is_one_continuous_guard(self):
        text=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertEqual(text.count('scripts.backup.operational.runner deploy'),1)
        self.assertNotIn('scripts/ci/deploy.py connect',text)
    def test_pinned_wheel_closure(self):
        from scripts.backup.operational.dependencies import audit
        self.assertEqual(len(audit()),20)


@unittest.skipUnless(__import__('sys').platform=='linux','new PR7 Linux kernel/process qualification pending')
class LinuxKernelQualification(unittest.TestCase):
    """Actual kernel/child tests, only generated temp data; no Docker or AWS."""
    def setUp(self):
        import tempfile
        from scripts.backup.capture.storage import Store
        self.temp=tempfile.TemporaryDirectory(prefix='pr7-disposable-test-');self.addCleanup(self.temp.cleanup)
        os.chmod(self.temp.name,0o700);self.store=Store(self.temp.name)
    def test_kernel_flock_owner_is_actual_process(self):
        from scripts.backup.operational.admin_broker import kernel_lock_owned
        with self.store.lock():
            self.assertTrue(kernel_lock_owned(self.store,os.getpid()))
            self.assertFalse(kernel_lock_owned(self.store,999999999))
        self.assertFalse(kernel_lock_owned(self.store,os.getpid()))
    def test_independent_process_cannot_enter_held_lock(self):
        import subprocess,sys
        code="import fcntl,sys; f=open(sys.argv[1],'r+');\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(3)"
        def attempt():return subprocess.run([sys.executable,'-I','-c',code,str(self.store.root/'capture.lock')],
            env={'PATH':'/usr/bin:/bin'},stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode
        with self.store.lock():self.assertEqual(attempt(),3)
        self.assertEqual(attempt(),0)
    def test_sigkill_of_disposable_child_leaves_durable_deploy_block(self):
        import subprocess,sys,selectors
        from scripts.backup.operational.lease import assert_deployment_clear
        code="import sys,signal; from scripts.backup.capture.storage import Store; from scripts.backup.operational.lease import deploy_lease; s=Store(sys.argv[1]);\nwith deploy_lease(s,'123:1',lambda:[]):\n print('HELD',flush=True)\n signal.pause()"
        child=subprocess.Popen([sys.executable,'-c',code,str(self.store.root)],cwd=ROOT,
            env={'PATH':'/usr/bin:/bin','PYTHONNOUSERSITE':'1'},stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout,selectors.EVENT_READ);self.assertTrue(selector.select(5))
                self.assertEqual(child.stdout.readline(),b'HELD\n')
            child.kill();child.wait(timeout=5)
            # Kernel releases flock; durable ownership still prohibits deploy.
            with self.store.lock():pass
            with self.assertRaisesRegex(CaptureFailure,'^DEPLOY_LEASE_RECOVERY_REQUIRED$'):assert_deployment_clear(self.store)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdout.close()


if __name__=='__main__':unittest.main()
