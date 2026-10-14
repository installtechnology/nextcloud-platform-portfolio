"""Explicit destination executor. No CLI/workflow/credential discovery on import.

Runtime and SDK adapters must be created by the separately authorized channel.
There is no switch from mocks to production and no restoration-proof emitter.
"""
from scripts.backup.sealing.common import require
from scripts.restore.destination import Destination
from scripts.restore.host_runtime import HostRuntime
from scripts.restore.selection import SelectedBackup
from scripts.restore.sequence import execute_candidate


def execute_restore(selected,reader,source,objects,runtime,owner,resume=None):
    require(type(selected) is SelectedBackup and type(runtime) is HostRuntime,
            'RESTORE_REAL_EXECUTOR_REQUIRED')
    destination=Destination(selected,reader,source,objects,runtime)
    with runtime.control.hold(owner,resume):
        return execute_candidate(runtime.store,runtime.operation,selected,destination,
                                 runtime.host.config['source_instance'],runtime.host.config['state_bucket'],resume)
