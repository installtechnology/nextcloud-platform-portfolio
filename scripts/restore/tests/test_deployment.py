"""Offline preparation-choice tests: no subprocess, Docker, IMDS or AWS execution."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import tarfile
import hashlib
from unittest.mock import patch

from scripts.ci import deploy
from scripts.restore.deployment import validate_preparation, export_reviewed, package_reviewed
from scripts.restore.prepare_identity import check_empty_files
from scripts.restore.tests import test_host_preparation as fixtures
from scripts.backup.sealing.common import SealFailure
from scripts.backup.operational.remote_lease import RemoteDeployLease, AuthenticatedHostChannel
from scripts.backup.operational.ssm import Target

ROOT = fixtures.ROOT


class Deployment(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.config()
        self.scope = (self.c['account'], self.c['region'], self.c['project'],
                      [self.c[k] for k in ('data_bucket', 'backups_bucket', 'state_bucket')], self.c['instance'], ROOT)

    def validate(self, c=None):
        return validate_preparation(json.dumps(c or self.c), *self.scope)

    def test_explicit_valid_target(self): self.assertEqual(self.validate(), self.c)
    def test_unknown_purpose(self):
        with patch.dict(os.environ, {'DEPLOY_PURPOSE': 'prepare_restore; echo unsafe'}):
            with self.assertRaisesRegex(ValueError, '^DEPLOY_PURPOSE_INVALID$'): deploy.deployment_purpose()
    def test_default_keeps_new_installation(self):
        with patch.dict(os.environ, {}, clear=True): self.assertEqual(deploy.deployment_purpose(), 'new_installation')
    def test_recovery_is_separate_from_terraform_mode(self):
        with patch.dict(os.environ, {'DEPLOY_PURPOSE': 'prepare_restore', 'DEPLOY_MODE': 'plan'}):
            self.assertEqual(deploy.deployment_purpose(), 'prepare_restore')
    def test_source_instance_rejected(self):
        c=copy.deepcopy(self.c); c['instance']=c['source_instance']
        with self.assertRaisesRegex(SealFailure, '^RESTORE_HOST_SOURCE_REJECTED$'): self.validate(c)
    def test_other_account_rejected(self):
        c=copy.deepcopy(self.c); c['account']='111111111111'
        with self.assertRaisesRegex(SealFailure, '^RESTORE_PREPARATION_SCOPE_MISMATCH$'): self.validate(c)
    def test_other_instance_rejected(self):
        c=copy.deepcopy(self.c); c['instance']='i-'+'3'*17
        with self.assertRaisesRegex(SealFailure, '^RESTORE_PREPARATION_SCOPE_MISMATCH$'): self.validate(c)
    def test_other_destination_bucket_rejected(self):
        c=copy.deepcopy(self.c); c['data_bucket']='fixture-other'
        with self.assertRaisesRegex(SealFailure, '^RESTORE_PREPARATION_BUCKET_MISMATCH$'): self.validate(c)
    def test_source_bucket_rejected(self):
        c=copy.deepcopy(self.c); c['data_bucket']=c['source_data_bucket']
        with self.assertRaisesRegex(SealFailure, '^RESTORE_HOST_STORAGE_SEPARATION$'): self.validate(c)
    def test_different_but_pinned_image_rejected(self):
        c=copy.deepcopy(self.c); c['images']['nextcloud']='nextcloud:other@sha256:'+'f'*64
        with self.assertRaisesRegex(SealFailure, '^RESTORE_PREPARATION_IMAGE_MISMATCH$'): self.validate(c)
    def test_duplicate_json_rejected(self):
        raw=json.dumps(self.c)[:-1]+',"instance":"'+self.c['instance']+'"}'
        with self.assertRaises(Exception): validate_preparation(raw,*self.scope)
    def test_oversized_json_rejected(self):
        with self.assertRaises(Exception): validate_preparation(' '*65537,*self.scope)
    def test_only_committed_reviewed_trees_are_exported(self):
        commit='a'*40; bodies={}; records=[]
        for i, prefix in enumerate(('scripts/restore', 'scripts/backup', 'contracts/recovery', 'docker')):
            digest=str(i)*40; bodies[digest]=b'synthetic'; records.append(f'100644 blob {digest}\t{prefix}/fixture.txt'.encode())
        calls=[]
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1]=='rev-parse': raw=(commit+'\n').encode()
            elif argv[1]=='ls-tree': raw=b'\0'.join(records)+b'\0'
            elif argv[2]=='-s': raw=str(len(bodies[argv[3]])).encode()
            else: raw=bodies[argv[3]]
            return type('Result', (), {'returncode':0,'stdout':raw})()
        with tempfile.TemporaryDirectory() as temp, patch('scripts.restore.deployment.subprocess.run', side_effect=run):
            target=Path(temp)/'export'; export_reviewed(ROOT,target,commit)
            self.assertEqual(len(list(target.rglob('*.txt'))),4)
            self.assertFalse(any('private' in str(p) for p in target.rglob('*')))
        self.assertTrue(all(c[0]=='git' for c in calls))
    def test_export_revision_mismatch_rejected(self):
        with patch('scripts.restore.deployment.subprocess.run') as run:
            run.return_value.returncode=0; run.return_value.stdout=b'b'*40+b'\n'
            with self.assertRaisesRegex(SealFailure, '^RESTORE_CODE_REVISION_MISMATCH$'):
                export_reviewed(ROOT,Path('not-created'),'a'*40)

    def test_single_archive_preserves_bytes_and_private_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'export'; (root/'scripts/restore').mkdir(parents=True)
            (root/'scripts/restore/fixture.py').write_bytes(b'synthetic\n')
            path, checksum=package_reviewed(root,Path(temp)/'export.tar')
            self.assertEqual(checksum,hashlib.sha256(Path(path).read_bytes()).hexdigest())
            with tarfile.open(path) as bundle:
                for member in bundle.getmembers():
                    self.assertEqual(member.mode,0o700 if member.isdir() else 0o600)
                    self.assertEqual((member.uid,member.gid),(0,0))
                    self.assertFalse(member.issym() or member.islnk())
                self.assertEqual(bundle.extractfile('scripts/restore/fixture.py').read(),b'synthetic\n')

    def test_archive_rejects_unexpected_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'export';root.mkdir();(root/'private.txt').write_text('synthetic')
            with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_ENTRY_INVALID$'):
                package_reviewed(root,Path(temp)/'export.tar')
            self.assertFalse((Path(temp)/'export.tar').exists())

    def test_archive_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'export';root.mkdir();archive=Path(temp)/'export.tar';archive.write_bytes(b'preserve')
            with self.assertRaises(FileExistsError):package_reviewed(root,archive)
            self.assertEqual(archive.read_bytes(),b'preserve')

    def test_archive_integrity_precedes_remote_extraction(self):
        import yaml
        tasks=yaml.safe_load((ROOT/'ansible/roles/restore_prepare/tasks/main.yml').read_text())
        copy_task=next(t for t in tasks if t['name']=='Copy only the reviewed Git export for restore tooling')
        self.assertNotIn('loop',copy_task)
        self.assertEqual(copy_task['ansible.builtin.copy']['src'],'{{ restore_reviewed_code_archive }}')
        check=next(i for i,t in enumerate(tasks) if t['name']=='Reject changed archive bytes before extraction')
        extract=next(i for i,t in enumerate(tasks) if t['name']=='Extract only the verified reviewed tooling archive')
        self.assertLess(check,extract)
        self.assertTrue(tasks[extract]['ansible.builtin.unarchive']['remote_src'])
    def test_summary_does_not_claim_application_running(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'summary'
            with patch.dict(os.environ, {'DEPLOY_PURPOSE':'prepare_restore','GITHUB_STEP_SUMMARY':str(file)}): deploy.summary()
            s=file.read_text(); self.assertIn('not installed or started',s); self.assertNotIn('http://',s)
    def test_workflow_choice_and_no_shell_input_interpolation(self):
        text=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn('options: [new_installation, prepare_restore, create_restore_target]',text)
        self.assertIn('DEPLOY_PURPOSE: ${{ inputs.purpose }}',text)
        self.assertNotIn('run: ${{ inputs.purpose }}',text)
        self.assertIn('cancel-in-progress: false',text)
    def test_recovery_never_selects_application_playbook(self):
        text=(ROOT/'scripts/ci/deploy.py').read_text()
        self.assertIn("args += ['playbooks/restore-prepare.yml'",text)
        self.assertIn('RESTORE_PREPARATION_RUNTIME_MISMATCH',text)
        self.assertIn('RESTORE_PREPARATION_LEASE_MISMATCH',text)
    def test_missing_restore_config_aborts_prepare(self):
        env=dict(AWS_ACCOUNT_ID=self.c['account'],AWS_REGION=self.c['region'],PROJECT_NAME=self.c['project'],
                 DEPLOY_ENVIRONMENT='lab',DATA_BUCKET_NAME=self.c['data_bucket'],BACKUPS_BUCKET_NAME=self.c['backups_bucket'],
                 STATE_BUCKET_NAME=self.c['state_bucket'],GITHUB_REF='refs/heads/main',DEPLOY_BRANCH='main',DEPLOY_MODE='plan',
                 DEPLOY_PURPOSE='prepare_restore')
        with patch.dict(os.environ,env,clear=True),patch.object(deploy,'private_file') as write:
            with self.assertRaisesRegex(ValueError,'RESTORE_HOST_CONFIGURATION'):deploy.prepare()
            write.assert_not_called()

    def test_new_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as temp:check_empty_files(Path(temp)/'new')
    def test_prepared_empty_directories_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ('docker','monitoring','data','backups'):(Path(temp)/name).mkdir()
            check_empty_files(temp)
    def test_existing_configuration_blocks_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'docker';directory.mkdir();(directory/'compose.yml').write_text('synthetic')
            with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_NONEMPTY_TARGET$'):check_empty_files(temp)
    def test_unexpected_directory_blocks_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'existing').mkdir()
            with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_NONEMPTY_TARGET$'):check_empty_files(temp)

    def guarded(self, purpose, runtime_instance=None):
        class Channel(AuthenticatedHostChannel):
            def request(self, request):
                return {**{k:v for k,v in request.items() if k!='credentials'},
                        'phase':'released' if request['command']=='release' else 'held'}
        c=self.c
        scope=dict(project=c['project'],environment='lab',data_bucket=c['data_bucket'],
                   backups_bucket=c['backups_bucket'],state_bucket=c['state_bucket'])
        lease=RemoteDeployLease(Channel(),Target(c['account'],c['region'],c['instance'],c['project']+'-lab-deploy-guard'),
                               '123:1',scope)
        with tempfile.TemporaryDirectory() as temp:
            env=dict(AWS_ACCOUNT_ID=c['account'],AWS_REGION=c['region'],PROJECT_NAME=c['project'],
                     DEPLOY_ENVIRONMENT='lab',DATA_BUCKET_NAME=c['data_bucket'],BACKUPS_BUCKET_NAME=c['backups_bucket'],
                     STATE_BUCKET_NAME=c['state_bucket'],GITHUB_REF='refs/heads/main',DEPLOY_BRANCH='main',DEPLOY_MODE='apply',
                     DEPLOY_PURPOSE=purpose,CONFIRMATION='APPLY',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',
                     RUNNER_TEMP=temp,GITHUB_SHA='a'*40)
            def terraform(active_lease):
                deploy.require_deploy_lease(active_lease)
                deploy.private_file(deploy.paths()/'runtime.json',json.dumps({'instance_id':runtime_instance or c['instance'],
                                    'data_bucket_name':c['data_bucket']}))
            with patch.dict(os.environ,env,clear=True),patch.object(deploy,'preflight'),\
                 patch.object(deploy,'restore_preparation',return_value=c),\
                 patch('scripts.restore.deployment.preparation_variables',return_value={}),\
                 patch.object(deploy,'terraform',side_effect=terraform),patch.object(deploy,'connect'),\
                 patch.object(deploy,'cleanup'),patch.object(deploy,'run') as run:
                deploy.guarded_deploy(lease)
                return run.call_args.args[0]
    def test_actual_dispatch_selects_only_restore_preparation(self):
        argv=self.guarded('prepare_restore')
        self.assertIn('playbooks/restore-prepare.yml',argv);self.assertNotIn('playbooks/deploy.yml',argv)
        self.assertIn('--extra-vars',argv)
    def test_actual_default_dispatch_keeps_normal_deploy(self):
        argv=self.guarded('new_installation')
        self.assertIn('playbooks/deploy.yml',argv);self.assertNotIn('playbooks/restore-prepare.yml',argv)
    def test_actual_dispatch_blocks_runtime_identity_mismatch(self):
        with self.assertRaisesRegex(RuntimeError,'^RESTORE_PREPARATION_RUNTIME_MISMATCH$'):
            self.guarded('prepare_restore','i-'+'4'*17)


if __name__=='__main__': unittest.main()
