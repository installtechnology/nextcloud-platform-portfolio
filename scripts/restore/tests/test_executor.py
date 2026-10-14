"""Synthetic private artifacts/PHP configs. No Docker, AWS, SQL or real backup."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import yaml
from scripts.restore.private_files import stage_private,tree_digest,write_new,verified_file
from scripts.restore.runtime import destination_runtime,remap_request,ENV_KEYS
from scripts.restore.parameters import ProtectedParameters,RUNTIME,SECRETS
from scripts.restore.destination import Destination
from scripts.restore.host_runtime import HostRuntime,validate_html_volume
from scripts.restore.control import RestoreControl
from scripts.restore.sequence import STEPS
from scripts.restore.executor import execute_restore
from scripts.restore.s3_reader import DestinationObjects
from scripts.backup.capture.storage import Store,CaptureFailure
from scripts.backup.sealing.common import SealFailure
from scripts.restore.tests import test_host_preparation as host_fixtures

ROOT=host_fixtures.ROOT
PHP=(ROOT/'.validation-tools/php/php.exe') if os.name=='nt' else Path(shutil.which('php') or '/missing-required-php')


def tar_bytes(entries):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as tar:
        item=tarfile.TarInfo('.');item.type=tarfile.DIRTYPE;item.mode=0o750;item.uid=item.gid=33;tar.addfile(item)
        for name,raw in sorted(entries.items()):
            item=tarfile.TarInfo(name);item.mode=0o640;item.uid=item.gid=33
            if raw is None:item.type=tarfile.DIRTYPE;tar.addfile(item)
            else:item.size=len(raw);tar.addfile(item,io.BytesIO(raw))
    return out.getvalue()


def fixture():
    c=host_fixtures.config();recipe=yaml.safe_load((ROOT/'docker/compose.yml').read_text())
    m=json.loads((ROOT/'contracts/recovery/fixtures/sse-s3-v2/complete-unproven.json').read_text())
    m['source']['data_bucket']=c['source_data_bucket'];m['source']['project']=c['project']
    for role in ('nextcloud','postgres'):m['compatibility'][role+'_image']=c['images'][role]
    db={'database':'nextcloud','owner':'nextcloud','extensions':[]}
    env={k:'synthetic' for k in ENV_KEYS}
    env.update(COMPOSE_PROJECT_NAME=c['project'],POSTGRES_DB='nextcloud',POSTGRES_USER='nextcloud',
               NEXTCLOUD_S3_BUCKET=c['source_data_bucket'],NEXTCLOUD_S3_REGION=c['region'],NEXTCLOUD_HTTP_PORT='80')
    for name in SECRETS:env[name.upper()+'_FILE_PATH']='./secrets/'+name
    runtime={name:(ROOT/'docker'/name).read_bytes().replace(b'\r\n',b'\n') for name in RUNTIME}
    p=ProtectedParameters(db,host_fixtures.globals_recipe(),{k:b'synthetic-private' for k in SECRETS},
                          ('\n'.join(k+'='+v for k,v in env.items())+'\n').encode(),runtime)
    return c,m,p


class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.c,self.m,self.p=fixture();b64=lambda b:base64.b64encode(b).decode()
        self.document={'internal_version':1,'kind':'local-recovery-parameters','database':self.p.database,
            'globals_sql_without_passwords':self.p.globals_sql,'images':self.c['images'],
            'capture_binding':{'operation_id':self.m['backup_id'],'consistency_id':self.m['consistency']['id']},
            'secrets_base64':{k:b64(v) for k,v in self.p.secrets.items()},'compose_env_base64':b64(self.p.compose_env),
            'runtime_code_base64':{k:b64(v) for k,v in self.p.runtime_code.items()},
            'recovery_requirement':'synthetic','restore_proven':False}
        self.configuration={'config.php':b'<?php $CONFIG=[];', '.platform-storage.json':b'{}'}
    def artifacts(self,mismatch=False):
        configuration=tar_bytes(self.configuration)
        files={'config':None,**{'config/'+k:v for k,v in self.configuration.items()},'version.php':b'<?php // synthetic'}
        if mismatch:files['config/config.php']=b'different'
        bodies={'configuration':configuration,'local_files':tar_bytes(files),
                'recovery_parameters':json.dumps(self.document).encode()}
        for role,raw in bodies.items():
            a=self.m['artifacts'][role];name=a['key'].rsplit('/',1)[1]
            a['size_bytes']=len(raw);a['sha256']=hashlib.sha256(raw).hexdigest();(self.root/name).write_bytes(raw)
    def test_stages_private_files_without_executing_code(self):
        self.artifacts();p,result=stage_private(self.root,self.m,self.p.runtime_code)
        self.assertEqual((self.root/'runtime/secrets/postgres_password').read_bytes(),b'synthetic-private')
        self.assertFalse(result['restore_proven']);self.assertFalse(result['destroy_authorized'])
        self.assertEqual(p.database,self.p.database)
    def test_different_configuration_copies_rejected(self):
        self.artifacts(True)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_CONFIGURATION_ARCHIVES_MISMATCH$'):
            stage_private(self.root,self.m,self.p.runtime_code)
        self.assertFalse((self.root/'runtime').exists())
    def test_unreviewed_backup_code_rejected_before_extraction(self):
        self.artifacts();reviewed=dict(self.p.runtime_code);reviewed['storage-guard.php']=b'different'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RUNTIME_REVIEW_REQUIRED$'):stage_private(self.root,self.m,reviewed)
        self.assertFalse((self.root/'html').exists())
    def test_changed_local_artifact_rejected(self):
        self.artifacts();(self.root/'recovery_parameters.json').write_bytes(b'changed')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_LOCAL_ARTIFACT_CHANGED$'):
            stage_private(self.root,self.m,self.p.runtime_code)
    def test_second_staging_never_overwrites(self):
        self.artifacts();stage_private(self.root,self.m,self.p.runtime_code)
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ARCHIVE_DESTINATION_NOT_NEW$'):
            stage_private(self.root,self.m,self.p.runtime_code)
    def test_new_private_file_never_overwrites(self):
        path=self.root/'secret';write_new(path,b'synthetic')
        with self.assertRaises(FileExistsError):write_new(path,b'changed')
        self.assertEqual(path.read_bytes(),b'synthetic')
    def test_tree_hash_detects_changed_config(self):
        (self.root/'config.php').write_bytes(b'a');before=tree_digest(self.root)
        (self.root/'config.php').write_bytes(b'b');self.assertNotEqual(before,tree_digest(self.root))
    def test_crlf_shebang_rejected(self):
        bad=dict(self.p.runtime_code);bad['postgres-init.sh']=bad['postgres-init.sh'].replace(b'\n',b'\r\n')
        self.document['runtime_code_base64']['postgres-init.sh']=base64.b64encode(bad['postgres-init.sh']).decode()
        self.artifacts()
        with self.assertRaisesRegex(SealFailure,'^RESTORE_RUNTIME_SHEBANG_INVALID$'):stage_private(self.root,self.m,bad)


class Recipe(unittest.TestCase):
    def setUp(self):self.c,self.m,self.p=fixture()
    def build(self):return destination_runtime(self.p,self.c,'8.8.8.8',self.m)
    def test_core_only_no_new_installation(self):
        raw,env=self.build();recipe=yaml.safe_load(raw)
        self.assertEqual(set(recipe['services']),{'nextcloud','postgres','redis'})
        self.assertNotIn('grafana_admin_password',recipe['secrets'])
        self.assertNotIn('configure-nextcloud.sh:',raw.decode())
        self.assertIn('restore-start.sh:',raw.decode());self.assertIn(b'NEXTCLOUD_S3_BUCKET=fixture-destination',env)
        self.assertNotIn(b'synthetic-private',raw+env)
    def test_native_auto_install_inputs_absent(self):
        raw,_=self.build();environment=yaml.safe_load(raw)['services']['nextcloud']['environment']
        self.assertNotIn('NEXTCLOUD_ADMIN_USER',environment);self.assertNotIn('POSTGRES_PASSWORD_FILE',environment)
    def test_env_duplicate_rejected(self):
        self.p.compose_env+=b'POSTGRES_DB=other\n'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ENV_REVIEW_REQUIRED$'):self.build()
    def test_env_extra_aws_credentials_rejected(self):
        self.p.compose_env+=b'AWS_ACCESS_KEY_ID=synthetic\n'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ENV_REVIEW_REQUIRED$'):self.build()
    def test_different_project_rejected(self):
        self.c['project']='other'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ENV_SCOPE_MISMATCH$'):self.build()
    def test_static_secret_path_rejected(self):
        self.p.compose_env=self.p.compose_env.replace(b'./secrets/postgres_password',b'/external/secret')
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SECRET_PATH_REJECTED$'):self.build()
    def test_different_image_rejected(self):
        self.c['images']['nextcloud']='nextcloud:other@sha256:'+'f'*64
        with self.assertRaisesRegex(SealFailure,'^RESTORE_COMPOSE_IMAGE_MISMATCH$'):self.build()
    def test_nonpublic_address_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_PUBLIC_HOST_REJECTED$'):
            destination_runtime(self.p,self.c,'127.0.0.1',self.m)
    def test_request_bound_to_backup(self):
        r=json.loads(remap_request(self.m,self.c,'8.8.8.8'))
        self.assertEqual(r['identity_sha256'],self.m['source']['installation_identity_sha256'])
        self.assertFalse(r['maintenance'])
    def test_invalid_request_mode_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REMAP_MODE_REJECTED$'):remap_request(self.m,self.c,'8.8.8.8','install')


class ConfigPHP(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PHP.is_file(),'Use the already installed PHP runtime; never download from tests')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.c,self.m,self.p=fixture()
        self.identity={'instanceid':'synthetic-id','passwordsalt':'synthetic-salt','secret':'synthetic-secret'}
        self.m['source']['installation_identity_sha256']=hashlib.sha256(json.dumps(self.identity,separators=(',',':')).encode()).hexdigest()
        self.store={'class':'\\OC\\Files\\ObjectStore\\S3','arguments':{'bucket':self.c['source_data_bucket'],
            'region':self.c['region'],'use_ssl':True,'use_path_style':False,'verify_bucket_exists':False}}
        self.config={**self.identity,'installed':True,'maintenance':True,'dbtype':'pgsql','dbname':'nextcloud',
            'dbuser':'nextcloud','dbtableprefix':'oc_','datadirectory':'/var/www/html/data','objectstore':self.store}
        self.save()
    def save(self):
        raw=json.dumps(self.config).replace('\\','\\\\').replace("'","\\'")
        (self.root/'config.php').write_text("<?php $CONFIG=json_decode('"+raw+"',true);",encoding='utf-8')
        (self.root/'.platform-storage.json').write_text(json.dumps(self.store),encoding='utf-8')
    def run_config(self,mode='remap'):
        return subprocess.run([str(PHP),str(ROOT/'scripts/restore/remap-config.php'),str(self.root)],
              input=remap_request(self.m,self.c,'8.8.8.8',mode),capture_output=True,timeout=10)
    def test_remap_preserves_identity_and_installed(self):
        r=self.run_config();self.assertEqual(r.returncode,0,r.stderr.decode())
        result=json.loads(r.stdout);self.assertTrue(result['identity_matches']);self.assertFalse(result['restore_proven'])
        self.assertNotIn(b'synthetic-secret',r.stdout+r.stderr)
        self.assertEqual(self.run_config('verify').returncode,0)
    def test_second_remap_rejected_without_overwrite(self):
        self.assertEqual(self.run_config().returncode,0);before=(self.root/'config.php').read_bytes()
        self.assertNotEqual(self.run_config().returncode,0);self.assertEqual(before,(self.root/'config.php').read_bytes())
    def test_apps_config_has_official_context(self):
        (self.root/'apps.config.php').write_text("<?php $CONFIG=['apps_paths'=>[['path'=>OC::$SERVERROOT.'/apps','writable'=>false]]];")
        self.assertEqual(self.run_config().returncode,0)
    def test_uninstalled_configuration_rejected(self):
        self.config['installed']=False;self.save();self.assertNotEqual(self.run_config().returncode,0)
    def test_wrong_identity_rejected(self):
        self.config['secret']='other';self.save();self.assertNotEqual(self.run_config().returncode,0)
    def test_static_aws_credentials_rejected(self):
        self.store['arguments']['key']='synthetic';self.save();self.assertNotEqual(self.run_config().returncode,0)
    def test_marker_mismatch_rejected(self):
        (self.root/'.platform-storage.json').write_text('{}');self.assertNotEqual(self.run_config().returncode,0)
    def test_conflicting_lower_precedence_store_rejected(self):
        (self.root/'a.config.php').write_text("<?php $CONFIG=['objectstore'=>['class'=>'other']];")
        (self.root/'z.config.php').write_text("<?php $CONFIG="+self._store_php()+";")
        before=(self.root/'config.php').read_bytes();self.assertNotEqual(self.run_config().returncode,0)
        self.assertEqual(before,(self.root/'config.php').read_bytes())
    def _store_php(self):
        raw=json.dumps({'objectstore':self.store}).replace('\\','\\\\').replace("'","\\'")
        return "json_decode('"+raw+"',true)"
    def test_config_output_never_leaks(self):
        (self.root/'apps.config.php').write_text("<?php echo 'synthetic-private'; $CONFIG=[];")
        r=self.run_config();self.assertNotEqual(r.returncode,0);self.assertNotIn(b'synthetic-private',r.stdout+r.stderr)


class IntegratedSequence(unittest.TestCase):
    def setUp(self):
        self.d=object.__new__(Destination);self.d.runtime=Mock();self.d.runtime.host.config={'backups_bucket':'backups','state_bucket':'state'}
        self.d.selected=SimpleNamespace(bundle={'manifest':{'consistency':{'maintenance_initially_enabled':False}}})
    def test_start_only_calls_restore_recipe(self):
        self.d.start_without_install()
        self.d.runtime.compose.assert_called_once_with('up','-d','--no-deps','--pull','never','--no-build','nextcloud')
    def test_readonly_revalidation_does_not_restart(self):
        r=self.d.check_application_and_restart(read_only=True)
        self.d.runtime.compose.assert_not_called();self.assertFalse(r['restore_proven'])
    def test_first_validation_restarts_once(self):
        self.d.check_application_and_restart();self.d.runtime.compose.assert_called_once_with('restart','nextcloud')
        self.assertEqual(self.d.runtime.wait_healthy.call_count,2)
    def test_original_maintenance_not_silently_disabled(self):
        self.d.selected.bundle['manifest']['consistency']['maintenance_initially_enabled']=True
        self.d.check_application_and_restart(read_only=True)
        self.d.runtime.wait_healthy.assert_called_once_with(('postgres','redis','nextcloud'),nextcloud_maintenance=True)
        self.d.runtime.application_status.assert_called_once();self.d.runtime.compose.assert_not_called()
    def test_remap_requires_verified_objects(self):
        self.d.mappings=None
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OBJECTS_NOT_VERIFIED$'):self.d.remap_storage_explicitly()
        self.d.runtime.remap.assert_not_called()


class HostCommands(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.r=object.__new__(HostRuntime);self.c,_,_=fixture();self.r.host=SimpleNamespace(config=self.c)
        self.r.guard=Mock();self.r.root=Path(self.temp.name);self.r.reviewed_root=self.r.root/'code'
        self.r.store=Store(self.temp.name,synthetic=True)
        self.r.operation='20261011T120000Z-22222222-2222-4333-8444-555555555555'
        self.r.directory=self.r.store.operation(self.r.operation);self.r.directory.mkdir()
        self.r.public_ip='8.8.8.8'
    def test_arbitrary_command_rejected_before_process(self):
        with patch('scripts.restore.host_runtime.subprocess.run') as run:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_COMMAND_REJECTED$'):self.r.command(['/bin/sh','-c','unsafe'])
            run.assert_not_called()
    def test_arbitrary_compose_subcommand_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_COMPOSE_COMMAND_REJECTED$'):self.r.compose('down','--volumes')
    def test_approved_command_is_journaled_and_suppresses_output(self):
        with patch('scripts.restore.host_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.r.compose('up','-d','--no-deps','--pull','never','--no-build','postgres','redis')
            self.assertEqual(self.r.store.read(self.r.operation+'/host-command.json')['phase'],'finished')
            kwargs=run.call_args.kwargs;self.assertEqual(kwargs['stdout'],subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
            self.assertNotIn('AWS_ACCESS_KEY_ID',kwargs['env'])
    def test_process_timeout_leaves_intervention_journal(self):
        with patch('scripts.restore.host_runtime.subprocess.run',side_effect=subprocess.TimeoutExpired('synthetic',1)):
            with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_COMMAND_TIMEOUT$'):self.r.compose('restart','nextcloud')
        self.assertEqual(self.r.store.read(self.r.operation+'/host-command.json')['phase'],'intervention_required')
    def test_interrupted_command_cannot_be_retried(self):
        self.r.store.write(self.r.operation+'/host-command.json',{'phase':'start_intent'})
        with patch('scripts.restore.host_runtime.subprocess.run') as run:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_COMMAND_INTERRUPTED$'):self.r.compose('restart','nextcloud')
            run.assert_not_called()
    def test_helper_has_no_network_and_no_log_driver(self):
        args=self.r.remap_command();self.assertIn('none',args)
        self.assertEqual(args[args.index('--network')+1],'none')
        self.assertEqual(args[args.index('--log-driver')+1],'none')
        self.assertNotIn('--rm',args);self.assertNotIn('--privileged',args)
    def test_mock_executor_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_REAL_EXECUTOR_REQUIRED$'):
            execute_restore(Mock(),Mock(),Mock(),Mock(),Mock(),'123:1')


class BucketProtection(unittest.TestCase):
    def setUp(self):
        self.a=object.__new__(DestinationObjects);self.a.bucket='fixture-destination'
        arn='arn:aws:s3:::'+self.a.bucket
        self.responses={'get_bucket_versioning':{'Status':'Enabled'},
            'get_bucket_encryption':{'ServerSideEncryptionConfiguration':{'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}},
            'get_public_access_block':{'PublicAccessBlockConfiguration':dict(BlockPublicAcls=True,IgnorePublicAcls=True,BlockPublicPolicy=True,RestrictPublicBuckets=True)},
            'get_bucket_ownership_controls':{'OwnershipControls':{'Rules':[{'ObjectOwnership':'BucketOwnerPreferred'}]}},
            'get_bucket_policy':{'Policy':json.dumps({'Statement':[{'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[arn,arn+'/*'],
                                                        'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]})}}
        self.a.call=Mock(side_effect=lambda action,**kwargs:self.responses[action])
    def test_exact_bucket_protections_accepted(self):self.a.assert_protections()
    def test_versioning_suspended_rejected(self):
        self.responses['get_bucket_versioning']['Status']='Suspended'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_VERSIONING_REQUIRED$'):self.a.assert_protections()
    def test_kms_not_silently_accepted(self):
        self.responses['get_bucket_encryption']['ServerSideEncryptionConfiguration']['Rules'][0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']='aws:kms'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_SSE_S3_REQUIRED$'):self.a.assert_protections()
    def test_public_access_not_accepted(self):
        self.responses['get_public_access_block']['PublicAccessBlockConfiguration']['BlockPublicPolicy']=False
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_PUBLIC_ACCESS_REJECTED$'):self.a.assert_protections()
    def test_incompatible_acl_mode_rejected(self):
        self.responses['get_bucket_ownership_controls']['OwnershipControls']['Rules'][0]['ObjectOwnership']='BucketOwnerEnforced'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_ACL_COMPATIBILITY_REQUIRED$'):self.a.assert_protections()
    def test_missing_tls_deny_rejected(self):
        self.responses['get_bucket_policy']['Policy']='{"Statement":[]}'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_TARGET_TLS_POLICY_REQUIRED$'):self.a.assert_protections()


class VolumeSafety(unittest.TestCase):
    def setUp(self):
        self.project='fixture';self.name='fixture_nextcloud_html'
        self.value={'Name':self.name,'Driver':'local','Scope':'local','Options':None,
                    'Mountpoint':'/var/lib/docker/volumes/'+self.name+'/_data',
                    'Labels':{'com.docker.compose.project':self.project,'com.docker.compose.volume':'nextcloud_html'}}
    def test_precreated_owned_unmounted_volume_accepted(self):validate_html_volume(self.value,self.name,self.project,[{'Mounts':[]}])
    def test_other_project_rejected(self):
        self.value['Labels']['com.docker.compose.project']='other'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_REJECTED$'):validate_html_volume(self.value,self.name,self.project,[])
    def test_other_driver_rejected(self):
        self.value['Driver']='external'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_REJECTED$'):validate_html_volume(self.value,self.name,self.project,[])
    def test_bind_or_remote_driver_options_rejected(self):
        self.value['Options']={'type':'none','o':'bind','device':'/external'}
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_REJECTED$'):validate_html_volume(self.value,self.name,self.project,[])
    def test_other_mountpoint_rejected(self):
        self.value['Mountpoint']='/data'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_REJECTED$'):validate_html_volume(self.value,self.name,self.project,[])
    def test_mounted_even_readonly_volume_rejected(self):
        peers=[{'Mounts':[{'Name':self.name,'RW':False}]}]
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_IN_USE$'):validate_html_volume(self.value,self.name,self.project,peers)
    def test_missing_peer_mount_inspection_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HTML_VOLUME_INSPECTION_REQUIRED$'):
            validate_html_volume(self.value,self.name,self.project,[{}])


class JournalInspection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.op='20261011T120000Z-22222222-2222-4333-8444-555555555555'
        (self.root/self.op).mkdir();self.store=Store(self.temp.name,synthetic=True)
        self.control=object.__new__(RestoreControl);c,_,_=fixture()
        self.control.host=SimpleNamespace(config=c);self.control.staging=self.store
        self.control.api=Mock();self.control.api.containers.return_value=[]
        self.journal={'internal_version':1,'kind':'restore-candidate-journal',
            'binding':{'operation_id':self.op,'backup_id':self.op,'manifest_sha256':'a'*64,'completion_version_id':'fixture-version',
                       'destination':{k:c[k] for k in ('account','region','instance','data_bucket')}},
            'phase':'candidate_ready','completed_steps':list(STEPS),'restore_proven':False,'destroy_authorized':False}
    def save(self):self.store.write(self.op+'/restore-journal.json',self.journal)
    def test_complete_journal_clear(self):self.save();self.assertEqual(self.control.inspect(),[])
    def test_premature_ready_marker_blocks(self):
        self.journal['completed_steps']=[];self.save();self.assertIn('RESTORE_INTERRUPTED',self.control.inspect())
    def test_foreign_target_binding_rejected(self):
        self.journal['binding']['destination']['instance']='i-'+'9'*17;self.save()
        with self.assertRaisesRegex(SealFailure,'^RESTORE_JOURNAL_BINDING_REJECTED$'):self.control.inspect()
    def test_pending_command_blocks(self):
        self.save();self.store.write(self.op+'/host-command.json',{'phase':'start_intent'})
        self.assertIn('RESTORE_COMMAND_INTERRUPTED',self.control.inspect())
    def test_false_functional_proof_rejected(self):
        self.journal['restore_proven']=True;self.save()
        with self.assertRaisesRegex(SealFailure,'^RESTORE_JOURNAL_BINDING_REJECTED$'):self.control.inspect()

    def historical(self):
        self.save();self.store.write(self.op+'/host-command.json',{'phase':'finished'})
        self.execution={'version':1,'operation_id':self.op,
            'boot_id':'11111111-1111-4111-8111-111111111111','execs':[
                {'container_id':'a'*64,'exec_id':'b'*64,'phase':'finished',
                 'command_sha256':'c'*64,'exit_code':0}]}
        self.save_execution()

    def save_execution(self):self.store.write(self.op+'/exec-journal.json',self.execution)

    def inspect_after_reboot(self):
        with patch('scripts.restore.control.Path.read_text',return_value='22222222-2222-4222-8222-222222222222'):
            return self.control.inspect()

    def test_completed_history_after_reboot_clear_without_journal_write(self):
        self.historical();before=(self.root/self.op/'exec-journal.json').read_bytes()
        self.assertEqual(self.inspect_after_reboot(),[])
        self.assertEqual((self.root/self.op/'exec-journal.json').read_bytes(),before)

    def test_historical_running_unknown_exec_still_blocks(self):
        self.historical();self.control.api.containers.return_value=[{'Id':'a'*64}]
        with patch('scripts.restore.control.DockerExecAPI') as bridge:
            bridge.return_value.running_execs.return_value=['d'*64]
            self.assertIn('UNTRACKED_DOCKER_EXEC',self.inspect_after_reboot())

    def test_interrupted_restore_after_reboot_preserves_guard(self):
        self.historical();self.journal['completed_steps']=[];self.save()
        with self.assertRaisesRegex(CaptureFailure,'^CAPTURE_BOOT_CHANGED$'):self.inspect_after_reboot()

    def test_pending_host_command_after_reboot_preserves_guard(self):
        self.historical();self.store.write(self.op+'/host-command.json',{'phase':'start_intent'})
        with self.assertRaisesRegex(CaptureFailure,'^CAPTURE_BOOT_CHANGED$'):self.inspect_after_reboot()

    def test_missing_host_command_after_reboot_preserves_guard(self):
        self.historical();(self.root/self.op/'host-command.json').unlink()
        with self.assertRaisesRegex(CaptureFailure,'^CAPTURE_BOOT_CHANGED$'):self.inspect_after_reboot()

    def test_invalid_historical_records_rejected(self):
        for field,value in [('phase','inspection_required'),('exec_id',None),('container_id','invalid'),
                            ('command_sha256','invalid'),('exit_code',True),('exit_code',-1),('exit_code',256)]:
            with self.subTest(field=field,value=value):
                self.historical();self.execution['execs'][0][field]=value;self.save_execution()
                with self.assertRaisesRegex(SealFailure,'^EXEC_JOURNAL_INVALID$'):self.inspect_after_reboot()

    def test_duplicate_historical_exec_rejected(self):
        self.historical();self.execution['execs']*=2;self.save_execution()
        with self.assertRaisesRegex(SealFailure,'^EXEC_JOURNAL_INVALID$'):self.inspect_after_reboot()

    def test_invalid_historical_boot_rejected(self):
        self.historical();self.execution['boot_id']='invalid';self.save_execution()
        with self.assertRaisesRegex(SealFailure,'^EXEC_JOURNAL_INVALID$'):self.inspect_after_reboot()


if __name__=='__main__':unittest.main()
