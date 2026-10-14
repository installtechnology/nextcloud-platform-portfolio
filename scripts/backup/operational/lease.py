"""Durable deploy ownership on PR3's shared lock; no TTL-based recovery.

The authenticated host service keeps this context open before the first apply
until Ansible/Compose finishes. Its activation is a separate authorization.
Losing the channel leaves an unresolved journal even when flock is released.
"""
from contextlib import contextmanager
import re
from .interlock import deployment_guard
from ..capture.storage import CaptureFailure


def write_lease(store, value):
    try:
        store.write('deploy-lease.json', value)
    except Exception:
        raise CaptureFailure('DEPLOY_JOURNAL_WRITE_FAILED') from None


def assert_deployment_clear(store):
    path = store.root / 'deploy-lease.json'
    if path.exists() or path.is_symlink():
        try:
            value = store.read('deploy-lease.json')
        except Exception:
            raise CaptureFailure('DEPLOY_LEASE_RECOVERY_REQUIRED') from None
        if (not isinstance(value, dict) or set(value) != {'kind', 'version', 'owner', 'phase'}
                or value.get('kind') != 'deploy-lease' or type(value.get('version')) is not int
                or value['version'] != 1 or not isinstance(value.get('owner'), str)
                or not re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}', value['owner'])
                or value.get('phase') != 'released'):
            raise CaptureFailure('DEPLOY_LEASE_RECOVERY_REQUIRED')


@contextmanager
def deploy_lease(store, owner, inspect_capture_processes):
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}', owner):
        raise CaptureFailure('DEPLOY_OWNER_INVALID')
    with deployment_guard(store, inspect_capture_processes):
        assert_deployment_clear(store)
        record = {'kind': 'deploy-lease', 'version': 1, 'owner': owner, 'phase': 'held'}
        write_lease(store, record)
        try:
            yield
        except BaseException:
            record['phase'] = 'intervention_required'
            try:
                write_lease(store, record)
            except Exception:
                pass  # Previous held journal still blocks, even after lock release.
            raise CaptureFailure('DEPLOY_INTERRUPTED') from None
        else:
            record['phase'] = 'released'
            write_lease(store, record)
