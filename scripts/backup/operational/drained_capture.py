"""Live sequence: drain writers BEFORE S3 deny, restore S3 BEFORE reopening.

Separate operational controller; PR3 Capture and fixture guards are unchanged.
Failure never invokes automatic maintenance, HBA or policy recovery.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
from pathlib import Path
import shutil
import threading
import uuid
import copy
from .fencing import Coordinator,HeldWindow,assert_fence_clear,now
from .fence_policy import build_policy,digest,require
from .lease import assert_deployment_clear
from ..capture.engine import Capture,utc
from ..capture.storage import CaptureFailure,sync_directory
from ..sealing.common import json_bytes


class BoundedEvents(Capture):
    def event(self,journal,phase,**updates):
        prospective=copy.deepcopy(journal)
        prospective.update(phase=phase,updated_at=utc(),**updates)
        prospective['events'].append({'phase':phase,'at':prospective['updated_at']})
        # PR3 Store.read has a 1 MiB cap. Leave space for failure/recovery events;
        # never persist a journal that its recovery reader cannot consume.
        require(len(json_bytes(prospective))<=900*1024,'LIVE_JOURNAL_LIMIT')
        super().event(journal,phase,**updates)


class DrainedCoordinator(Coordinator):
    @contextmanager
    def window(self,operation):
        with self.store.lock(),self.authority.hold(self.scope,operation):
            assert_deployment_clear(self.store);assert_fence_clear(self.store)
            require(self.barrier.before_window(self.scope,operation) is None,'FENCE_CAPTURE_RECOVERY_REQUIRED')
            self.identity();self.configuration();raw,original=self.read()
            require(not (self.store.operation(operation)/'fence-journal.json').exists(),'FENCE_JOURNAL_EXISTS')
            fenced=build_policy(raw,self.scope,operation)
            record={'kind':'administrative-s3-fence','version':1,'operation_id':operation,
                'scope':self.scope.__dict__.copy(),'identity':self.identity(),'before_raw':raw,
                'before_hash':digest(original),'before_bytes_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'fenced_policy':fenced,'fenced_hash':digest(fenced),'phase':'install_intent',
                'capture_id':None,'observations':[],'created_at':now(),'backup_complete':False}
            self.write(operation,record);self.store.write('fence-active.json',{'operation_id':operation,'terminal':False})
            window=HeldWindow(self,operation,threading.get_ident())
            try:
                yield window
                require(self.load(operation)['phase']=='restored','FENCE_EXPLICIT_RELEASE_REQUIRED')
            except BaseException:
                # Leave durable policy/HBA/maintenance state for explicit recovery.
                raise
            finally:window.active=False;window.released=True

    def bind(self,window,capture_id):
        require(type(window) is HeldWindow and window.coordinator is self,'FENCE_WINDOW_REQUIRED')
        window.assert_held();record=self.load(window.operation)
        require(record['capture_id'] in (None,capture_id),'FENCE_CAPTURE_BINDING_MISMATCH')
        require(record['phase'] in ('install_intent','observed','quiescent'),'FENCE_NOT_OBSERVED')
        self.store.operation(capture_id);record['capture_id']=capture_id;self.write(window.operation,record)

    def activate_after_drain(self,window,backend,original):
        window.assert_held();record=self.load(window.operation)
        require(record['phase']=='install_intent' and record['capture_id']==backend.context['operation_id'],
                'FENCE_CAPTURE_BINDING_MISMATCH')
        backend.assert_quiescent(original)
        self.configuration();require(self.call('pending_uploads')==[],'FENCE_MULTIPART_PENDING')
        self.compare(record['before_hash']);self.call('put_policy',record['fenced_policy']);self.compare(record['fenced_hash'])
        record.update(phase='observed',applied_observed_at=now());self.write(window.operation,record)
        backend.assert_quiescent(original)

    def restore_before_reopening(self,window,backend,original):
        require(self.load(window.operation)['phase']=='quiescent','FENCE_RECONCILIATION_REQUIRED')
        backend.assert_quiescent(original)
        self._restore(window.operation,self.load(window.operation),window)
        # Only the administrative lock remains held. There is no claim of an S3
        # fence after this transition; inventory/capture checkpoints are forbidden
        # by their phase checks. Keep the host ownership assertion for restoration.
        window.released=False


class LiveCapture:
    def __init__(self,store,backend,hooks,coordinator,window):
        require(type(coordinator) is DrainedCoordinator,'DRAINED_COORDINATOR_REQUIRED')
        self.store,self.backend,self.hooks,self.coordinator,self.window=store,backend,hooks,coordinator,window
        self.events=BoundedEvents(store,backend,hooks)

    def start(self):
        if (self.store.root/'active.json').exists():
            active=self.store.read('active.json');self.store.operation(active.get('operation_id'))
            prior=self.store.read(active['operation_id']+'/journal.json')
            require(active.get('terminal') is True and Capture.valid_journal(prior,active['operation_id'])
                    and prior.get('service_state_restored') is True,'RECOVERY_REQUIRED')
        original=self.backend.preflight()
        for source in original['capture_sources']:
            path=Path(source).resolve()
            require(not self.store.root.is_relative_to(path) and not path.is_relative_to(self.store.root),'STAGING_SOURCE_OVERLAP')
        require(shutil.disk_usage(self.store.root).free>=original['estimated_capture_bytes']+1048576,'INSUFFICIENT_STAGING_DISK')
        bid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+str(uuid.uuid4())
        directory=self.store.operation(bid);directory.mkdir(mode=0o700);sync_directory(self.store.root);self.store.reserve(directory)
        journal={'internal_version':1,'kind':'local-capture-journal','operation_id':bid,'consistency_id':'capture-'+str(uuid.uuid4()),
            'original':original,'events':[],'stop_intents':[],'maintenance_fingerprint':None,'artifacts':{},
            'status':'incomplete','service_state_restored':False,
            'scope':{'local_only':True,'s3_consistency':'live_pair_required','encryption':'not_implemented',
                     'transfer':'not_implemented','functional_restore':'not_verified'}}
        self.backend.context={'operation_id':bid,'consistency_id':journal['consistency_id']}
        self.events.event(journal,'prepared');self.store.write('active.json',{'operation_id':bid,'terminal':False})
        try:
            require(self.backend.maintenance()==original['maintenance'] and self.backend.fingerprint()==original['config_fingerprint'],
                    'PREEXISTING_STATE_CHANGED')
            self.events.event(journal,'maintenance_intent')
            if not original['maintenance']:self.backend.set_maintenance(True)
            self.events.event(journal,'maintenance_owned',maintenance_fingerprint=self.backend.fingerprint())
            self.events.event(journal,'stop_intent',stop_intents=['nextcloud']);self.backend.stop_writer('nextcloud')
            self.coordinator.activate_after_drain(self.window,self.backend,original)
            self.backend.assert_quiescent(original);self.events.event(journal,'quiesced',quiesced_at=utc())
            self.events.event(journal,'inventory_before_intent')
            self.events.event(journal,'inventory_before_observed',inventory_observation=self.hooks.before(journal))
            self.events.event(journal,'dump_intent')
            journal['artifacts']['database']=self.store.artifact(directory,'database.dump',self.backend.dump,self.backend.limit)
            self.backend.check_dump(directory/'database.dump');self.events.event(journal,'dump_catalog_checked')
            for role,name in (('configuration','configuration.tar'),('local_files','local_files.tar'),('recovery_parameters','recovery_parameters.json')):
                self.events.event(journal,role+'_intent')
                journal['artifacts'][role]=self.store.artifact(directory,name,lambda out,role=role:self.backend.capture(role,out),self.backend.limit)
                self.events.event(journal,role+'_captured')
            self.backend.assert_quiescent(original);self.events.event(journal,'inventory_after_intent')
            self.events.event(journal,'inventory_after_observed',inventory_observation=self.hooks.after(journal))
            self.backend.assert_quiescent(original);self.events.event(journal,'local_capture_finished',capture_finished_at=utc())
            # No automatic release on exceptions. Here both inventories, dump
            # catalog and captures succeeded and all writers remain fenced.
            self.coordinator.restore_before_reopening(self.window,self.backend,original)
            self.events.restore(journal)
            self.events.event(journal,'candidate_ready',status='local_candidate_only')
            self.store.write('active.json',{'operation_id':bid,'terminal':True})
            return {'operation_id':bid,'backup_complete':False}
        except BaseException as error:
            from ..capture.control import cleanup_signals
            with cleanup_signals():
                code=error.code if isinstance(error,CaptureFailure) else 'LIVE_CAPTURE_INTERRUPTED'
                self.store.release_reserve(directory)
                self.events.event(journal,'intervention_required',status='incomplete',error_code=code)
            raise CaptureFailure('LIVE_CAPTURE_RECONCILIATION_REQUIRED') from None
