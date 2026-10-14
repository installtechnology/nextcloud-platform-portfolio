"""Real, supervised Docker/PostgreSQL destination adapter; no calls on import.

Must be wired to an authorized restore controller, not the production dispatcher.
No captured SQL execution, cleanup, DB deletion or credentials in argv/output.
"""
import re
from scripts.backup.operational.postgres import Limited,Discard
from scripts.backup.operational.supervision import Supervisor
from scripts.backup.operational.docker_api import DockerExecAPI
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.json_input import parse_json
from scripts.backup.errors import ValidationFailure
from scripts.backup.sealing.common import require,SealFailure
from scripts.restore.host_scope import RestoreHost
from scripts.restore.roles import role_recipe
from scripts.backup.operational.database_fence import DatabaseFence
from scripts.backup.operational.postgres import PostgreSQL


class PostgreSQLDestination:
    def __init__(self,host,api,supervisor,operation,container):
        import docker
        require(type(host) is RestoreHost and type(supervisor) is Supervisor
                and type(supervisor.docker) is DockerExecAPI and type(api) is docker.APIClient
                and api.base_url=='http+docker://localhost'
                and api._custom_adapter.socket_path=='/var/run/docker.sock'
                and supervisor.docker.api is api,'RESTORE_REAL_ADAPTERS_REQUIRED')
        require(re.fullmatch('[a-f0-9]{64}',container) is not None,'RESTORE_POSTGRES_CONTAINER_INVALID')
        self.host,self.api,self.supervisor,self.operation,self.container=host,api,supervisor,operation,container
        self.database=None
        self.owner=None
        self.fence=None

    def guard(self):
        self.host.identity();info=self.api.inspect_container(self.container);c=self.host.config
        config=info.get('Config',{});labels=config.get('Labels',{})
        require(info.get('Id')==self.container and config.get('Image')==c['images']['postgres']
                and labels.get('com.docker.compose.project')==c['project']
                and labels.get('com.docker.compose.service')=='postgres'
                and info.get('State',{}).get('Running') is True,'RESTORE_POSTGRES_CONTAINER_MISMATCH')
        image=self.api.inspect_image(c['images']['postgres'])
        require(info.get('Image')==image.get('Id') and
                'postgres@'+c['images']['postgres'].split('@')[1] in image.get('RepoDigests',[]),
                'RESTORE_POSTGRES_IMAGE_MISMATCH')
        image_config=image.get('Config',{});security=info.get('HostConfig',{})
        require(isinstance(image_config.get('Entrypoint'),list) and image_config.get('Cmd')==['postgres']
                and config.get('Entrypoint')==image_config['Entrypoint'] and config.get('Cmd')==['postgres']
                and security.get('Privileged') is False and security.get('CapAdd') in (None,[])
                and security.get('PidMode')=='' and security.get('IpcMode')=='private'
                and security.get('PortBindings') in (None,{})
                and security.get('NetworkMode')==c['project']+'_backend',
                'RESTORE_POSTGRES_SECURITY_MISMATCH')
        mounts=[m for m in info.get('Mounts',[]) if m.get('Destination')=='/var/lib/postgresql/data']
        require(len(mounts)==1 and mounts[0].get('Type')=='volume' and mounts[0].get('RW') is True
                and mounts[0].get('Name')==c['project']+'_postgres_data','RESTORE_POSTGRES_VOLUME_MISMATCH')
        self.volume=mounts[0]['Source']
        for summary in self.api.containers(all=True):
            if summary['Id']==self.container:continue
            peer=self.api.inspect_container(summary['Id'])
            require(not any(m.get('RW') and (m.get('Name')==mounts[0]['Name'] or m.get('Source')==self.volume)
                            for m in peer.get('Mounts',[])),'RESTORE_EXTERNAL_DATABASE_WRITER')
        self.supervisor.assert_idle(self.operation)
        self.supervisor.assert_no_untracked_execs(self.operation,[self.container])

    def command(self,args,input_file=None,timeout=60):
        self.guard();output=Limited()
        try:
            self.supervisor.execute(self.operation,self.container,args,'postgres',output,timeout,input_file)
        except CaptureFailure as e:
            raise SealFailure(e.code) from None
        return output.getvalue()

    def query(self,database,sql):
        require(re.fullmatch('[a-z][a-z0-9_]{1,62}',database) is not None,'RESTORE_DATABASE_IDENTIFIER_REJECTED')
        try:
            return parse_json(self.command(['psql','--host=/var/run/postgresql','--username=postgres',
                '-X','-A','-t','-v','ON_ERROR_STOP=1','--dbname',database,'-c',sql]))
        except ValidationFailure as error:
            # Preserve the bounded parser's reason code, never the SQL/response.
            raise SealFailure(error.code) from None

    def inspect_database(self,database):
        self.database=database
        return self.query(database,"""SELECT json_build_object(
          'server_major',current_setting('server_version_num')::int/10000,
          'database',current_database(),'owner',pg_get_userbyid(d.datdba),
          'tables',(SELECT coalesce(json_agg(objects.name ORDER BY objects.name),'[]'::json) FROM (
             SELECT 'relation:'||n.nspname||'.'||c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
               AND NOT EXISTS (SELECT 1 FROM pg_depend dep WHERE dep.classid='pg_class'::regclass AND dep.objid=c.oid AND dep.deptype='e')
             UNION ALL
             SELECT 'function:'||n.nspname||'.'||p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
               AND NOT EXISTS (SELECT 1 FROM pg_depend dep WHERE dep.classid='pg_proc'::regclass AND dep.objid=p.oid AND dep.deptype='e')
             UNION ALL
             SELECT 'type:'||n.nspname||'.'||t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
             WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND t.typtype IN ('d','e')
               AND NOT EXISTS (SELECT 1 FROM pg_depend dep WHERE dep.classid='pg_type'::regclass AND dep.objid=t.oid AND dep.deptype='e')
             ) objects),
          'sessions',(SELECT coalesce(json_agg(pid ORDER BY pid),'[]'::json) FROM pg_stat_activity
                      WHERE backend_type='client backend' AND datname=current_database() AND pid<>pg_backend_pid()),
          'extensions',(SELECT json_agg(json_build_object('name',extname,'version',extversion) ORDER BY extname) FROM pg_extension))
          FROM pg_database d WHERE d.datname=current_database()""")

    def assert_roles(self,parameters):
        owner=parameters.database['owner'];recipe=role_recipe(parameters.globals_sql,owner)
        metadata_keys={'encoding','collate','ctype','locale_provider','collation_version','server_version_num','custom_tablespaces'}
        require(metadata_keys<=set(parameters.database) and parameters.database['custom_tablespaces']==0,
                'RESTORE_POSTGRES_METADATA_REVIEW_REQUIRED')
        observed=self.query(parameters.database['database'],"""SELECT json_build_object(
           'encoding',pg_encoding_to_char(d.encoding),'collate',d.datcollate,'ctype',d.datctype,
           'locale_provider',d.datlocprovider,'collation_version',d.datcollversion,
           'server_version_num',current_setting('server_version_num')::int,
           'custom_tablespaces',(SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')))
           FROM pg_database d WHERE d.datname=current_database()""")
        require(isinstance(observed,dict) and observed=={k:parameters.database[k] for k in metadata_keys},
                'RESTORE_POSTGRES_LOCALE_OR_VERSION_MISMATCH')
        document=self.query(parameters.database['database'],"""SELECT json_build_object('roles',coalesce(json_agg(json_build_object(
           'name',rolname,'superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,
           'inherit',rolinherit,'login',rolcanlogin,'replication',rolreplication,'bypassrls',rolbypassrls)
           ORDER BY rolname),'[]'::json)) FROM pg_roles WHERE rolname !~ '^pg_'""")
        require(isinstance(document,dict) and set(document)=={'roles'},'RESTORE_ROLES_REVIEW_REQUIRED')
        rows=document['roles']
        attributes=set(recipe[owner])
        require(isinstance(rows,list) and len(rows)==2
                and all(isinstance(r,dict) and set(r)==attributes|{'name'}
                        and isinstance(r['name'],str)
                        and all(type(r[k]) is bool for k in attributes) for r in rows)
                and {r['name'] for r in rows}==set(recipe),'RESTORE_ROLES_REVIEW_REQUIRED')
        for row in rows:require({k:v for k,v in row.items() if k!='name'}==recipe[row['name']],
                               'RESTORE_ROLES_REVIEW_REQUIRED')
        self.owner=owner
        # The fresh PostgreSQL initializer already sets passwords from protected
        # restored files. Validate those files separately in the host adapter;
        # never execute arbitrary globals or put passwords in a psql command.

    def check_catalog(self,dump):
        self.guard();self.supervisor.execute(self.operation,self.container,['pg_restore','--list'],
                                              'postgres',Discard(),300,dump)

    def hold_writers(self):
        self.guard();require(self.database is not None and self.fence is None,'RESTORE_FENCE_STATE_INVALID')
        pg=PostgreSQL(self.supervisor,self.operation,self.container,self.database)
        self.fence=DatabaseFence(self.supervisor.store,self.operation,pg,self.volume)
        self.fence.install()

    def release_writers_after_verified_restore(self):
        self.guard();require(self.fence is not None,'RESTORE_FENCE_REQUIRED')
        self.fence.assert_fenced();self.fence.restore()

    def execute(self,args,dump):
        require(self.database is not None and args==['pg_restore','--exit-on-error','--single-transaction',
                                                     '--dbname',self.database],'RESTORE_POSTGRES_COMMAND_REJECTED')
        require(self.fence is not None,'RESTORE_FENCE_REQUIRED');self.fence.assert_fenced()
        require(isinstance(self.owner,str) and re.fullmatch('[a-z][a-z0-9_]{1,62}',self.owner),
                'RESTORE_ROLES_REVIEW_REQUIRED')
        # Run dump statements as the validated non-superuser application owner.
        # The local postgres connection only supplies SET ROLE authority.
        self.command(['pg_restore','--host=/var/run/postgresql','--username=postgres',
                      '--role',self.owner,*args[1:]],dump,1200)
