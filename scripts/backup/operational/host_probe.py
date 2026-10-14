"""Real collectors for the explicit host target. Import never inspects a host.

Stopped Nextcloud config is evaluated in a read-only, network-none pinned helper,
not by bootstrapping the application. Helpers are journaled before Docker calls.
"""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import urllib.request
from .capture_backend import HostProbe, CaptureTarget
from .postgres import PostgreSQL
from .fence_policy import require
from ..capture.storage import CaptureFailure
from ..json_input import parse_json
from ..limits import Limits
from ..safe_files import SafeRoot, signature

# Reviewed monitoring recipe, not a configurable peer allowlist. Changes to the
# deployment recipe require review of these pins and their regression tests.
ALLOY_IMAGE = 'grafana/alloy:v1.20.1@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f'
ALLOY_CONFIG_HASHES = {'config.alloy': '5f3b13cf7cd84c3ceff6f94666b449fb533b6ba107278a9fe1bb38b513f08206',
                       'blackbox.yml': '6c298e69229c1d38c5848a133fc479720117ab0b0578c34fa197e2d1153ac50c'}
ALLOY_COMMAND = ['run', '/etc/alloy/config.alloy', '--storage.path=/var/lib/alloy/data',
                 '--server.http.listen-addr=0.0.0.0:12345', '--disable-reporting', '--server.http.enable-pprof=false']


def monitoring_config(path):
    import stat
    with SafeRoot(path.parent) as safe:
        with safe.open(path.name) as stream:
            info = os.fstat(stream.fileno())
            require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022, 'MONITOR_CONFIG_UNSAFE')
            before = signature(stream);data = stream.read(1048577)
            require(len(data) <= 1048576 and before == signature(stream), 'MONITOR_CONFIG_CHANGED')
        safe.assert_unchanged()
    return data


