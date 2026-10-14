"""Live-only backend and writer barrier. No synthetic constructor or flags."""
import hashlib
import json
import time
from .capture_backend import OperationalCaptureBackend
from .database_fence import DatabaseFence
from .fencing import WriterBarrier
from .fence_policy import require
from ..capture.storage import CaptureFailure


class LiveCaptureBackend(OperationalCaptureBackend):
    def container_ready(self,service,info,state):
        if service!='nextcloud' or state['maintenance'] is False:
            return super().container_ready(service,info,state)
        # Existing maintenance intentionally makes the project healthcheck fail.
        # Read HTTP status independently; do not forge a healthy Docker snapshot.
        status=self.probe.nextcloud_status()
        return status=={'installed':True,'maintenance':True,'needsDbUpgrade':False}

    def preflight(self):
        self.original=super().preflight()
        return self.original

    @OperationalCaptureBackend.context.setter
    def context(self,value):
        OperationalCaptureBackend.context.fset(self,value)
        mounts=[m for m in self.containers['postgres']['Mounts'] if m.get('Destination')=='/var/lib/postgresql/data']
        require(len(mounts)==1 and mounts[0].get('Type')=='volume', 'POSTGRES_VOLUME_INVALID')
        self.database_fence=DatabaseFence(self.store,self.context['operation_id'],self.pg,mounts[0]['Source'])

    def stop_writer(self,service):
        super().stop_writer(service)
        # Stop/drain Apache before fencing authentication; do not kill DB peers.
        self.probe.assert_writers_stopped()
        self.database_fence.install()

    def assert_quiescent(self,original):
        self.database_fence.assert_fenced()
        super().assert_quiescent(original)

    def start_writer(self,service):
        journal=self.store.read(self.context['operation_id']+'/journal.json')
        require('error_code' not in journal,'LIVE_CAPTURE_RECONCILIATION_REQUIRED')
        path=self.store.root/self.database_fence.relative
        if path.exists():
            record=self.store.read(self.database_fence.relative)
            if record['phase'] != 'restored':self.database_fence.restore()
        # Startup remains in maintenance while dependency health recovers.
        for _ in range(30):
            state=self.probe.containers()['postgres']['State']
            if state.get('Running') and state.get('Health',{}).get('Status')=='healthy':break
            time.sleep(1)
        else:raise CaptureFailure('POSTGRES_RESTORED_HEALTH_TIMEOUT')
        super().start_writer(service)


class LiveWriterBarrier(WriterBarrier):
    def __init__(self,store,inspect_idle):
        self.store,self.inspect_idle,self.backend=store,inspect_idle,None
    def attach(self,backend):
        require(type(backend) is LiveCaptureBackend and self.backend is None, 'LIVE_BACKEND_REQUIRED')
        self.backend=backend
    def before_window(self,scope,operation):
        require(self.inspect_idle() == [], 'ORPHANED_CAPTURE_PRESENT')
        if (self.store.root/'active.json').exists():
            require(self.store.read('active.json').get('terminal') is True, 'CAPTURE_RECOVERY_REQUIRED')
        return None
    def verify(self,operation,capture_id,phase):
        require(self.backend is not None and self.backend.context['operation_id']==capture_id
                and self.backend.window.operation==operation and phase in ('before','after'), 'LIVE_WRITER_BINDING_REQUIRED')
        self.backend.assert_quiescent(self.backend.original)
        record=self.store.read(capture_id+'/postgres-fence.json')
        evidence={'capture_id':capture_id,'fence_id':operation,'phase':phase,
                  'database_fence_sha256':record['fenced_sha256'],
                  'boot_id':self.backend.supervisor.boot_id,
                  'containers':self.backend.original['containers'],
                  'helper_journal':self.store.read(self.backend.probe.helper.relative)}
        return hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest()
