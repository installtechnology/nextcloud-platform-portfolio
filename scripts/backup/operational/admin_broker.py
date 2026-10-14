"""Exclusive root administrative broker; credentials never reach the backup job.

One authenticated UNIX peer holding the kernel flock may mutate a journal-bound
policy. No arbitrary policies, delete APIs, automatic unlock or credential export.
Run/credential provisioning is a separate authorized administrative operation.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
from .fencing import PolicyAuthority
from .policy_ownership import assert_policy_owner_clear,guard_statement
from .fence_policy import FenceScope,build_policy,digest,policy_document,require
from ..capture.storage import CaptureFailure
from ..json_input import parse_json
from ..limits import Limits
from ..sealing.common import json_bytes

SOCKET='/run/nextcloud-recovery/admin.sock'


def kernel_lock_owned(store,pid,locks=None):
    info=(store.root/'capture.lock').stat()
    text=Path('/proc/locks').read_text() if locks is None else locks
    for line in text.splitlines():
        parts=line.split()
        if len(parts)<8 or parts[1:4]!=['FLOCK','ADVISORY','WRITE']:continue
        try:
            major,minor,inode=parts[5].split(':')
            if int(parts[4])==pid and int(major,16)==os.major(info.st_dev) and int(minor,16)==os.minor(info.st_dev) and int(inode)==info.st_ino:
                return True
        except ValueError:continue
    return False


class AdministrativeBroker:
    def __init__(self,store,api,scope,expiration=None):
        self.store,self.api,self.scope,self.owner=store,api,scope,None
        self.expiration=expiration

    def invoke(self,pid,value):
        require(isinstance(value,dict) and set(value)=={'version','command','operation','document'}
                and type(value['version']) is int and value['version']==1,'ADMIN_PROTOCOL_INVALID')
        command,operation,document=value['command'],value['operation'],value['document']
        require(command in ('acquire','assert','release','identity','read_policy','read_configuration','pending_uploads','put_policy'),
                'ADMIN_COMMAND_REJECTED')
        if command in ('identity','read_policy','read_configuration','pending_uploads'):
            require(document is None,'ADMIN_PROTOCOL_INVALID')
            return getattr(self.api,command)(self.scope)
        self.store.operation(operation)
        require(kernel_lock_owned(self.store,pid),'ADMIN_KERNEL_LOCK_REQUIRED')
        if command=='acquire':
            if self.expiration is not None:
                from datetime import datetime,timezone
                require((self.expiration-datetime.now(timezone.utc)).total_seconds()>=3000,
                        'ADMIN_SESSION_LIFETIME_INSUFFICIENT')
            require(document is None and self.owner is None,'ADMIN_AUTHORITY_BUSY')
            record=assert_policy_owner_clear(self.store)
            require(record['scope']==self.scope.__dict__,'POLICY_OWNER_SCOPE_MISMATCH')
            current=policy_document(self.api.read_policy(self.scope))
            require([s for s in current['Statement'] if s.get('Sid')=='ExclusiveAdministrativeDataPolicyOwner']
                    ==[guard_statement(self.scope)],'POLICY_OWNER_GUARD_MISSING')
            self.owner=(pid,operation)
            return {'code':'ADMIN_AUTHORITY_HELD'}
        require(self.owner==(pid,operation),'ADMIN_AUTHORITY_OWNER_MISMATCH')
        self.api.identity(self.scope)
        if command=='assert':return {'code':'ADMIN_AUTHORITY_HELD'}
        record=self.store.read(operation+'/fence-journal.json')
        require(record.get('operation_id')==operation and record.get('scope')==self.scope.__dict__
                and record.get('before_hash')==digest(policy_document(record['before_raw']))
                and record['fenced_policy']==build_policy(record['before_raw'],self.scope,operation),
                'ADMIN_JOURNAL_BINDING_INVALID')
        if command=='release':
            require(document is None and record.get('phase')=='restored'
                    and self.store.read('fence-active.json')=={'operation_id':operation,'terminal':True},
                    'ADMIN_RECONCILIATION_REQUIRED')
            self.owner=None;return {'code':'ADMIN_AUTHORITY_RELEASED'}
        require(command=='put_policy' and isinstance(document,dict),'ADMIN_PROTOCOL_INVALID')
        expected=record['fenced_policy'] if record.get('phase')=='install_intent' else policy_document(record['before_raw']) if record.get('phase')=='restore_intent' else None
        previous=record['before_hash'] if record.get('phase')=='install_intent' else record['fenced_hash']
        require(document==expected and digest(policy_document(self.api.read_policy(self.scope)))==previous,
                'ADMIN_POLICY_WRITE_REJECTED')
        self.api.put_policy(self.scope,document)
        return {'code':'ADMIN_POLICY_WRITE_SUBMITTED'}

    def serve(self,connection):
        pid,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        require(uid==0,'ADMIN_ROOT_PEER_REQUIRED')
        stream=connection.makefile('rwb',buffering=0)
        try:
            while True:
                raw=stream.readline(32769)
                if not raw:break
                require(len(raw)<=32768 and raw.endswith(b'\n'),'ADMIN_PROTOCOL_LIMIT')
                try:
                    result=self.invoke(pid,parse_json(raw,Limits(json_bytes=32768)))
                    response={'ok':True,'result':result}
                except CaptureFailure as error:
                    response={'ok':False,'code':error.code}
                stream.write(json_bytes(response)+b'\n')
        finally:
            stream.close()
            # In particular, owner is not erased on peer death. New acquisition
            # stays blocked; S3 denies/journal require administrative reconciliation.


class BrokerAPI:
    def __init__(self,scope,inventory):
        require(type(scope) is FenceScope and os.name=='posix','ADMIN_BROKER_REQUIRED')
        self.scope,self.inventory,self.connection,self.stream,self.operation=scope,inventory,None,None,None
    def open(self):
        import stat
        path=Path(SOCKET)
        for part in (path,*path.parents):require(not part.is_symlink(),'ADMIN_SOCKET_UNSAFE')
        info=path.stat();require(stat.S_ISSOCK(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o600,'ADMIN_SOCKET_UNSAFE')
        self.connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.connection.settimeout(30);self.connection.connect(SOCKET)
        _,uid,_=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        require(uid==0,'ADMIN_SERVER_IDENTITY_REJECTED')
        self.stream=self.connection.makefile('rwb',buffering=0);return self
    def close(self):
        if self.stream:self.stream.close()
        if self.connection:self.connection.close()
    def request(self,command,document=None):
        require(self.stream is not None,'ADMIN_BROKER_UNAVAILABLE')
        try:
            self.stream.write(json_bytes({'version':1,'command':command,'operation':self.operation,'document':document})+b'\n')
            raw=self.stream.readline(32769);require(raw and len(raw)<=32768 and raw.endswith(b'\n'),'ADMIN_RESPONSE_INVALID')
            value=parse_json(raw,Limits(json_bytes=32768))
            require(value.get('ok') is True,'ADMIN_BROKER_OPERATION_REJECTED')
            return value['result']
        except CaptureFailure:raise
        except BaseException:raise CaptureFailure('ADMIN_OUTCOME_UNKNOWN') from None
    def check(self,scope):require(scope==self.scope,'FENCE_SCOPE_MISMATCH')
    def identity(self,scope):self.check(scope);return self.request('identity')
    def read_policy(self,scope):self.check(scope);return self.request('read_policy')
    def read_configuration(self,scope):self.check(scope);return self.request('read_configuration')
    def pending_uploads(self,scope):self.check(scope);return self.request('pending_uploads')
    def put_policy(self,scope,document):self.check(scope);return self.request('put_policy',document)
    def inventory_hash(self,scope):self.check(scope);return self.inventory.projection_hash(scope.bucket)


class BrokerAuthority(PolicyAuthority):
    def __init__(self,api):self.api=api
    from contextlib import contextmanager
    @contextmanager
    def hold(self,scope,operation):
        self.api.check(scope);require(self.api.operation is None,'ADMIN_AUTHORITY_BUSY')
        self.api.operation=operation;self.api.request('acquire')
        try:
            yield
            self.api.request('release')
        finally:self.api.operation=None
    def assert_held(self,scope,operation):
        require(self.api.operation==operation,'ADMIN_AUTHORITY_LOST');self.api.check(scope);self.api.request('assert')


def main():
    # Deliberately no activation from workflow/Environment. An administrator
    # provisions this root-only process and its ephemeral session over a separate
    # authorized channel. The backup SSH dispatcher cannot invoke this entrypoint.
    import sys
    from .runtime_config import load
    from .host_worker import credentials
    from .sdk import clients
    from .ssm import Target
    from .fence_admin_api import AdministrativePolicyAPI
    from ..capture.storage import Store
    try:
        config=load();require(os.geteuid()==0,'ADMIN_ROOT_REQUIRED')
        record=parse_json(sys.stdin.buffer.readline(32769),Limits(json_bytes=32768))
        target=Target(config['account'],config['region'],config['instance'],config['admin_role'].rsplit('/',1)[1])
        sdk=clients(credentials(record),target)
        scope=FenceScope(config['account'],config['data_bucket'],config['admin_role'],config['emergency_role'])
        api=AdministrativePolicyAPI(sdk['s3'],sdk['sts'],scope,target.region,None);api.identity(scope)
        from datetime import datetime
        store=Store(config['staging_root']);broker=AdministrativeBroker(store,api,scope,
            datetime.fromisoformat(record['expiration'].replace('Z','+00:00')))
        path=Path(SOCKET)
        require(path.parent.is_dir() and not path.parent.is_symlink()
                and path.parent.stat().st_uid==0 and path.parent.stat().st_mode&0o077==0
                and not path.exists() and not path.is_symlink(),'ADMIN_SOCKET_UNSAFE')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
            listener.bind(SOCKET);os.chmod(SOCKET,0o600);listener.listen(1)
            try:
                while True:
                    connection,_=listener.accept()
                    with connection:broker.serve(connection)
            finally:
                # Socket only, never journal/policy/lease cleanup.
                path.unlink(missing_ok=True)
    except BaseException:
        raise SystemExit('ADMIN_BROKER_REQUIRES_RECONCILIATION') from None


if __name__=='__main__':main()
