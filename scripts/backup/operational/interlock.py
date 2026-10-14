"""Fail-closed deployment gate reusing PR3's local lock/journal.

No collector, process termination, maintenance changes or automatic recovery.
Caller must hold the context for the entire mutation, not just preflight.
Host integration uses the real collector; injected tests are not operational evidence.
"""
from contextlib import contextmanager
from ..capture.engine import Capture
from ..capture.storage import CaptureFailure


@contextmanager
def deployment_guard(store, inspect_capture_processes):
    with store.lock():
        active_path = store.root / 'active.json'
        if active_path.exists() or active_path.is_symlink():
            active = store.read('active.json')
            if (not isinstance(active, dict) or set(active) != {'operation_id', 'terminal'}
                    or active['terminal'] is not True):
                raise CaptureFailure('RECOVERY_REQUIRED')
            operation = active['operation_id']
            store.operation(operation)
            journal = store.read(operation + '/journal.json')
            if (not Capture.valid_journal(journal, operation)
                    or journal.get('service_state_restored') is not True):
                raise CaptureFailure('RECOVERY_REQUIRED')
        try:
            processes = inspect_capture_processes()
        except Exception:
            raise CaptureFailure('PROCESS_INSPECTION_REQUIRED') from None
        if not isinstance(processes, list):
            raise CaptureFailure('PROCESS_INSPECTION_REQUIRED')
        if processes:
            raise CaptureFailure('ORPHANED_CAPTURE_PRESENT')
        from .fencing import assert_fence_clear
        assert_fence_clear(store)
        from .backup_lease import assert_backup_clear
        assert_backup_clear(store)
        # Empty list must come from a real complete collector at operational use.
        # Do not release the OS lock while the deployment mutates this host.
        yield
