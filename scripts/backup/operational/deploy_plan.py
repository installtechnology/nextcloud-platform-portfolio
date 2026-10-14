"""Existing-host deploy never replaces the host holding the lease."""
from .fence_policy import require


def validate_runtime_target(document,instance):
    changes=document.get('resource_changes')
    require(isinstance(changes,list),'RUNTIME_PLAN_UNVERIFIED')
    hosts=[r for r in changes if r.get('address')=='aws_instance.host']
    require(len(hosts)==1,'GUARDED_INSTANCE_MISSING')
    host=hosts[0];change=host.get('change',{})
    require(host.get('mode')=='managed' and change.get('actions')==['no-op']
            and change.get('before',{}).get('id')==instance
            and change.get('after',{}).get('id')==instance,'GUARDED_INSTANCE_CHANGE_REJECTED')
    # Existing encrypted volumes must never be deleted/replaced by a deploy
    # intended to run under the current data-preservation lease.
    for resource in changes:
        change=resource.get('change',{})
        require(isinstance(change.get('actions'),list) and 'delete' not in change['actions'],
                'RUNTIME_DESTRUCTIVE_CHANGE_REJECTED')
