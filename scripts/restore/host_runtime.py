"""Explicit Linux destination operations. Not imported by the production dispatcher.

Every mutation requires the separate held restore control. Failure preserves the
destination and journals; no cleanup, reinstall, container removal or force retry.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from scripts.backup.sealing.common import require,SealFailure
from scripts.backup.safe_files import SafeRoot
from scripts.backup.capture.storage import sync_directory
from scripts.backup.operational.docker_api import DockerExecAPI
from scripts.backup.operational.supervision import Supervisor
from scripts.restore.control import RestoreControl
from scripts.restore.host_scope import RestoreHost
from scripts.restore.private_files import write_new
from scripts.restore.runtime import destination_runtime,remap_request


def validate_html_volume(value,name,project,peers):
    require(isinstance(value,dict) and value.get('Name')==name and value.get('Driver')=='local'
            and value.get('Scope')=='local' and value.get('Options') in (None,{})
            and value.get('Mountpoint')=='/var/lib/docker/volumes/'+name+'/_data'
            and value.get('Labels',{}).get('com.docker.compose.project')==project
            and value.get('Labels',{}).get('com.docker.compose.volume')=='nextcloud_html',
            'RESTORE_HTML_VOLUME_REJECTED')
    require(isinstance(peers,list) and all(isinstance(p,dict) and isinstance(p.get('Mounts'),list) for p in peers),
            'RESTORE_HTML_VOLUME_INSPECTION_REQUIRED')
    require(not any(m.get('Name')==name or m.get('Source')==value['Mountpoint']
                    for peer in peers for m in peer['Mounts']),'RESTORE_HTML_VOLUME_IN_USE')


class HostRuntime:
    def __init__(self,host,api,control,store,operation,reviewed_root,public_ip):
        import docker
        require(type(host) is RestoreHost and type(api) is docker.APIClient and type(control) is RestoreControl
                and control.api is api and control.host is host and control.staging is store,
                'RESTORE_REAL_RUNTIME_REQUIRED')
        self.host,self.api,self.control,self.store,self.operation=host,api,control,store,operation
        self.reviewed_root=Path(reviewed_root);self.public_ip=public_ip
        require(self.reviewed_root==Path('/usr/local/lib/nextcloud-recovery/repository'),
                'RESTORE_REVIEWED_CODE_PATH_REJECTED')
        self.directory=store.operation(operation);self.root=Path(host.config['deployment_root'])/'docker'
        self.supervisor=None;self.html=None;self.helper=None;self.resumed_database=None
        if (self.directory/'exec-journal.json').exists():
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            self.supervisor=Supervisor(self.store,DockerExecAPI(self.api),boot)

    def owned_helper(self):
        path=self.directory/'remap-helper.json'
        if not path.exists():return None
        record=self.store.read(self.operation+'/remap-helper.json')
        require(set(record)=={'container_id','image','name'} and re.fullmatch('[a-f0-9]{64}',record['container_id'])
                and record['image']==self.host.config['images']['nextcloud']
                and record['name']=='nextcloud-restore-'+self.operation,'RESTORE_REMAP_HELPER_AMBIGUOUS')
        return record

    def guard(self):self.control.assert_held();self.host.identity()

    def assert_empty(self):
        self.guard()
        require(not self.api.containers(all=True) and not self.api.volumes().get('Volumes'),
                'RESTORE_RUNTIME_NOT_EMPTY')
        require(not self.root.is_symlink() and self.root.is_dir() and not any(self.root.iterdir()),
                'RESTORE_RUNTIME_NOT_EMPTY')
        for image in self.host.config['images'].values():
            observed=self.api.inspect_image(image)
            require(image.split(':',1)[0]+'@'+image.split('@')[1] in observed.get('RepoDigests',[]),
                    'RESTORE_PINNED_IMAGE_NOT_AVAILABLE')

    def command(self,args,input_bytes=None):
        self.guard();relative=self.operation+'/host-command.json'
        require(isinstance(args,list) and all(isinstance(a,str) and '\x00' not in a for a in args),
                'RESTORE_HOST_COMMAND_INVALID')
        compose_prefix=['/usr/bin/docker','--host=unix:///var/run/docker.sock','compose',
                        '--project-name',self.host.config['project'],'--env-file',str(self.root/'.env'),
                        '-f',str(self.root/'restore-compose.yml')]
        tails=(['up','-d','--no-deps','--pull','never','--no-build','postgres','redis'],
               ['up','-d','--no-deps','--pull','never','--no-build','nextcloud'],['restart','nextcloud'])
        require((input_bytes is None and any(args==compose_prefix+tail for tail in tails))
                or (args==self.remap_command() and isinstance(input_bytes,bytes) and len(input_bytes)<=16384),
                'RESTORE_HOST_COMMAND_REJECTED')
        old_path=self.store.root/relative
        if old_path.exists():require(self.store.read(relative).get('phase')=='finished','RESTORE_HOST_COMMAND_INTERRUPTED')
        record={'kind':'restore-host-command','phase':'start_intent',
                'command_sha256':hashlib.sha256('\0'.join(args).encode()).hexdigest()}
        self.store.write(relative,record)
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/root','COMPOSE_ANSI':'never',
             'DOCKER_HOST':'unix:///var/run/docker.sock'}
        try:
            result=subprocess.run(args,cwd=self.root,env=env,input=input_bytes,stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,timeout=900,check=False)
            require(result.returncode==0,'RESTORE_HOST_COMMAND_FAILED')
        except BaseException as error:
            if isinstance(error,subprocess.TimeoutExpired):code='RESTORE_HOST_COMMAND_TIMEOUT'
            elif isinstance(error,SealFailure):code=error.code
            elif isinstance(error,OSError):code='RESTORE_HOST_COMMAND_START_FAILED'
            else:code='RESTORE_HOST_COMMAND_INTERRUPTED'
            record.update(phase='intervention_required',error_code=code);self.store.write(relative,record)
            raise SealFailure(code) from None
        record['phase']='finished';self.store.write(relative,record);self.guard()

    def compose(self,*args):
        require(tuple(args) in (('up','-d','--no-deps','--pull','never','--no-build','postgres','redis'),
                               ('up','-d','--no-deps','--pull','never','--no-build','nextcloud'),
                               ('restart','nextcloud')),'RESTORE_COMPOSE_COMMAND_REJECTED')
        self.command(['/usr/bin/docker','--host=unix:///var/run/docker.sock','compose',
                      '--project-name',self.host.config['project'],'--env-file',str(self.root/'.env'),
                      '-f',str(self.root/'restore-compose.yml'),*args])

    def containers(self,required):
        self.guard();found={};project=self.host.config['project']
        owned=self.owned_helper()
        for item in self.api.containers(all=True):
            info=self.api.inspect_container(item['Id']);labels=info.get('Config',{}).get('Labels',{})
            if owned is not None and item['Id']==owned['container_id']:
                require(info['State'].get('Running') is False and info['State'].get('ExitCode')==0
                        and info['Config'].get('Image')==self.host.config['images']['nextcloud']
                        and info['HostConfig'].get('NetworkMode')=='none'
                        and info.get('Name')=='/'+owned['name']
                        and info['Config'].get('Cmd')==['/opt/remap.php','/var/www/html/config'],
                        'RESTORE_REMAP_HELPER_AMBIGUOUS')
                continue
            name=labels.get('com.docker.compose.service')
            require(labels.get('com.docker.compose.project')==project and name in ('nextcloud','postgres','redis')
                    and name not in found,'RESTORE_EXTERNAL_CONTAINER_REJECTED')
            require(info.get('Config',{}).get('Image')==self.host.config['images'][name]
                    and info.get('State',{}).get('Running') is True,'RESTORE_CONTAINER_IDENTITY_REJECTED')
            found[name]=info
        require(set(found)==set(required),'RESTORE_CONTAINERS_NOT_READY');return found

    def wait_healthy(self,required,nextcloud_maintenance=False):
        deadline=time.monotonic()+360
        while time.monotonic()<deadline:
            containers=self.containers(required)
            statuses=[v['State'].get('Health',{}).get('Status') for name,v in containers.items()
                      if not (name=='nextcloud' and nextcloud_maintenance)]
            if all(s=='healthy' for s in statuses):return containers
            require(all(s in ('starting','healthy','unhealthy') for s in statuses),'RESTORE_HEALTH_STATUS_INVALID')
            time.sleep(3)
        raise SealFailure('RESTORE_HEALTH_TIMEOUT')

    def application_status(self,manifest):
        from scripts.backup.operational.postgres import Limited,Discard
        from scripts.backup.json_input import parse_json
        from scripts.backup.limits import Limits
        self.guard();container=self.containers(('postgres','redis','nextcloud'))['nextcloud']['Id']
        require(self.supervisor is not None,'RESTORE_SUPERVISOR_REQUIRED')
        self.supervisor.execute(self.operation,container,['php','/opt/platform/assert-restored.php'],
                                'www-data',Discard(),30)
        out=Limited()
        self.supervisor.execute(self.operation,container,['php','/var/www/html/occ','status','--output=json'],
                                'www-data',out,60)
        status=parse_json(out.getvalue(),Limits(json_bytes=16384))
        expected=manifest['consistency']['maintenance_initially_enabled']
        require(isinstance(status,dict) and status.get('installed') is True and status.get('maintenance') is expected
                and status.get('needsDbUpgrade') is False
                and status.get('versionstring')==manifest['compatibility']['nextcloud_version'],
                'RESTORE_APPLICATION_STATUS_REJECTED')
        return {'installed':True,'maintenance':expected,'version':status['versionstring'],
                'identity_matches':True,'restore_proven':False}

    def install_runtime(self,parameters,manifest):
        self.guard();require(not any(self.root.iterdir()),'RESTORE_RUNTIME_NOT_EMPTY')
        staged=self.directory/'runtime'
        with SafeRoot(staged) as safe:
            for name in parameters.runtime_code:
                with safe.open(name) as stream:data=stream.read(1024**2+1)
                write_new(self.root/name,data,0o755 if name.endswith('.sh') else 0o644)
            (self.root/'secrets').mkdir(mode=0o700)
            for name,data in parameters.secrets.items():write_new(self.root/'secrets'/name,data,0o444)
        recipe,env=destination_runtime(parameters,self.host.config,self.public_ip,manifest)
        write_new(self.root/'restore-compose.yml',recipe);write_new(self.root/'.env',env)
        for name in ('restore-start.sh','assert-restored.php'):
            with SafeRoot(self.reviewed_root/'scripts/restore') as safe,safe.open(name) as stream:
                data=stream.read(1024**2+1)
            write_new(self.root/name,data,0o755 if name.endswith('.sh') else 0o644)
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.supervisor=Supervisor(self.store,DockerExecAPI(self.api),boot);self.supervisor.initialize(self.operation)

    def verify_resume_files(self,parameters,manifest):
        self.guard();require(self.supervisor is not None,'RESTORE_SUPERVISOR_REQUIRED')
        recipe,env=destination_runtime(parameters,self.host.config,self.public_ip,manifest)
        expected={**parameters.runtime_code,'restore-compose.yml':recipe,'.env':env}
        with SafeRoot(self.reviewed_root/'scripts/restore') as safe:
            for name in ('restore-start.sh','assert-restored.php'):
                with safe.open(name) as f:expected[name]=f.read(1024**2+1)
        require({p.name for p in self.root.iterdir()}==set(expected)|{'secrets'},'RESTORE_RESUME_RUNTIME_CHANGED')
        with SafeRoot(self.root) as safe:
            for name,content in expected.items():
                info=(self.root/name).lstat()
                mode=0o600 if name in ('.env','restore-compose.yml') else 0o755 if name.endswith('.sh') else 0o644
                require(stat.S_ISREG(info.st_mode) and info.st_uid==info.st_gid==0
                        and stat.S_IMODE(info.st_mode)==mode,'RESTORE_RESUME_RUNTIME_CHANGED')
                with safe.open(name) as f:require(f.read(1024**2+1)==content,'RESTORE_RESUME_RUNTIME_CHANGED')
            safe.assert_unchanged()
        require({p.name for p in (self.root/'secrets').iterdir()}==set(parameters.secrets),
                'RESTORE_RESUME_RUNTIME_CHANGED')
        with SafeRoot(self.root/'secrets') as safe:
            for name,content in parameters.secrets.items():
                info=(self.root/'secrets'/name).lstat()
                require(info.st_uid==info.st_gid==0 and stat.S_IMODE(info.st_mode)==0o444,
                        'RESTORE_RESUME_RUNTIME_CHANGED')
                with safe.open(name) as f:require(f.read(4097)==content,'RESTORE_RESUME_RUNTIME_CHANGED')
            safe.assert_unchanged()
    def revalidate_pre_database(self,parameters,manifest):
        self.guard();require(self.supervisor is not None,'RESTORE_SUPERVISOR_REQUIRED')
        self.verify_resume_files(parameters,manifest)
        containers=self.containers(('postgres','redis'))
        require(all(c['State'].get('Health',{}).get('Status')=='healthy' for c in containers.values()),
                'RESTORE_RESUME_CONTAINERS_REJECTED')
        from scripts.restore.postgres_adapter import PostgreSQLDestination
        pg=PostgreSQLDestination(self.host,self.api,self.supervisor,self.operation,containers['postgres']['Id'])
        before=pg.inspect_database(parameters.database['database'])
        require(before.get('server_major')==17 and before.get('database')==parameters.database['database']
                and before.get('owner')==parameters.database['owner'] and before.get('tables')==[]
                and before.get('sessions')==[] and before.get('extensions')==parameters.database['extensions'],
                'RESTORE_RESUME_DATABASE_NOT_EMPTY')
        pg.assert_roles(parameters)
        self.resumed_database=containers['postgres']['Id']

    def start_database(self):
        if self.resumed_database is not None:
            containers=self.wait_healthy(('postgres','redis'))
            require(containers['postgres']['Id']==self.resumed_database,'RESTORE_RESUME_DATABASE_CHANGED')
            return self.resumed_database
        self.compose('up','-d','--no-deps','--pull','never','--no-build','postgres','redis')
        return self.wait_healthy(('postgres','redis'))['postgres']['Id']

    def remap_command(self):
        # Root-private staged tree, not a live mount. Network is disabled for the
        # helper and no operational credentials/secrets appear in its environment.
        helper_name='nextcloud-restore-'+self.operation
        return ['/usr/bin/docker','--host=unix:///var/run/docker.sock','run','--name',helper_name,
              '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
              '--log-driver','none','--user','0:0','--interactive','--entrypoint','php',
              '--env','NEXTCLOUD_S3_BUCKET='+self.host.config['source_data_bucket'],
              '--env','NEXTCLOUD_S3_REGION='+self.host.config['region'],
              '--env','NEXTCLOUD_TRUSTED_DOMAINS='+self.public_ip,
              '--mount','type=bind,src='+str(self.directory/'html')+',dst=/var/www/html',
              '--mount','type=bind,src='+str(self.reviewed_root/'scripts/restore/remap-config.php')+',dst=/opt/remap.php,readonly',
              '--pull','never',self.host.config['images']['nextcloud'],'/opt/remap.php','/var/www/html/config']

    def remap(self,manifest):
        self.guard();helper_name='nextcloud-restore-'+self.operation
        self.command(self.remap_command(),remap_request(manifest,self.host.config,self.public_ip))
        # Stopped helper is retained with the command journal; never remove it or
        # auto-retry. Explicitly identify it before excluding it from peer checks.
        self.helper=self.api.inspect_container(helper_name)
        require(self.helper.get('State',{}).get('Running') is False and self.helper['State'].get('ExitCode')==0,
                'RESTORE_REMAP_HELPER_AMBIGUOUS')
        self.store.write(self.operation+'/remap-helper.json',{'container_id':self.helper['Id'],
                         'image':self.host.config['images']['nextcloud'],'name':helper_name})

    def publish_html(self,artifact):
        self.guard();project=self.host.config['project'];name=project+'_nextcloud_html'
        existing=[v for v in self.api.volumes().get('Volumes',[]) or [] if v.get('Name')==name]
        require(len(existing)<=1,'RESTORE_HTML_VOLUME_REJECTED')
        if existing:
            # Compose may initialize declared volumes while starting PG/Redis.
            # Accept only the same project's still-empty and unmounted HTML volume.
            value=self.api.inspect_volume(name)
        else:
            value=self.api.create_volume(name=name,driver='local',labels={'com.docker.compose.project':project,
                                                                         'com.docker.compose.volume':'nextcloud_html'})
        peers=[self.api.inspect_container(c['Id']) for c in self.api.containers(all=True)]
        validate_html_volume(value,name,project,peers)
        target=Path(value['Mountpoint'])
        require(target.is_dir() and not target.is_symlink() and not any(target.iterdir()),'RESTORE_HTML_VOLUME_NOT_EMPTY')
        from scripts.restore.archives import inspect_archive
        from scripts.restore.private_files import verified_file
        filename=artifact['key'].rsplit('/',1)[1]
        members=verified_file(self.directory,filename,artifact,inspect_archive)
        require(all(m.uid in (0,33) and m.gid in (0,33) for m in members.values()),'RESTORE_HTML_OWNERSHIP_REVIEW_REQUIRED')
        source=self.directory/'html'
        # Exclusive destination and same-filesystem moves only. Partial state is retained.
        require(source.stat().st_dev==target.stat().st_dev,'RESTORE_HTML_FILESYSTEM_MISMATCH')
        for child in source.iterdir():os.rename(child,target/child.name)
        for relative,m in members.items():
            path=target if relative=='.' else target/relative
            os.chown(path,m.uid,m.gid,follow_symlinks=False)
            if not m.issym():os.chmod(path,m.mode & 0o777,follow_symlinks=False)
        sync_directory(target);self.html=target