def approved_monitor(info, target, image_info, read_config=monitoring_config):
    """Recognize one pinned observer; never assert PG quiescence from identity.

    This observer connects TCP for availability probes, not SQL authentication.
    Mandatory DatabaseFence rejects every TCP client and drains prior sessions.
    All other network/volume/process writer checks remain required.
    """
    config=info.get('Config',{});host=info.get('HostConfig',{});labels=config.get('Labels',{})
    require(labels.get('com.docker.compose.project')==target.project
            and labels.get('com.docker.compose.service')=='alloy'
            and labels.get('com.docker.compose.oneoff')=='False', 'EXTERNAL_DATABASE_CONTAINER_REJECTED')
    require(config.get('Image')==ALLOY_IMAGE and info.get('Image')==image_info.get('Id')
            and 'grafana/alloy@'+ALLOY_IMAGE.split('@')[1] in image_info.get('RepoDigests',[]), 'MONITOR_IMAGE_REJECTED')
    require(config.get('Entrypoint')==['/bin/alloy'] and config.get('Cmd')==ALLOY_COMMAND, 'MONITOR_COMMAND_REJECTED')
    require(host.get('ReadonlyRootfs') is True and host.get('Privileged') is False and host.get('PortBindings')=={}
            and host.get('PidMode')=='' and host.get('IpcMode')=='private'
            and host.get('NetworkMode') in (target.project+'_backend',target.project+'_observability')
            and host.get('CapAdd') in (None,[]) and 'ALL' in host.get('CapDrop',[])
            and any(v in ('no-new-privileges','no-new-privileges:true') for v in host.get('SecurityOpt',[])), 'MONITOR_SECURITY_REJECTED')
    require(set(info.get('NetworkSettings',{}).get('Networks',{}))=={target.project+'_backend',target.project+'_observability'}, 'MONITOR_NETWORK_REJECTED')
    env=config.get('Env') or [];allowed={'PATH','HOME','SSL_CERT_FILE','SSL_CERT_DIR','COMPOSE_PROJECT_NAME','ALLOY_DEPLOY_MODE'}
    require(all(isinstance(v,str) and '=' in v and v.split('=',1)[0] in allowed for v in env)
            and sum(v.startswith('COMPOSE_PROJECT_NAME=') for v in env)==1
            and 'COMPOSE_PROJECT_NAME='+target.project in env
            and [v for v in env if v.startswith('ALLOY_DEPLOY_MODE=')]==['ALLOY_DEPLOY_MODE=docker'], 'MONITOR_CREDENTIALS_REJECTED')
    mounts=info.get('Mounts',[]);by_dest={m.get('Destination'):m for m in mounts}
    require(len(mounts)==len(by_dest)==4 and set(by_dest)=={'/var/lib/alloy','/etc/alloy/config.alloy',
            '/etc/alloy/blackbox.yml','/var/lib/docker/containers'}, 'MONITOR_MOUNTS_REJECTED')
    volume=by_dest['/var/lib/alloy']
    require(volume.get('Type')=='volume' and volume.get('Name')==target.project+'_alloy_data'
            and volume.get('Source')=='/var/lib/docker/volumes/'+target.project+'_alloy_data/_data'
            and volume.get('RW') is True, 'MONITOR_MOUNTS_REJECTED')
    for filename,expected in ALLOY_CONFIG_HASHES.items():
        source=PurePosixPath(target.deployment_root)/'monitoring/alloy'/filename;m=by_dest['/etc/alloy/'+filename]
        require(m.get('Type')=='bind' and m.get('Source')==str(source) and m.get('RW') is False, 'MONITOR_MOUNTS_REJECTED')
        data=read_config(Path(str(source))).replace(b'\r\n',b'\n')
        if filename=='blackbox.yml':
            data,count=re.subn(br'^        Host: "[A-Za-z0-9_.:-]+"$',b'        Host: <authorized-host>',data,flags=re.M)
            require(count==1, 'MONITOR_CONFIG_REJECTED')
        require(hashlib.sha256(data).hexdigest()==expected, 'MONITOR_CONFIG_REJECTED')
    logs=by_dest['/var/lib/docker/containers']
    require(logs.get('Type')=='bind' and logs.get('Source')=='/var/lib/docker/containers'
            and logs.get('RW') is False, 'MONITOR_MOUNTS_REJECTED')
    return info['Id']


