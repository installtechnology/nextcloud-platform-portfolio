"""Dedicated role/protocol/workflow tests; no AWS, SSH, SSM, host or restore calls."""
import copy
from datetime import datetime,timezone,timedelta
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
import yaml
from scripts.restore.access import validate_access
from scripts.restore.host_worker import parse_request,completed_operation
from scripts.restore.sequence import STEPS
from scripts.restore import runner,host_dispatch,host_worker
from scripts.restore.prepare_images import prepare
from scripts.restore.tests import test_host_preparation as fixtures
from scripts.backup.sealing.common import SealFailure
from scripts.backup.capture.storage import CaptureFailure

ROOT=fixtures.ROOT


def access():
    c=fixtures.config()
    return {'version':1,'repository':'fixture/platform','branch':'main','github_environment':'lab',
        'resource_environment':'restore','role_arn':f"arn:aws:iam::{c['account']}:role/{c['project']}-lab-restore",
        'source':{'account_id':c['account'],'region':c['region'],'project':c['project'],'environment':'lab',
                  'data_bucket':c['source_data_bucket'],'installation_identity_sha256':'a'*64}}


def environment():
    c=fixtures.config();a=access()
    return {'AWS_ACCOUNT_ID':c['account'],'AWS_REGION':c['region'],'PROJECT_NAME':c['project'],
        'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':a['repository'],
        'GITHUB_SHA':'b'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','CONFIRMATION':'RESTORE',
        'RESTORE_GITHUB_ENVIRONMENT':'lab','RESTORE_RESOURCE_ENVIRONMENT':'restore','RESTORE_ROLE_ARN':a['role_arn'],
        'RESTORE_INSTANCE_ID':c['instance'],'RESTORE_SOURCE_INSTANCE_ID':c['source_instance'],
        'RESTORE_SELECTION':'specific','RESTORE_BACKUP_ID':'20261011T120000Z-22222222-2222-4333-8444-555555555555'}


