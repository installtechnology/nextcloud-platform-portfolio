import copy
import tempfile
import unittest
from pathlib import Path
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sandbox.spec import IMAGES, NETWORKS, CONTROLS, environment
from scripts.backup.sandbox.validation import compose_model, validate_compose, validate_snapshot

PROJECT='nc-s3-imds-test-0123456789abcdef'

class SafetyTests(unittest.TestCase):
    def failcode(self,code,fn):
        with self.assertRaises(CaptureFailure) as error: fn()
        self.assertEqual(error.exception.code,code)
    def test_reviewed_model_never_authorizes_operations(self):
        r=validate_compose(compose_model());self.assertFalse(r['runtime_allowed']);self.assertFalse(r['backup_complete'])
    def mutate(self, change):
        d=compose_model();change(d);return d
    def test_extra_service(self):
        self.failcode('SANDBOX_FIVE_SERVICES_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['services'].update(extra={}))))
    def test_missing_service(self):
        self.failcode('SANDBOX_FIVE_SERVICES_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['services'].pop('redis'))))
    def test_each_image_pin(self):
        for service in IMAGES:
            with self.subTest(service=service):
                self.failcode('SANDBOX_IMAGE_MISMATCH',lambda:validate_compose(self.mutate(lambda d:d['services'][service].update(image='unexpected'))))
    def test_host_network_public_ports_privileges_socket(self):
        for field,value in (('ports',['3000:80']),('network_mode','host'),('privileged',True),('cap_add',['NET_ADMIN']),('env_file',['private'])):
            with self.subTest(field=field):
                self.failcode('SANDBOX_ISOLATION_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['services']['nextcloud'].update({field:value}))))
    def test_noninternal_network(self):
        self.failcode('SANDBOX_ISOLATION_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['networks']['db_internal'].update(internal=False))))
    def test_operational_mount(self):
        self.failcode('SANDBOX_MOUNT_REJECTED',lambda:validate_compose(self.mutate(lambda d:d['services']['nextcloud']['volumes'].append({'type':'bind','source':'/opt/nextcloud-platform','read_only':True}))))
    def test_unreviewed_entrypoint(self):
        self.failcode('SANDBOX_REVIEWED_COMPOSE_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['services']['nextcloud'].update(entrypoint=['true']))))
    def test_static_credentials_webidentity_ecs_proxies(self):
        for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_PROFILE','AWS_WEB_IDENTITY_TOKEN_FILE','AWS_CONTAINER_CREDENTIALS_FULL_URI','HTTPS_PROXY'):
            with self.subTest(key=key):
                self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:environment('nextcloud',{**CONTROLS,key:'synthetic-rejected'}))
    def test_no_credentials_fallback(self): self.failcode('SANDBOX_IMDS_REQUIRED',lambda:environment('nextcloud',{}))
    def test_wrong_imds_endpoint(self): self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:environment('nextcloud',{**CONTROLS,'AWS_EC2_METADATA_SERVICE_ENDPOINT':'http://169.254.169.254'}))
    def test_other_services_cannot_use_imds_controls(self): self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:environment('postgres',CONTROLS))
    def snapshot(self):
        d=compose_model();containers=[];images={};networks={};volumes={}
        for n in ('db_internal','storage_internal'):
            networks[n]={'Internal':True,'Driver':'bridge','EnableIPv6':False,'Labels':{'com.docker.compose.project':PROJECT,'com.docker.compose.network':n,'platform.sandbox.kind':'s3-imds-v1'}}
        for name,ref in IMAGES.items():
            repo,digest=ref.split('@');repo=repo.rsplit(':',1)[0]
            images[name]={'Id':'synthetic-'+name,'Os':'linux','Architecture':'amd64','RepoDigests':[repo+'@'+digest]}
            mounts=[]
            if name in ('nextcloud','postgres'):
                volume='nextcloud_html' if name=='nextcloud' else 'postgres_data'
                volumes[PROJECT+'_'+volume]={'Driver':'local','Options':None,'Labels':{'com.docker.compose.project':PROJECT,'platform.capture.isolated':'true','com.docker.compose.volume':volume}}
                mounts=[{'Type':'volume','Name':PROJECT+'_'+volume,'Source':'synthetic-volume-'+volume,'Destination':'/var/www/html' if name=='nextcloud' else '/var/lib/postgresql/data'}]
            containers.append({'Id':name,'Config':{'Image':ref,'User':d['services'][name].get('user',''),'Labels':{'com.docker.compose.project':PROJECT,'com.docker.compose.service':name,'platform.capture.isolated':'true','platform.sandbox.kind':'s3-imds-v1'},'Env':[k+'='+(PROJECT if k=='PLATFORM_SANDBOX_PROJECT' else v) for k,v in d['services'][name].get('environment',{}).items()]},'Image':images[name]['Id'],'HostConfig':({'ReadonlyRootfs':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']} if name in ('s3-emulator','imds-emulator') else {}),'NetworkSettings':{'Networks':{n:{} for n in NETWORKS[name]}},'Mounts':mounts})
        return {'origin':'synthetic-local-v1','egress_controls':dict.fromkeys(('external_routes_blocked','imds_ipv4_blocked','imds_ipv6_blocked','exclusive_vm'),True),'containers':containers,'images':images,'networks':networks,'volumes':volumes}
    def validate(self,snapshot):
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent)/PROJECT;root.mkdir()
            return validate_snapshot(PROJECT,root,snapshot)
    def test_synthetic_snapshot_never_authorizes(self): self.assertFalse(self.validate(self.snapshot())['runtime_allowed'])
    def test_origin_required(self):
        s=self.snapshot();s['origin']='production';self.failcode('SANDBOX_ORIGIN_REQUIRED',lambda:self.validate(s))
    def test_each_egress_requirement(self):
        for key in self.snapshot()['egress_controls']:
            with self.subTest(key=key):
                s=self.snapshot();s['egress_controls'][key]=False;self.failcode('SANDBOX_EGRESS_NOT_PROVEN',lambda:self.validate(s))
    def test_each_image_runtime_identity(self):
        for key in IMAGES:
            with self.subTest(key=key):
                s=self.snapshot();s['images'][key]['Id']='foreign';self.failcode('SANDBOX_IMAGE_MISMATCH',lambda:self.validate(s))
    def test_foreign_consumer_even_stopped(self):
        s=self.snapshot();s['containers'].append({'Id':'foreign','Config':{'Labels':{}},'State':{'Running':False},'NetworkSettings':{'Networks':{'storage_internal':{}}},'Mounts':[]})
        self.failcode('SANDBOX_EXTERNAL_WRITER_FOUND',lambda:self.validate(s))
    def test_invalid_project(self):
        with tempfile.TemporaryDirectory() as root:
            self.failcode('SANDBOX_PROJECT_REQUIRED',lambda:validate_snapshot('production',root,self.snapshot()))
    def test_invalid_environment_is_sanitized(self):
        s=self.snapshot();s['containers'][0]['Config']['Env'].append('AWS_ACCESS_KEY_ID=synthetic-sensitive-value')
        self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:self.validate(s))
    def test_reuse_installer_guards_not_reimplementation(self):
        from scripts.backup.sandbox.spec import HERE
        php=(HERE/'install-sandbox.php').read_text()
        self.assertIn('productionInstallationAdapter()',php)
        self.assertIn('runPlatformInstallation($adapter',php)
        self.assertEqual(php.count("$adapter['"),1)
        self.assertIn("'http' => ['verify' => '/fixture/ca.pem'",php)
        self.assertNotIn("'credentials'",php)
    def test_no_auto_installer_vars(self):
        env=compose_model()['services']['nextcloud']['environment']
        self.assertFalse(set(env)&{'POSTGRES_PASSWORD','POSTGRES_PASSWORD_FILE','NEXTCLOUD_ADMIN_USER','NEXTCLOUD_ADMIN_PASSWORD','NEXTCLOUD_ADMIN_PASSWORD_FILE'})

    def test_cli_rejects_every_execution_input(self):
        import io
        from contextlib import redirect_stdout
        from scripts.backup.sandbox.__main__ import main
        for args in (['start'],['capture'],['restore'],['--operational'],['--aws'],['--allow']):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args),2)
                self.assertIn('SANDBOX_NO_EXECUTION_MODE',output.getvalue())
                self.assertIn('"backup_complete": false',output.getvalue())
    def test_trial_plan_never_claims_execution(self):
        import json
        from scripts.backup.sandbox.spec import HERE
        plan=json.loads((HERE/'trial-plan.json').read_text())
        self.assertFalse(plan['operational_allowed']);self.assertFalse(plan['backup_complete'])
        self.assertEqual(len(plan['cases']),16)
        self.assertEqual(len({c['id'] for c in plan['cases']}),16)
        self.assertTrue(all(c['status']=='not_executed' for c in plan['cases']))

    def test_explicit_sandbox_context_required(self):
        self.failcode('SANDBOX_CONTEXT_REQUIRED',lambda:environment('nextcloud',CONTROLS))
        for service in IMAGES:
            with self.subTest(service=service):
                model=compose_model();model['services'][service]['environment'].pop('PLATFORM_SYNTHETIC_SANDBOX')
                self.failcode('SANDBOX_CONTEXT_REQUIRED',lambda:validate_compose(model))
    def test_runtime_namespace_and_dns_override_rejected(self):
        for key,value in (('NetworkMode','container:foreign'),('PidMode','container:foreign'),('IpcMode','container:foreign'),('ExtraHosts',['s3.sandbox.test:169.254.169.254']),('Dns',['8.8.8.8']),('DnsSearch',['outside']),('DnsOptions',['debug']),('VolumesFrom',['production'])):
            with self.subTest(key=key):
                snapshot=self.snapshot();snapshot['containers'][0]['HostConfig'][key]=value
                self.failcode('SANDBOX_ISOLATION_REQUIRED',lambda:self.validate(snapshot))
    def test_compose_dns_override_rejected(self):
        for key,value in (('dns',['8.8.8.8']),('extra_hosts',['s3.sandbox.test:169.254.169.254']),('volumes_from',['production'])):
            with self.subTest(key=key):
                self.failcode('SANDBOX_ISOLATION_REQUIRED',lambda:validate_compose(self.mutate(lambda d:d['services']['nextcloud'].update({key:value}))))
    def test_operational_root_rejected(self):
        from scripts.backup.sandbox.spec import HERE
        self.failcode('SANDBOX_ROOT_REQUIRED',lambda:validate_snapshot(PROJECT,HERE.parents[2],self.snapshot()))
    def test_operational_volume_name_rejected(self):
        snapshot=self.snapshot();name='operational-volume';snapshot['volumes'][name]=snapshot['volumes'].pop(PROJECT+'_nextcloud_html')
        snapshot['containers'][0]['Mounts'][0]['Name']=name
        self.failcode('SANDBOX_MOUNT_REJECTED',lambda:self.validate(snapshot))
    def test_readonly_unreviewed_bind_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent)/PROJECT;root.mkdir();source=root/'unreviewed';source.write_text('synthetic')
            snapshot=self.snapshot();snapshot['containers'][0]['Mounts'].append({'Type':'bind','Source':str(source),'Destination':'/fixture/unreviewed','RW':False})
            self.failcode('SANDBOX_MOUNT_REJECTED',lambda:validate_snapshot(PROJECT,root,snapshot))
    def test_escape_and_sensitive_destination_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent)/PROJECT;root.mkdir();source=Path(parent)/'outside';source.write_text('synthetic')
            snapshot=self.snapshot();snapshot['containers'][0]['Mounts'].append({'Type':'bind','Source':str(source),'Destination':'/root/.aws/credentials','RW':False})
            self.failcode('SANDBOX_MOUNT_REJECTED',lambda:validate_snapshot(PROJECT,root,snapshot))
    def test_reviewed_bind_acceptance_and_writable_rejection(self):
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent)/PROJECT;(root/'code').mkdir(parents=True);source=root/'code/platform.config.php';source.write_text('<?php // synthetic')
            snapshot=self.snapshot();mount={'Type':'bind','Source':str(source),'Destination':'/opt/platform/platform.config.php','RW':False};snapshot['containers'][0]['Mounts'].append(mount)
            self.assertFalse(validate_snapshot(PROJECT,root,snapshot)['runtime_allowed'])
            mount['RW']=True;self.failcode('SANDBOX_MOUNT_REJECTED',lambda:validate_snapshot(PROJECT,root,snapshot))
    def test_moto_configuration_injection_rejected(self):
        for service in IMAGES:
            values=dict(compose_model()['services'][service].get('environment',{}));values['MOTO_PROXY_MODE']='true'
            self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:environment(service,values))

    def test_runtime_unresolved_or_missing_identity_rejected(self):
        for identity in (None,'production','nc-s3-imds-test-${SANDBOX_ID:?16 lowercase hexadecimal characters required}'):
            with self.subTest(identity=identity):
                values=dict(compose_model()['services']['nextcloud']['environment'])
                if identity is None: values.pop('PLATFORM_SANDBOX_PROJECT')
                else: values['PLATFORM_SANDBOX_PROJECT']=identity
                self.failcode('SANDBOX_PROJECT_REQUIRED',lambda:environment('nextcloud',values))
    def wrapper_trial(self, mode):
        import os, json, subprocess
        from scripts.backup.sandbox.spec import HERE
        php=os.environ.get('CAPTURE_TEST_PHP')
        if not php: self.skipTest('Explicit cached PHP executable required; no install or fallback')
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent);preflight=root/'preflight.php';original=root/'original.php';wrapper=root/'wrapper.php';report=root/'report.json'
            prefix=(HERE.parents[2]/'docker/install-nextcloud.php').read_text(encoding='utf-8').split('function productionInstallationAdapter(): array {')[0]
            factory=r'''function productionInstallationAdapter(): array {
  $GLOBALS['installed'] = getenv('TEST_MODE') === 'installed';
  $a = [
    'guard' => static function () { $GLOBALS['calls'][]='guard'; },
    'state' => static function () { return ['installed'=>$GLOBALS['installed'],'storage_matches'=>true,'install_config_matches'=>true]; },
    'local_data_safe' => static function () { return true; },
    'database_empty' => static function () { return getenv('TEST_MODE') !== 'partial'; },
    'bucket_empty' => static function () { throw new RuntimeException('ORIGINAL_BUCKET_MUST_BE_REPLACED'); },
    'install' => static function () { $GLOBALS['calls'][]='install';$GLOBALS['installed']=true;return 0; },
  ];
  $GLOBALS['factory']=$a;return $a;
}
'''
            original.write_text(prefix+factory,encoding='utf-8')
            stub=r'''<?php
namespace Aws\S3 {
 class S3Client {
  public function __construct($options) { $GLOBALS['options']=$options; }
  public function listObjectsV2($request) {
   $GLOBALS['request']=$request;
   if(getenv('TEST_MODE')==='tls') { throw new \RuntimeException('synthetic-sensitive-error'); }
   return getenv('TEST_MODE')==='nonempty' ? ['KeyCount'=>1,'Contents'=>[['Key'=>'synthetic']]] : ['KeyCount'=>0,'Contents'=>[]];
  }
 }
}
namespace {
 $GLOBALS['calls']=[];
 register_shutdown_function(static function () {
  $unchanged=[];
  foreach(($GLOBALS['factory']??[]) as $key=>$closure) {
   $unchanged[$key] = ($GLOBALS['adapter'][$key]??null) === $closure;
  }
  file_put_contents(getenv('TEST_REPORT'),json_encode(['unchanged'=>$unchanged,'calls'=>$GLOBALS['calls'],'options'=>$GLOBALS['options']??null,'request'=>$GLOBALS['request']??null]));
 });
}
'''
            preflight.write_text(stub,encoding='utf-8')
            body=(HERE/'install-sandbox.php').read_text(encoding='utf-8')
            # Only filesystem paths change for this Windows harness; wrapper logic is unchanged.
            for old,new in (('/fixture/sandbox-preflight.php',preflight),('/opt/platform/install-original.php',original),('/var/www/html/config',root)):
                body=body.replace("'"+old+"'",json.dumps(new.as_posix()))
            wrapper.write_text(body,encoding='utf-8')
            env={k:v for k,v in os.environ.items() if not k.startswith(('AWS_','NC_','NEXTCLOUD_','POSTGRES_','REDIS_'))}
            env.update(TEST_MODE=mode,TEST_REPORT=str(report))
            result=subprocess.run([php,str(wrapper)],env=env,capture_output=True,text=True,timeout=10)
            self.assertNotIn('synthetic-sensitive-error',result.stdout+result.stderr)
            self.assertTrue(report.exists(),result.stderr)
            return result,json.loads(report.read_text())
    def test_wrapper_preserves_all_other_adapter_callbacks(self):
        result,evidence=self.wrapper_trial('fresh')
        self.assertEqual(result.returncode,0);self.assertIn('INSTALLED',result.stdout)
        self.assertTrue(all(value for key,value in evidence['unchanged'].items() if key!='bucket_empty'))
        self.assertFalse(evidence['unchanged']['bucket_empty'])
        self.assertEqual(evidence['request'],{'Bucket':'fixture-data','MaxKeys':1})
        self.assertEqual(evidence['options']['endpoint'],'https://s3.sandbox.test:5000')
        self.assertEqual(evidence['options']['http']['verify'],'/fixture/ca.pem')
        self.assertNotIn('credentials',evidence['options'])
    def test_wrapper_partial_or_nonempty_state_never_installs(self):
        for mode,reason in (('partial','DATABASE_NOT_EMPTY'),('nonempty','DATA_BUCKET_NOT_EMPTY'),('tls','INITIAL_INSTALL_ERROR')):
            with self.subTest(mode=mode):
                result,evidence=self.wrapper_trial(mode)
                self.assertEqual(result.returncode,1);self.assertIn(reason,result.stderr)
                self.assertNotIn('install',evidence['calls'])
    def test_wrapper_installed_state_skips_s3_and_install(self):
        result,evidence=self.wrapper_trial('installed')
        self.assertEqual(result.returncode,0);self.assertIn('ALREADY_INSTALLED',result.stdout)
        self.assertIsNone(evidence['options']);self.assertNotIn('install',evidence['calls'])
    def test_malformed_snapshot_error_sanitized(self):
        with tempfile.TemporaryDirectory() as parent:
            root=Path(parent)/PROJECT;root.mkdir();snapshot={'origin':'synthetic-local-v1','egress_controls':None,'synthetic-sensitive-value':'not-printable'}
            self.failcode('SANDBOX_INPUT_INVALID',lambda:validate_snapshot(PROJECT,root,snapshot))
    def test_malformed_environment_key_rejected(self):
        self.failcode('SANDBOX_ENV_INVALID',lambda:environment('nextcloud',{1:'synthetic-sensitive-value'}))

    def test_auxiliary_root_or_writable_context_rejected(self):
        for service in ('s3-emulator','imds-emulator'):
            for field,value in (('User','root'),('ReadonlyRootfs',False),('CapDrop',[]),('SecurityOpt',[])):
                with self.subTest(service=service,field=field):
                    snapshot=self.snapshot();info=next(c for c in snapshot['containers'] if c['Id']==service)
                    info['Config' if field=='User' else 'HostConfig'][field]=value
                    self.failcode('SANDBOX_AUXILIARY_CONTEXT_REQUIRED',lambda:self.validate(snapshot))
    def test_volume_destination_and_tmpfs_injection_rejected(self):
        snapshot=self.snapshot();snapshot['containers'][0]['Mounts'][0]['Destination']='/root/.aws'
        self.failcode('SANDBOX_MOUNT_REJECTED',lambda:self.validate(snapshot))
        snapshot=self.snapshot();snapshot['containers'][0]['Mounts'].append({'Type':'tmpfs','Source':'','Destination':'/root/.aws'})
        self.failcode('SANDBOX_MOUNT_REJECTED',lambda:self.validate(snapshot))
    def test_home_and_interpreter_configuration_override_rejected(self):
        for key,value in (('HOME','/operational-home'),('PYTHONPATH','/unreviewed'),('LD_PRELOAD','/unreviewed'),('BASH_ENV','/unreviewed')):
            with self.subTest(key=key):
                values=dict(compose_model()['services']['nextcloud']['environment']);values['PLATFORM_SANDBOX_PROJECT']=PROJECT;values[key]=value
                self.failcode('SANDBOX_CREDENTIAL_SOURCE_REJECTED',lambda:environment('nextcloud',values))
