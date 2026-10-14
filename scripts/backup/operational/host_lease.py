"""Bounded SSH-stdio lease server. Library only; no listener/install/activation.

EOF, malformed requests and cancellation unwind to intervention_required, not
released. Only explicit release allows a clean end. No arbitrary commands.
"""
from .lease import deploy_lease
from .policy_ownership import assert_policy_owner_clear
from .fence_policy import require
from ..json_input import parse_json
from ..limits import Limits
from ..sealing.common import json_bytes


def serve_lease(store, identity_probe, inspect_processes, incoming, outgoing, authorize=None):
    def read():
        raw=incoming.readline(16385)
        require(0 < len(raw) <= 16384 and raw.endswith(b'\n'), 'LEASE_PROTOCOL_INTERRUPTED')
        value=parse_json(raw,Limits(json_bytes=16384))
        fields={'version','command','owner','nonce','account','region','instance'}
        if authorize is not None:
            fields.add('scope')
            if value.get('command')=='acquire':fields.add('credentials')
        require(isinstance(value,dict) and set(value)==fields
                and type(value['version']) is int and value['version']==(2 if authorize else 1), 'LEASE_PROTOCOL_INVALID')
        import re
        require(isinstance(value['nonce'],str) and re.fullmatch(r'[a-f0-9]{64}',value['nonce']), 'LEASE_PROTOCOL_INVALID')
        return value
    def reply(request,phase):
        outgoing.write(json_bytes({**request,'phase':phase})+b'\n');outgoing.flush()
    first=read()
    require(first['command']=='acquire','LEASE_PROTOCOL_INVALID')
    if authorize is not None:
        authorize(first.pop('credentials'),first)
    identity=identity_probe()
    require(identity=={k:first[k] for k in ('account','region','instance')},'REMOTE_DEPLOY_TARGET_MISMATCH')
    # Absence of owner registration is not permission to bootstrap unguarded.
    assert_policy_owner_clear(store)
    release=None
    with deploy_lease(store,first['owner'],inspect_processes):
        reply(first,'held')
        while True:
            request=read()
            require({k:v for k,v in request.items() if k!='command'}=={k:v for k,v in first.items() if k!='command'},'LEASE_PROTOCOL_OWNER_CHANGED')
            require(identity_probe()==identity,'REMOTE_DEPLOY_TARGET_MISMATCH')
            assert_policy_owner_clear(store)
            require(request['command'] in ('assert','release'),'LEASE_PROTOCOL_INVALID')
            if request['command']=='release':release=request;break
            reply(request,'held')
    reply(release,'released') # Durable release precedes acknowledgement.
