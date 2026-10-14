"""Host/SQL/YAML tests with synthetic observations; no IMDS or Docker requests."""
import copy
import json
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import yaml

from scripts.restore.host_scope import validate
import scripts.restore.host_scope as host_scope
from scripts.restore.prepare_identity import check_identity,unique
from scripts.restore.roles import role_recipe
from scripts.restore.postgres_adapter import PostgreSQLDestination
from scripts.backup.sealing.common import SealFailure

ROOT=Path(__file__).resolve().parents[3]
SOURCE='i-'+ '1'*17
TARGET='i-'+ '2'*17


def config():
    compose=yaml.safe_load((ROOT/'docker/compose.yml').read_text())
    return {'version':1,'account':'000000000000','region':'us-east-1','instance':TARGET,
            'source_instance':SOURCE,'project':'fixture-platform','source_data_bucket':'fixture-source',
            'data_bucket':'fixture-destination','backups_bucket':'fixture-backups','state_bucket':'fixture-state',
            'staging_root':'/var/lib/nextcloud-restore','deployment_root':'/opt/nextcloud-platform',
            'images':{k:compose['services'][k]['image'] for k in ('nextcloud','postgres','redis')}}


def globals_recipe(owner='nextcloud'):
    return """-- synthetic PG17 globals (no passwords)
SET default_transaction_read_only = off;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
CREATE ROLE postgres;
ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS;
CREATE ROLE OWNER;
ALTER ROLE OWNER WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS;
""".replace('OWNER',owner)


