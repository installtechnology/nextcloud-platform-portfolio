"""Persistent operation exclusion also covering encryption/upload after capture."""
from contextlib import contextmanager
import re
from .fence_policy import require
from .lease import assert_deployment_clear
from .fencing import assert_fence_clear
from ..capture.storage import CaptureFailure


def assert_backup_clear(store):
    path=store.root/'backup-lease.json'
    if not path.exists() and not path.is_symlink():return
    record=store.read('backup-lease.json')
    require(isinstance(record,dict) and set(record)=={'kind','version','owner','operation_id','phase'}
            and record['kind']=='backup-lease' and type(record['version']) is int and record['version']==1
            and isinstance(record['owner'],str) and re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}',record['owner'])
            and record['phase']=='released', 'BACKUP_LEASE_RECOVERY_REQUIRED')
    store.operation(record['operation_id'])


@contextmanager
def backup_lease(store,operation,owner):
    require(isinstance(owner,str) and re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}',owner),'BACKUP_OWNER_INVALID')
    store.operation(operation)
    with store.lock():
        assert_backup_clear(store);assert_deployment_clear(store);assert_fence_clear(store)
        record={'kind':'backup-lease','version':1,'owner':owner,'operation_id':operation,'phase':'held'}
        store.write('backup-lease.json',record)
    try:
        yield
    except BaseException:
        with store.lock():
            require(store.read('backup-lease.json')==record,'BACKUP_LEASE_OWNER_CHANGED')
            record['phase']='intervention_required';store.write('backup-lease.json',record)
        raise
    else:
        with store.lock():
            require(store.read('backup-lease.json')==record,'BACKUP_LEASE_OWNER_CHANGED')
            record['phase']='released';store.write('backup-lease.json',record)
