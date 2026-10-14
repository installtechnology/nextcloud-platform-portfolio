"""Cross-operation orphan collector. Unknown journals/execs block, never kill."""
from pathlib import Path
import re
from .fence_policy import require
from .supervision import Supervisor
from .postgres import Limited
from ..json_input import parse_json


class HostSupervisor:
    def __init__(self,store,docker,boot_id,postgres):
        self.store,self.docker,self.boot_id,self.pg=store,docker,boot_id,postgres

    def inspect(self):
        blockers=[]
        for directory in self.store.root.iterdir():
            if not directory.is_dir():continue
            require(not directory.is_symlink(), 'HOST_JOURNAL_PATH_UNSAFE')
            self.store.operation(directory.name)
            if (directory/'exec-journal.json').exists():
                supervisor=Supervisor(self.store,self.docker,self.boot_id)
                record=supervisor.load(directory.name)
                require(re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',record['boot_id']), 'EXEC_JOURNAL_INVALID')
                # Docker may discard completed exec IDs when a container is
                # replaced or the daemon reboots. Only durably finished records
                # can be historical; unresolved intents never get this treatment.
                completed=all(isinstance(e,dict) and set(e)=={'container_id','exec_id','phase','command_sha256','exit_code'}
                    and e.get('phase')=='finished'
                    and type(e.get('exit_code')) is int and isinstance(e.get('exec_id'),str)
                    and re.fullmatch(r'[a-f0-9]{64}',e['exec_id'])
                    and re.fullmatch(r'[a-f0-9]{64}',e.get('container_id',''))
                    and re.fullmatch(r'[a-f0-9]{64}',e.get('command_sha256','')) for e in record['execs'])
                if not completed:
                    blockers.extend(supervisor.blockers(directory.name))
            for filename,terminal in (('state-helper-journal.json','removed'),('postgres-fence.json','restored')):
                if not (directory/filename).exists():continue
                record=self.store.read(directory.name+'/'+filename)
                require(record.get('operation_id')==directory.name, 'HOST_JOURNAL_BINDING_INVALID')
                if filename.startswith('state-helper'):
                    require(isinstance(record.get('helpers'),list), 'HOST_JOURNAL_INVALID')
                    if any(r.get('phase')!=terminal for r in record['helpers']):blockers.append('STATE_HELPER_RECOVERY_REQUIRED')
                elif record.get('phase')!=terminal:blockers.append('POSTGRES_FENCE_RECOVERY_REQUIRED')
        # All app-container execs must be accounted for. Host control queries use
        # a registry operation too; healthchecks are never treated as captures.
        for container in self.pg.containers:
            if self.docker.running_execs(container):blockers.append('UNTRACKED_DOCKER_EXEC')
        output=Limited()
        # These are PostgreSQL backend PIDs for named capture clients across the
        # server, not host/container IDs. Exclude only this inspection's session.
        query="SELECT json_build_object('pids',coalesce(json_agg(pid ORDER BY pid),'[]'::json)) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND application_name LIKE 'ncbackup-%'"
        self.pg.command('psql',['-X','-A','-t','-v','ON_ERROR_STOP=1','--dbname',self.pg.database,'-c',query],output,30)
        document=parse_json(output.getvalue())
        require(set(document)=={'pids'} and isinstance(document['pids'],list), 'HOST_POSTGRES_INSPECTION_REQUIRED')
        values=document['pids']
        require(all(type(pid) is int and 0 < pid <= 2147483647 for pid in values)
                and len(values)==len(set(values)), 'HOST_POSTGRES_INSPECTION_REQUIRED')
        if values:blockers.append('ORPHANED_POSTGRES_CAPTURE_SESSION')
        return blockers