class HostScope(unittest.TestCase):
    def test_config_valid(self):self.assertEqual(validate(config()).config['instance'],TARGET)
    def test_source_instance_blocked(self):
        c=config();c['instance']=SOURCE
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_SOURCE_REJECTED$'):validate(c)
    def test_source_bucket_blocked(self):
        c=config();c['data_bucket']=c['source_data_bucket']
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_STORAGE_SEPARATION$'):validate(c)
    def test_arbitrary_staging_root_blocked(self):
        c=config();c['staging_root']='/var/lib/nextcloud-recovery'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_PATH_REJECTED$'):validate(c)
    def test_activation_flag_not_allowed(self):
        c=config();c['operational_allowed']=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_CONFIG_INVALID$'):validate(c)
    def test_unpinned_image_blocked(self):
        c=config();c['images']['postgres']='postgres:17'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_IMAGE_PINS_REQUIRED$'):validate(c)
    def test_actual_imds_identity_must_match(self):
        h=validate(config());expected={k:h.config[k] for k in ('account','region','instance')}
        with patch.object(host_scope,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(host_scope,'imds_identity',return_value=expected) as imds:
            self.assertEqual(h.identity(),expected);imds.assert_called_once()
    def test_original_imds_identity_is_rejected(self):
        h=validate(config());original={'account':h.config['account'],'region':h.config['region'],'instance':SOURCE}
        with patch.object(host_scope,'os',SimpleNamespace(name='posix',geteuid=lambda:0)),patch.object(host_scope,'imds_identity',return_value=original):
            with self.assertRaisesRegex(SealFailure,'^RESTORE_HOST_IDENTITY_MISMATCH$'):h.identity()
    def test_nonroot_does_not_query_imds(self):
        with patch.object(host_scope,'os',SimpleNamespace(name='posix',geteuid=lambda:1000)),patch.object(host_scope,'imds_identity') as imds:
            with self.assertRaisesRegex(SealFailure,'^RESTORE_LINUX_ROOT_REQUIRED$'):validate(config()).identity()
            imds.assert_not_called()
    def test_standalone_preparation_identity(self):
        c=config();check_identity(c,{k:c[k] for k in ('account','region','instance')})
    def test_standalone_wrong_guest(self):
        c=config()
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_IDENTITY_REJECTED$'):
            check_identity(c,{k:(SOURCE if k=='instance' else c[k]) for k in ('account','region','instance')})
    def test_duplicate_configuration_json(self):
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_JSON_REJECTED$'):unique([('instance',SOURCE),('instance',TARGET)])


class Roles(unittest.TestCase):
    def test_exact_project_recipe(self):
        r=role_recipe(globals_recipe(),'nextcloud');self.assertFalse(r['nextcloud']['superuser']);self.assertTrue(r['postgres']['superuser'])
    def test_unknown_role(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_GLOBALS_REVIEW_REQUIRED$'):
            role_recipe(globals_recipe()+'CREATE ROLE unrelated;','nextcloud')
    def test_drop_statement(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_GLOBALS_REVIEW_REQUIRED$'):
            role_recipe(globals_recipe()+'DROP DATABASE nextcloud;','nextcloud')
    def test_password_not_in_globals(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_GLOBALS_REVIEW_REQUIRED$'):
            role_recipe(globals_recipe()+"ALTER ROLE nextcloud PASSWORD 'synthetic';",'nextcloud')
    def test_privileged_application_role(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_OWNER_PRIVILEGES_REJECTED$'):
            role_recipe(globals_recipe().replace('NOSUPERUSER','SUPERUSER'),'nextcloud')
    def test_duplicate_attributes(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_GLOBALS_REVIEW_REQUIRED$'):
            role_recipe(globals_recipe().replace('NOSUPERUSER INHERIT','NOSUPERUSER NOSUPERUSER INHERIT'),'nextcloud')
    def test_restrict_metacommands_are_not_executed(self):
        token='x'*32
        self.assertEqual(set(role_recipe('\\restrict '+token+'\n'+globals_recipe()+'\\unrestrict '+token,'nextcloud')),{'postgres','nextcloud'})
    def test_shell_psql_metacommand_rejected(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_GLOBALS_REVIEW_REQUIRED$'):
            role_recipe(globals_recipe()+'\\! unsafe','nextcloud')


class PostgreSQL(unittest.TestCase):
    """Isolate the real adapter algorithm via __new__; not operational evidence."""
    def setUp(self):
        self.pg=object.__new__(PostgreSQLDestination);self.pg.database='nextcloud';self.pg.owner='nextcloud'
        self.pg.fence=SimpleNamespace(assert_fenced=Mock())
        self.pg.guard=Mock();self.pg.command=Mock(return_value=b'');self.pg.supervisor=Mock()
        self.pg.operation='synthetic';self.pg.container='a'*64
    def parameters(self):
        metadata={'encoding':'UTF8','collate':'C','ctype':'C','locale_provider':'c',
                  'collation_version':None,'server_version_num':170011,'custom_tablespaces':0}
        return SimpleNamespace(database={'database':'nextcloud','owner':'nextcloud',**metadata},globals_sql=globals_recipe())
    def roles_document(self):
        return {'roles':[{'name':name,**attrs} for name,attrs in role_recipe(globals_recipe(),'nextcloud').items()]}
    def assert_roles_response(self,document):
        parameters=self.parameters();metadata={k:v for k,v in parameters.database.items() if k not in ('database','owner')}
        self.pg.owner=None
        self.pg.command.side_effect=[json.dumps(metadata).encode(),json.dumps(document).encode()]
        self.pg.assert_roles(parameters)
    def test_roles_use_real_parser_and_object_envelope(self):
        self.assert_roles_response(self.roles_document());self.assertEqual(self.pg.owner,'nextcloud')
        sql=self.pg.command.call_args.args[0][-1]
        self.assertIn("SELECT json_build_object('roles',coalesce(json_agg",sql)
    def test_old_array_response_rejected_with_exact_parser_code(self):
        with self.assertRaisesRegex(SealFailure,'^JSON_OBJECT_REQUIRED$'):
            self.assert_roles_response(self.roles_document()['roles'])
        self.assertIsNone(self.pg.owner)
    def test_invalid_roles_envelopes_and_rows_fail_closed(self):
        valid=self.roles_document()
        cases=[{}, {'roles':None}, {'roles':[]}, {'roles':valid['roles'],'extra':True},
               {'roles':[None,valid['roles'][1]]}, {'roles':[valid['roles'][0],valid['roles'][0]]}]
        for document in cases:
            with self.subTest(document_kind=type(document).__name__),self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_REVIEW_REQUIRED$'):
                self.assert_roles_response(document)
            self.assertIsNone(self.pg.owner)
    def test_integer_role_flags_rejected_even_when_equal_to_boolean(self):
        document=self.roles_document();document['roles'][0]['superuser']=1
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_REVIEW_REQUIRED$'):self.assert_roles_response(document)
        self.assertIsNone(self.pg.owner)
    def test_privilege_mismatch_rejected_before_owner_accepted(self):
        document=self.roles_document();next(r for r in document['roles'] if r['name']=='nextcloud')['superuser']=True
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_REVIEW_REQUIRED$'):self.assert_roles_response(document)
        self.assertIsNone(self.pg.owner)
    def test_duplicate_json_reason_preserved(self):
        self.pg.command.return_value=b'{"roles":[],"roles":[]}'
        with self.assertRaisesRegex(SealFailure,'^DUPLICATE_JSON_KEY$'):self.pg.query('nextcloud','SELECT 1')
    def test_all_restore_catalog_queries_produce_objects(self):
        tree=ast.parse((ROOT/'scripts/restore/postgres_adapter.py').read_text())
        queries=[node.args[1].value for node in ast.walk(tree)
                 if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='query'
                 and len(node.args)==2 and isinstance(node.args[1],ast.Constant)]
        self.assertEqual(len(queries),3)
        for sql in queries:self.assertTrue(sql.lstrip().startswith('SELECT json_build_object('))
    def test_exact_restore_command(self):
        source=Mock();self.pg.execute(['pg_restore','--exit-on-error','--single-transaction','--dbname','nextcloud'],source)
        self.pg.fence.assert_fenced.assert_called_once()
        args=self.pg.command.call_args.args[0]
        self.assertEqual(args,['pg_restore','--host=/var/run/postgresql','--username=postgres','--role','nextcloud','--exit-on-error','--single-transaction','--dbname','nextcloud'])
    def test_cleanup_flag_rejected_before_execution(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_POSTGRES_COMMAND_REJECTED$'):
            self.pg.execute(['pg_restore','--clean'],Mock())
        self.pg.command.assert_not_called()
    def test_restore_without_fencing_rejected(self):
        self.pg.fence=None
        with self.assertRaisesRegex(SealFailure,'^RESTORE_FENCE_REQUIRED$'):
            self.pg.execute(['pg_restore','--exit-on-error','--single-transaction','--dbname','nextcloud'],Mock())
    def test_fence_loss_rejected(self):
        self.pg.fence.assert_fenced.side_effect=SealFailure('POSTGRES_FENCE_LOST')
        with self.assertRaisesRegex(SealFailure,'^POSTGRES_FENCE_LOST$'):
            self.pg.execute(['pg_restore','--exit-on-error','--single-transaction','--dbname','nextcloud'],Mock())
        self.pg.command.assert_not_called()
    def test_catalog_uses_supervisor(self):
        source=Mock();self.pg.check_catalog(source)
        self.assertEqual(self.pg.supervisor.execute.call_args.args[2],['pg_restore','--list'])
    def test_query_invalid_identifier(self):
        with self.assertRaisesRegex(SealFailure,'^RESTORE_DATABASE_IDENTIFIER_REJECTED$'):
            self.pg.query('db;DROP DATABASE original','SELECT 1')
        self.pg.command.assert_not_called()
    def test_owner_role_must_be_verified(self):
        self.pg.owner=None
        with self.assertRaisesRegex(SealFailure,'^RESTORE_ROLES_REVIEW_REQUIRED$'):
            self.pg.execute(['pg_restore','--exit-on-error','--single-transaction','--dbname','nextcloud'],Mock())
        self.pg.command.assert_not_called()


class Preparation(unittest.TestCase):
    def test_yaml_role_names_and_no_application_role(self):
        p=yaml.safe_load((ROOT/'ansible/playbooks/restore-prepare.yml').read_text())[0]
        self.assertEqual(p['roles'],['common','docker','restore_prepare'])
        self.assertNotIn('nextcloud_stack',p['roles'])
    def test_metadata_guard_before_roles(self):
        p=yaml.safe_load((ROOT/'ansible/playbooks/restore-prepare.yml').read_text())[0]
        self.assertIn('pre_tasks',p);task=p['pre_tasks'][1]
        self.assertFalse(task['changed_when']);self.assertTrue(task['no_log'])
        self.assertIn('prepare_identity.py',task['ansible.builtin.command']['argv'][2])
    def test_no_application_start_or_dependency_network_fallback(self):
        tasks=yaml.safe_load((ROOT/'ansible/roles/restore_prepare/tasks/main.yml').read_text())
        for task in tasks:
            self.assertNotIn('ansible.builtin.shell',task)
            self.assertNotIn('ansible.builtin.service',task)
            self.assertNotIn('community.docker.docker_compose_v2',task)
        setup=[t for t in tasks if 'creates' in t.get('ansible.builtin.command',{})]
        self.assertEqual(len(setup),1)
    def test_worker_import_verified_before_private_binding(self):
        tasks=yaml.safe_load((ROOT/'ansible/roles/restore_prepare/tasks/main.yml').read_text())
        index=next(i for i,t in enumerate(tasks) if t['name']=='Verify restore worker imports with the host runtime dependencies')
        task=tasks[index]
        self.assertFalse(task['changed_when'])
        command=task['ansible.builtin.command']
        self.assertEqual(command['argv'][0],'/usr/local/lib/nextcloud-recovery/venv/bin/python')
        self.assertIn('import scripts.restore.host_worker',command['argv'][-1])
        self.assertIn("version('PyYAML') == '6.0.3'",command['argv'][-1])
        binding=next(i for i,t in enumerate(tasks) if t['name']=='Install private target binding without credentials')
        self.assertLess(index,binding)
    def test_restore_prepare_requires_explicit_workflow_choice(self):
        text=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn('default: new_installation',text)
        self.assertIn('options: [new_installation, prepare_restore, create_restore_target]',text)





class SharedPreparation(unittest.TestCase):
    def guard(self):
        h=config();base='arn:aws:iam::'+h['account']+':role/'+h['project']+'-restore'
        return dict(version=2, **{k:h[k] for k in ('account','region','instance','project','data_bucket','backups_bucket','state_bucket','images','deployment_root')},
                    environment='restore', installation_identity='a'*64, staging_root='/var/lib/nextcloud-recovery',
                    admin_role=base+'-data-policy-admin', emergency_role=base+'-data-policy-emergency',
                    backup_role=base+'-backup', guard_role=base+'-deploy-guard', storage_encryption='SSE-S3', repository='fixture/platform')
    def test_shared_binding_matches_destination(self):
        from scripts.restore.host_scope import validate_shared_preparation
        self.assertEqual(validate_shared_preparation(config(),self.guard())['instance'],TARGET)
    def test_shared_binding_rejects_source_instance(self):
        from scripts.restore.host_scope import validate_shared_preparation
        g=self.guard();g['instance']=SOURCE
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SHARED_GUARD_SCOPE_MISMATCH$'):validate_shared_preparation(config(),g)
    def test_shared_binding_rejects_other_data_bucket(self):
        from scripts.restore.host_scope import validate_shared_preparation
        g=self.guard();g['data_bucket']='fixture-other-destination'
        with self.assertRaisesRegex(SealFailure,'^RESTORE_SHARED_GUARD_SCOPE_MISMATCH$'):validate_shared_preparation(config(),g)
    def test_shared_binding_rejects_activation_flag(self):
        from scripts.restore.host_scope import validate_shared_preparation
        from scripts.backup.capture.storage import CaptureFailure
        g=self.guard();g['operational_allowed']=True
        with self.assertRaisesRegex(CaptureFailure,'^HOST_CONFIGURATION_INVALID$'):validate_shared_preparation(config(),g)

class SharedPreparationTasks(unittest.TestCase):
    def tasks(self):return yaml.safe_load((ROOT/'ansible/roles/restore_prepare/tasks/main.yml').read_text(encoding='utf-8'))
    def test_existing_guard_is_not_overwritten_without_explicit_configuration(self):
        task=next(t for t in self.tasks() if t['name']=='Install unchanged PR7 lease binding without enabling backup dispatchers')
        self.assertEqual(task['when'],'restore_shared_guard_configuration is defined')
        self.assertEqual(task['ansible.builtin.copy']['mode'],'0600')
    def test_existing_guard_uses_original_strict_loader_and_scope_check(self):
        task=next(t for t in self.tasks() if t['name']=='Verify shared lease configuration against the destination binding')
        command=task['ansible.builtin.command']['argv'][-1]
        self.assertIn('runtime_config import load',command)
        self.assertIn("g=p['guard'] if p['provided'] else load()",command)
        self.assertIn('validate_shared_preparation',command)

if __name__=='__main__':unittest.main()