def imds_identity():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base='http://169.254.169.254/latest/'
    try:
        request=urllib.request.Request(base+'api/token',method='PUT',
            headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
        with opener.open(request,timeout=3) as response: token=response.read(4097)
        require(0 < len(token) <= 4096, 'HOST_IDENTITY_UNAVAILABLE')
        request=urllib.request.Request(base+'dynamic/instance-identity/document',
            headers={'X-aws-ec2-metadata-token':token.decode('ascii')})
        with opener.open(request,timeout=3) as response: document=parse_json(response.read(16385),Limits(json_bytes=16384))
        return {'account':document['accountId'],'region':document['region'],'instance':document['instanceId']}
    except Exception: raise CaptureFailure('HOST_IDENTITY_UNAVAILABLE') from None


class ReadOnlyState:
    def __init__(self, api, store, operation, image, script):
        self.api,self.store,self.operation,self.image=api,store,operation,image
        self.script=Path(script)
        require(self.script.is_absolute() and self.script.is_file() and not self.script.is_symlink(), 'STATE_HELPER_CODE_REQUIRED')
        self.relative=operation+'/state-helper-journal.json'

    def read(self, info):
        path=self.store.root/self.relative
        record=self.store.read(self.relative) if path.exists() else {'operation_id':self.operation,'helpers':[]}
        require(record.get('operation_id') == self.operation and isinstance(record.get('helpers'),list)
                and len(record['helpers']) < 200 and all(r.get('phase') == 'removed' for r in record['helpers']),
                'STATE_HELPER_RECOVERY_REQUIRED')
        mounts=info['Mounts']
        destinations={'/var/www/html','/docker-entrypoint-hooks.d/before-starting/10-platform.sh',
            '/opt/platform/healthcheck.php','/opt/platform/integration.php','/opt/platform/platform.config.php',
            '/opt/platform/storage-guard.php','/opt/platform/install-nextcloud.php',
            '/run/secrets/postgres_password','/run/secrets/nextcloud_admin_password','/run/secrets/redis_password'}
        require(all(m.get('Type') in ('bind','volume') and isinstance(m.get('Source'),str)
                    and m['Source'].startswith('/') and m.get('Destination') in destinations
                    and not Path(m['Source']).is_symlink() for m in mounts), 'STATE_HELPER_MOUNTS_REJECTED')
        binds={m['Source']:{'bind':m['Destination'],'mode':'ro'} for m in mounts}
        require('/run/docker.sock' not in [m['Destination'] for m in mounts], 'STATE_HELPER_MOUNTS_REJECTED')
        binds[str(self.script)]={'bind':'/opt/recovery/read-state.php','mode':'ro'}
        env=info['Config'].get('Env') or []
        require(not any(v.split('=',1)[0] in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN') for v in env),
                'STATIC_APPLICATION_CREDENTIALS_REJECTED')
        item={'phase':'create_intent','container_id':None};record['helpers'].append(item);self.store.write(self.relative,record)
        try:
            config=self.api.create_host_config(binds=binds,network_mode='none',read_only=True,
                cap_drop=['ALL'],security_opt=['no-new-privileges:true'],
                tmpfs={'/tmp':'rw,noexec,nosuid,size=16m'},
                log_config={'Type':'local','Config':{'max-size':'64k','max-file':'1','compress':'false'}})
            created=self.api.create_container(image=self.image,command=['php','/opt/recovery/read-state.php'],
                entrypoint=[],user='www-data',environment=env,network_disabled=True,
                host_config=config,labels={'nextcloud.recovery.helper':self.operation})
            identifier=created['Id']
            require(isinstance(identifier,str) and re.fullmatch(r'[a-f0-9]{64}',identifier), 'STATE_HELPER_ID_INVALID')
            item.update(phase='start_intent',container_id=identifier);self.store.write(self.relative,record)
            self.api.start(identifier)
            outcome=self.api.wait(identifier,timeout=30)
            require(outcome.get('StatusCode') == 0, 'STATE_HELPER_FAILED')
            observed=self.api.inspect_container(identifier)
            require(observed['State']['Running'] is False and observed['State']['ExitCode'] == 0
                    and observed['Config']['Labels'].get('nextcloud.recovery.helper') == self.operation
                    and observed['Config']['Image'] == self.image, 'STATE_HELPER_IDENTITY_CHANGED')
            raw=self.api.logs(identifier,stdout=True,stderr=False,tail=1)
            require(isinstance(raw,bytes) and 0 < len(raw) <= 16384, 'STATE_HELPER_OUTPUT_INVALID')
            document=parse_json(raw,Limits(json_bytes=16384))
            item['phase']='remove_intent';self.store.write(self.relative,record)
            # Only our stopped read-only helper, no application container/volume.
            self.api.remove_container(identifier,v=False,force=False)
            item['phase']='removed';self.store.write(self.relative,record)
            return document
        except CaptureFailure: raise
        except BaseException: raise CaptureFailure('STATE_HELPER_RECOVERY_REQUIRED') from None


class RealHostProbe(HostProbe):
    def __init__(self,target,api,store,operation,supervisor,script,identity=imds_identity):
        require(type(target) is CaptureTarget, 'CAPTURE_TARGET_REQUIRED')
        self.target,self.api,self.store,self.operation,self.supervisor=target,api,store,operation,supervisor
        self.identity_provider=identity
        self.helper=ReadOnlyState(api,store,operation,target.images['nextcloud'],script)
        self.pg=None

    def identity(self): return self.identity_provider()

    def containers(self):
        values=self.api.containers(all=True,filters={'label':['com.docker.compose.project='+self.target.project]})
        result={}
        for value in values:
            labels=value.get('Labels') or {}; service=labels.get('com.docker.compose.service')
            if service not in ('nextcloud','postgres','redis'):continue
            require(service not in result, 'DUPLICATE_APPLICATION_CONTAINER')
            result[service]=self.api.inspect_container(value['Id'])
        require(set(result) == {'nextcloud','postgres','redis'}, 'CAPTURE_CONTAINERS_INVALID')
        return result

    def read_state(self): return self.helper.read(self.containers()['nextcloud'])

    def nextcloud_status(self):
        from .postgres import Limited
        output=Limited()
        code="""$d=preg_split('/\\s+/',trim((string)getenv('NEXTCLOUD_TRUSTED_DOMAINS')));$h=$d[0]??'';
if($h===''||strpbrk($h,"\\r\\n")!==false){exit(2);}
$c=stream_context_create(['http'=>['timeout'=>5,'header'=>"Host: $h\\r\\n"]]);
$s=json_decode((string)@file_get_contents('http://127.0.0.1/status.php',false,$c),true);
if(!is_array($s)){exit(2);}echo json_encode(array_intersect_key($s,array_flip(['installed','maintenance','needsDbUpgrade'])));"""
        self.supervisor.execute(self.operation,self.containers()['nextcloud']['Id'],['php','-r',code],'www-data',output,10)
        value=parse_json(output.getvalue());require(isinstance(value,dict),'NEXTCLOUD_STATUS_UNAVAILABLE');return value

    def baseline(self):
        state=self.read_state();found=self.containers()
        # Reject incompatible observers before maintenance or application stop;
        # repeat the same checks throughout the quiescent window.
        self.assert_database_participants(found)
        require(re.fullmatch(r'[a-z][a-z0-9_]{1,62}',state['database'])
                and re.fullmatch(r'[a-z][a-z0-9_]{1,62}',state['owner']), 'DATABASE_IDENTIFIERS_INVALID')
        self.pg=PostgreSQL(self.supervisor,self.operation,found['postgres']['Id'],state['database'])
        # Reuse metadata query only, not the PR3 fixture constructor/guards.
        from ..capture.docker_backend import DockerBackend
        class Query:
            db=state['database']
            def exec_pg(_,executable,*args):
                from .postgres import Limited
                output=Limited();self.pg.command(executable,list(args),output,120);return output.getvalue()
        metadata=DockerBackend.database_metadata(Query())
        require(metadata['server_version_num']//10000 == 17 and metadata['owner'] == state['owner']
                and metadata['custom_tablespaces'] == 0, 'POSTGRES_COMPATIBILITY_FAILED')
        self.pg.compatibility()
        html=next(m['Source'] for m in found['nextcloud']['Mounts'] if m['Destination']=='/var/www/html')
        size=sum(p.lstat().st_size for p in Path(html).rglob('*') if p.is_file() and not p.is_symlink())
        return {'maintenance':state['maintenance'],'writers':{'nextcloud':True},
            'config_fingerprint':state['config_fingerprint'],'capture_sources':[html,self.target.deployment_root],
            'estimated_capture_bytes':2*(size+metadata['database_bytes'])+64*1024*1024,
            'dependencies_running':{'postgres':True,'redis':True},'identity':state['installation_identity_sha256'],
            'containers':{s:{'id':i['Id'],'image_id':i['Image'],'mounts':i['Mounts']} for s,i in found.items()},
            'database':state['database'],'owner':state['owner'],'table_prefix':state['table_prefix']}

    def assert_database_participants(self, found):
        participants={i['Id'] for i in found.values()}
        postgres=found['postgres']
        require(not postgres.get('HostConfig',{}).get('PortBindings'), 'EXTERNAL_POSTGRES_PORT_REJECTED')
        observer=None
        for network in postgres['NetworkSettings']['Networks'].values():
            info=self.api.inspect_network(network['NetworkID'])
            for peer in set(info.get('Containers') or {}) - participants:
                require(observer in (None,peer), 'DUPLICATE_MONITORING_PEER')
                snapshot=self.api.inspect_container(peer)
                require(snapshot.get('Id')==peer, 'MONITOR_IDENTITY_CHANGED')
                observer=approved_monitor(snapshot,self.target,self.api.inspect_image(ALLOY_IMAGE))

    def assert_writers_stopped(self):
        found=self.containers();require(not found['nextcloud']['State']['Running'], 'WRITER_STILL_RUNNING')
        self.assert_database_participants(found)
        participants={i['Id'] for i in found.values()}
        html=next(m['Source'] for m in found['nextcloud']['Mounts'] if m['Destination']=='/var/www/html')
        for container in self.api.containers(all=True):
            if container['Id'] in participants:continue
            info=self.api.inspect_container(container['Id'])
            require(not any(m.get('Source') == html and m.get('RW') is True for m in info.get('Mounts',[])),
                    'EXTERNAL_VOLUME_WRITER_REJECTED')
        # Unknown cron/timer/CLI writers block; we never stop unrelated services.
        for directory in ('/var/spool/cron/crontabs','/etc/cron.d','/etc/cron.hourly','/etc/cron.daily',
                          '/etc/cron.weekly','/etc/cron.monthly','/etc/systemd/system','/usr/lib/systemd/system'):
            require(Path(directory).is_dir(), 'HOST_WRITER_AUDIT_UNAVAILABLE')
            for path in Path(directory).rglob('*'):
                if not path.is_file():continue
                # Unit aliases are followed only into the system unit directory,
                # never into deployment data or an arbitrary caller path.
                if path.is_symlink():
                    resolved=path.resolve()
                    require(str(resolved).startswith(('/etc/systemd/system/','/usr/lib/systemd/system/','/lib/systemd/system/'))
                            or str(resolved)=='/dev/null','HOST_WRITER_AUDIT_UNAVAILABLE')
                    if str(resolved)=='/dev/null':continue
                with path.open('rb') as stream:text=stream.read(1048577)
                require(len(text) <= 1048576, 'HOST_WRITER_AUDIT_LIMIT')
                active=b'\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(b'#'))
                require(not any(pattern in active for pattern in (b'cron.php',b'occ ',b'docker exec',self.target.deployment_root.encode())),
                        'UNCONTROLLED_HOST_JOB_REJECTED')
        with Path('/etc/crontab').open('rb') as stream:text=stream.read(1048577)
        require(len(text)<=1048576 and not any(pattern in text for pattern in
                (b'cron.php',b'occ ',b'docker exec',self.target.deployment_root.encode())), 'UNCONTROLLED_HOST_JOB_REJECTED')
        # /proc scan returns only a boolean, never argv/environ or FD paths.
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():continue
            try:
                status=(process/'status').read_text();uid=int(re.search(r'^Uid:\s+(\d+)',status,re.M).group(1))
                require(uid != 33, 'UNCONTROLLED_WWW_DATA_PROCESS')
                for descriptor in (process/'fd').iterdir():
                    if str(descriptor.resolve()).startswith(html+'/'):
                        flags=(process/'fdinfo'/descriptor.name).read_text()
                        mode=int(re.search(r'^flags:\s+([0-7]+)',flags,re.M).group(1),8)&3
                        require(mode == 0, 'EXTERNAL_VOLUME_WRITER_REJECTED')
            except FileNotFoundError:continue # exited process
            except PermissionError:raise CaptureFailure('HOST_WRITER_AUDIT_UNAVAILABLE') from None
        return None

    def assert_restored(self,original):
        found=self.containers()
        require(found['nextcloud']['State']['Running'] is original['writers']['nextcloud'], 'WRITER_STATE_NOT_RESTORED')
        require(all(found[name]['State']['Running'] and found[name]['State'].get('Health',{}).get('Status')=='healthy'
                    for name in ('postgres','redis')), 'DEPENDENCY_STATE_NOT_RESTORED')
        return None
