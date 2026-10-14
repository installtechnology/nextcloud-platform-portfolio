"""Durable pg_hba fencing. No role/password changes or termination of peers.

New TCP clients are rejected (including localhost/replication). Only local peer
postgres remains for supervised tools. Existing peers must drain, never be killed.
An ambiguous write/reload is left fenced until explicit administrative recovery.
"""
import base64
import hashlib
import os
from pathlib import Path
import stat
import time
from .fence_policy import require
from .postgres import Limited
from ..capture.storage import CaptureFailure, sync_directory
from ..safe_files import SafeRoot, signature

RULES = b'local all postgres peer\nlocal all all reject\nhost all all 0.0.0.0/0 reject\nhost all all ::0/0 reject\nhost replication all 0.0.0.0/0 reject\nhost replication all ::0/0 reject\n'


class AuthProbe(Limited):
    def __init__(self):
        super().__init__(); self.error = bytearray()
    def write_stderr(self, data):
        require(len(self.error)+len(data) <= 4096, 'POSTGRES_AUTH_PROBE_AMBIGUOUS')
        self.error.extend(data)
    def rejected(self):
        # Never return or log the raw diagnostic (host/user/database are private).
        return b'pg_hba.conf rejects connection' in self.error


class DatabaseFence:
    def __init__(self, store, operation, postgres, volume, sleep=time.sleep):
        self.store, self.operation, self.pg = store, operation, postgres
        self.volume, self.sleep = Path(volume), sleep
        self.relative = operation+'/postgres-fence.json'
        self.path = None

    def sql(self, query):
        output = Limited()
        self.pg.command('psql', ['-X','-A','-t','-v','ON_ERROR_STOP=1','--dbname',self.pg.database,'-c',query],output,30)
        return output.getvalue().strip()

    def locate(self):
        value = self.sql('SHOW hba_file').decode('utf-8')
        require(value == '/var/lib/postgresql/data/pg_hba.conf', 'POSTGRES_HBA_PATH_REJECTED')
        self.path = self.volume/'pg_hba.conf'
        with SafeRoot(self.volume) as safe, safe.open('pg_hba.conf') as stream:
            data = stream.read(65537)
            require(0 < len(data) <= 65536, 'POSTGRES_HBA_LIMIT')
            info = os.fstat(stream.fileno())
            require(stat.S_IMODE(info.st_mode) & 0o022 == 0, 'POSTGRES_HBA_UNSAFE')
            return data, info

    def read(self):
        with SafeRoot(self.volume) as safe, safe.open('pg_hba.conf') as stream:
            data = stream.read(65537)
            require(len(data) <= 65536 and signature(stream)[2] == len(data), 'POSTGRES_HBA_CHANGED')
            return data

    def write(self, data, previous, metadata):
        require(self.read() == previous, 'POSTGRES_HBA_CONCURRENT_CHANGE')
        path = self.volume/'pg_hba.conf.ncbackup-pending'
        fd = os.open(path, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,'wb') as stream:
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        require(self.read() == previous, 'POSTGRES_HBA_CONCURRENT_CHANGE')
        os.replace(path, self.path); sync_directory(self.volume)

    def reload(self):
        require(self.sql('SELECT pg_reload_conf()') == b't', 'POSTGRES_RELOAD_UNCONFIRMED')
        # Reload is asynchronous. Parsed rules alone do not prove active auth;
        # TCP connection probes below must also be denied.
        require(self.sql('SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') == b'0',
                'POSTGRES_HBA_INVALID')

    def assert_fenced(self):
        record = self.store.read(self.relative)
        require(record.get('operation_id') == self.operation and record.get('phase') == 'held'
                and hashlib.sha256(self.read()).hexdigest() == record['fenced_sha256'], 'POSTGRES_FENCE_LOST')
        require(self.pg.sessions() == [], 'DATABASE_WRITER_NOT_FENCED')
        output=AuthProbe()
        try:
            # --no-password prevents prompts and environment-supplied passwords.
            # Connection rejection is expected, but its reason is checked with
            # the parsed effective leading rules and recorded current file.
            self.pg.supervisor.execute(self.operation,self.pg.container,
                ['psql','--host=127.0.0.1','--username=postgres','--no-password','-X','-c','SELECT 1'],
                'postgres',output,10)
        except CaptureFailure as error:
            require(error.code == 'EXEC_FAILED' and output.rejected(), 'POSTGRES_AUTH_PROBE_AMBIGUOUS')
        else: raise CaptureFailure('POSTGRES_TCP_FENCE_NOT_EFFECTIVE')

    def install(self):
        require(not (self.store.root/self.relative).exists(), 'POSTGRES_FENCE_ALREADY_EXISTS')
        original, metadata = self.locate()
        require(not original.startswith(RULES), 'POSTGRES_FOREIGN_FENCE')
        fenced = RULES+original
        record={'kind':'postgres-auth-fence','operation_id':self.operation,'phase':'install_intent',
                'before_base64':base64.b64encode(original).decode(),
                'before_sha256':hashlib.sha256(original).hexdigest(),
                'fenced_sha256':hashlib.sha256(fenced).hexdigest()}
        self.store.write(self.relative,record)
        self.write(fenced,original,metadata);self.reload()
        for _ in range(60):
            if not self.pg.sessions():break
            self.sleep(1)
        else:raise CaptureFailure('POSTGRES_PEERS_NOT_DRAINED')
        record['phase']='held';self.store.write(self.relative,record)
        self.assert_fenced()

    def restore(self):
        record=self.store.read(self.relative)
        require(record.get('phase') == 'held' and record.get('operation_id') == self.operation,
                'POSTGRES_FENCE_RECOVERY_REQUIRED')
        original=base64.b64decode(record['before_base64'],validate=True)
        require(hashlib.sha256(original).hexdigest() == record['before_sha256'], 'POSTGRES_FENCE_JOURNAL_INVALID')
        current,metadata=self.locate()
        require(current == RULES+original and hashlib.sha256(current).hexdigest() == record['fenced_sha256'],
                'POSTGRES_HBA_CONCURRENT_CHANGE')
        require(self.pg.sessions() == [], 'POSTGRES_PEERS_NOT_DRAINED')
        record['phase']='restore_intent';self.store.write(self.relative,record)
        self.write(original,current,metadata);self.reload()
        require(self.read() == original, 'POSTGRES_RESTORE_UNCONFIRMED')
        record['phase']='restored';self.store.write(self.relative,record)