def request():
    c=fixtures.config();a=access();e=environment()
    return {'version':1,'owner':'123:1','repository':a['repository'],'commit':'b'*40,'confirmation':'RESTORE',
        'github_environment':'lab','resource_environment':'restore','selection':'specific','backup_id':e['RESTORE_BACKUP_ID'],
        'scope':{k:c[k] for k in ('account','region','instance','source_instance','project')},'public_ip':'8.8.8.8',
        'restore_credentials':{'access_key':'ASIA'+'A'*16,'secret_key':'x'*40,'token':'synthetic-token-no-authentication',
                              'expiration':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}


class Access(unittest.TestCase):
    def setUp(self):self.c=fixtures.config();self.a=access()
    def check(self):return validate_access(self.a,self.c)
    def test_valid_dedicated_binding(self):self.assertEqual(self.check(),self.a)
    def test_deploy_role_rejected(self):
        self.a['role_arn']=self.a['role_arn'].replace('-restore','-github')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_DEDICATED_ROLE_REQUIRED$'):self.check()
    def test_backup_role_rejected(self):
        self.a['role_arn']=self.a['role_arn'].replace('-restore','-backup')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_DEDICATED_ROLE_REQUIRED$'):self.check()
    def test_branch_not_main_rejected(self):
        self.a['branch']='other'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ACCESS_REPOSITORY_REJECTED$'):self.check()
    def test_source_bucket_rejected(self):
        self.a['source']['data_bucket']='other-bucket'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ACCESS_SOURCE_REJECTED$'):self.check()
    def test_identity_missing_rejected(self):
        self.a['source']['installation_identity_sha256']=''
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ACCESS_SOURCE_REJECTED$'):self.check()
    def test_activation_flag_not_accepted(self):
        self.a['operational_allowed']=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ACCESS_CONFIG_REJECTED$'):self.check()


class Runner(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,environment(),clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.platform=patch.object(runner,'sys',SimpleNamespace(platform='linux',version_info=(3,12,0)))
        self.platform.start();self.addCleanup(self.platform.stop)
    def test_valid_config_no_calls(self):
        with patch.object(runner,'assume_oidc') as oidc:
            target,role,r=runner.configuration();self.assertTrue(role.endswith('-restore'));self.assertNotEqual(r['scope']['instance'],r['scope']['source_instance'])
            oidc.assert_not_called()
    def test_latest_valid_does_not_mix_id(self):
        os.environ['RESTORE_SELECTION']='latest_valid'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SELECTION_AMBIGUOUS$'):runner.configuration()
    def test_latest_valid_empty_id_accepted(self):
        os.environ.update(RESTORE_SELECTION='latest_valid',RESTORE_BACKUP_ID='')
        self.assertEqual(runner.configuration()[2]['selection'],'latest_valid')
    def test_specific_missing_id_fails(self):
        os.environ['RESTORE_BACKUP_ID']=''
        with self.assertRaises(SealFailure):runner.configuration()
    def test_role_cannot_be_reused(self):
        os.environ['RESTORE_ROLE_ARN']=access()['role_arn'].replace('-restore','-backup')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_DEDICATED_ROLE_REQUIRED$'):runner.configuration()
    def test_original_instance_rejected(self):
        os.environ['RESTORE_INSTANCE_ID']=os.environ['RESTORE_SOURCE_INSTANCE_ID']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SOURCE_INSTANCE_REJECTED$'):runner.configuration()
    def test_wrong_confirmation_rejected(self):
        os.environ['CONFIRMATION']='BACKUP'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_MANUAL_CONFIRMATION_REQUIRED$'):runner.configuration()
    def test_nonmanual_event_rejected(self):
        os.environ['GITHUB_EVENT_NAME']='push'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_MANUAL_CONFIRMATION_REQUIRED$'):runner.configuration()
    def test_wrong_branch_rejected(self):
        os.environ['GITHUB_REF']='refs/heads/other'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_MANUAL_CONFIRMATION_REQUIRED$'):runner.configuration()
    def test_invalid_config_rejected_before_oidc(self):
        os.environ['RESTORE_INSTANCE_ID']=''
        with patch.object(runner,'assume_oidc') as oidc:
            with self.assertRaises(SealFailure):runner.restore()
            oidc.assert_not_called()


class Protocol(unittest.TestCase):
    def setUp(self):self.r=request();self.c=fixtures.config();self.a=access()
    def check(self):return parse_request(json.dumps(self.r).encode(),self.c,self.a,'b'*40)
    def test_valid_request(self):self.assertEqual(self.check()['scope']['instance'],self.c['instance'])
    def test_foreign_repository_rejected(self):
        self.r['repository']='other/repository'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REQUEST_BINDING_REJECTED$'):self.check()
    def test_stale_host_code_rejected(self):
        self.r['commit']='c'*40
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REQUEST_BINDING_REJECTED$'):self.check()
    def test_other_target_rejected(self):
        self.r['scope']['instance']='i-'+'9'*17
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REQUEST_SCOPE_REJECTED$'):self.check()
    def test_extra_command_rejected(self):
        self.r['command']='shell'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REQUEST_REJECTED$'):self.check()
    def test_latest_specific_mix_rejected(self):
        self.r['selection']='latest_valid'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SELECTION_AMBIGUOUS$'):self.check()
    def test_confirmation_rejected(self):
        self.r['confirmation']='APPLY'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REQUEST_REJECTED$'):self.check()
    def test_host_private_ip_rejected(self):
        self.r['public_ip']='127.0.0.1'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_PUBLIC_HOST_REJECTED$'):self.check()


class Workflow(unittest.TestCase):
    def test_manual_and_permissions(self):
        d=yaml.safe_load((ROOT/'.github/workflows/restore.yml').read_text())
        self.assertEqual(set(d.get('on',d.get(True))),{'workflow_dispatch'})
        self.assertEqual(d['permissions'],{'contents':'read'})
        job=d['jobs']['restore'];self.assertEqual(job['permissions'],{'contents':'read','id-token':'write'})
        self.assertIn("inputs.confirmation == 'RESTORE'",job['if']);self.assertFalse(job['concurrency']['cancel-in-progress'])
    def test_concurrency_equals_deploy(self):
        a=yaml.safe_load((ROOT/'.github/workflows/restore.yml').read_text())['jobs']['restore']['concurrency']
        b=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())['jobs']['deploy']['concurrency']
        self.assertEqual(a,b)
    def test_no_source_secrets_or_artifacts(self):
        text=(ROOT/'.github/workflows/restore.yml').read_text()
        self.assertNotIn('secrets.RECOVERY_SSH_PRIVATE_KEY',text);self.assertNotIn('upload-artifact',text)
        steps=yaml.safe_load(text)['jobs']['restore']['steps']
        self.assertFalse(any('terraform' in s.get('run','').lower() for s in steps));self.assertNotIn('run: ${{',text)
    def test_forced_command_and_clean_environment(self):
        text=(ROOT/'scripts/restore/nextcloud-restore-ssh').read_text()
        self.assertIn('*) exit 2',text);self.assertNotIn('eval',text)
        self.assertIn('/usr/bin/env -i',(ROOT/'scripts/restore/nextcloud-restore').read_text())
    def test_channel_install_not_in_deploy(self):
        d=yaml.safe_load((ROOT/'ansible/playbooks/restore-channel-prepare.yml').read_text())[0]
        self.assertEqual(d['roles'],['restore_channel'])
        self.assertNotIn('restore-channel-prepare',(ROOT/'.github/workflows/deploy.yml').read_text())
        tasks=yaml.safe_load((ROOT/'ansible/roles/restore_channel/tasks/main.yml').read_text(encoding='utf-8'))
        parent=next(i for i,t in enumerate(tasks) if t['name']=='Prepare root-owned dispatcher directory on a clean destination')
        dispatch=next(i for i,t in enumerate(tasks) if t['name']=='Install reviewed fixed dispatchers without altering backup dispatchers')
        self.assertLess(parent,dispatch)
        self.assertEqual(tasks[parent]['ansible.builtin.file'],{'path':'/usr/local/libexec','state':'directory','owner':'root','group':'root','mode':'0755'})

    def test_host_dispatch_rejects_wrong_mode_before_worker(self):
        with patch.object(host_dispatch,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(host_dispatch,'load') as load:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_COMMAND_REJECTED$'):host_dispatch.dispatch('backup')
            load.assert_not_called()


class RepeatedRestore(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.c=fixtures.config();self.op=environment()['RESTORE_BACKUP_ID'];(self.root/self.op).mkdir()
        self.selected=SimpleNamespace(backup_id=self.op,manifest_sha256='a'*64,completion_version='fixture-version')
        self.record={'phase':'candidate_ready','completed_steps':list(STEPS),'binding':{
            'operation_id':self.op,'backup_id':self.op,'manifest_sha256':'a'*64,'completion_version_id':'fixture-version',
            'destination':{k:self.c[k] for k in ('account','region','instance','data_bucket')}}}
        self.store=SimpleNamespace(root=self.root,operation=lambda x:self.root/x,read=lambda x:self.record)
    def test_same_complete_set_reuses_only_verified_operation(self):
        self.assertEqual(completed_operation(self.store,self.selected,self.c),self.op)
    def test_partial_matching_set_never_resumes(self):
        self.record['phase']='intervention_required'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_INTERRUPTED_REQUIRES_RECONCILIATION$'):
            completed_operation(self.store,self.selected,self.c)
    def test_different_manifest_never_reuses_operation(self):
        self.selected.manifest_sha256='b'*64
        self.assertIsNone(completed_operation(self.store,self.selected,self.c))


class Response(unittest.TestCase):
    def success(self):
        bid=environment()['RESTORE_BACKUP_ID']
        return {'code':'RESTORE_CANDIDATE_READY','operation_id':bid,'backup_id':bid,'restore_proven':False,'destroy_authorized':False}
    def test_success_is_only_candidate(self):
        self.assertFalse(runner.validate_result(self.success())['restore_proven'])
    def test_functional_proof_from_transport_not_accepted(self):
        r=self.success();r['restore_proven']=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REMOTE_RESULT_REJECTED$'):runner.validate_result(r)

    def test_remote_error_code_preserved_without_payload(self):
        r={'code':'RESTORE_POSTGRES_FAILED_PRESERVE_DESTINATION','restore_proven':False,'destroy_authorized':False}
        with self.assertRaisesRegex(CaptureFailure,'^RESTORE_POSTGRES_FAILED_PRESERVE_DESTINATION$'):runner.validate_result(r)
    def test_remote_extra_payload_rejected(self):
        r=self.success();r['details']='synthetic-private'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REMOTE_RESULT_REJECTED$'):runner.validate_result(r)
    def test_unrecognized_error_text_rejected(self):
        r={'code':'synthetic-private','restore_proven':False,'destroy_authorized':False}
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REMOTE_RESULT_REJECTED$'):runner.validate_result(r)


class InstalledGuard(unittest.TestCase):
    def test_mismatched_shared_guard_rejected_before_aws(self):
        c=fixtures.config();a=access();host=SimpleNamespace(config=c,identity=Mock())
        g={'environment':'other','repository':a['repository'],'images':c['images'],
           'deployment_root':c['deployment_root'],'installation_identity':a['source']['installation_identity_sha256']}
        with patch.object(host_worker.runtime_config,'load',return_value=g),patch.object(host_worker,'clients') as sdk:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_SHARED_GUARD_BINDING_REJECTED$'):
                host_worker.execute(host,a,request())
            sdk.assert_not_called()


class ImagePreparation(unittest.TestCase):
    def test_existing_verified_images_not_pulled(self):
        c=fixtures.config();host=SimpleNamespace(config=c,identity=Mock());api=Mock()
        api.containers.return_value=[];api.volumes.return_value={'Volumes':[]}
        api.inspect_image.side_effect=lambda ref:{'RepoDigests':[ref.split(':',1)[0]+'@'+ref.split('@')[1]]}
        self.assertFalse(prepare(host,api));api.pull.assert_not_called()
    def test_existing_container_blocks_preparation(self):
        host=SimpleNamespace(config=fixtures.config(),identity=Mock());api=Mock();api.containers.return_value=[{'Id':'synthetic'}]
        with self.assertRaisesRegex(SealFailure,'^RESTORE_IMAGE_TARGET_NOT_EMPTY$'):prepare(host,api)
        api.pull.assert_not_called()


if __name__=='__main__':unittest.main()
