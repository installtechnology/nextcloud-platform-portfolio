"""Integrated destination sequence, separate from backup/deploy dispatchers.

No CLI, automatic credentials or functional-proof emitter. Actual execution must
be enclosed in RestoreControl.hold by the future authorized restore channel.
"""
import json
import shutil
from pathlib import Path
from scripts.backup.sealing.common import require
from scripts.backup.safe_files import SafeRoot
from scripts.restore.control import RestoreControl
from scripts.restore.host_runtime import HostRuntime
from scripts.restore.downloads import download_selected
from scripts.restore.private_files import stage_private,verified_file
from scripts.restore.objects import copy_selected,retained_objects
from scripts.restore.database import restore_database
from scripts.restore.postgres_adapter import PostgreSQLDestination
from scripts.restore.s3_reader import BackupReader,SourceObjects,DestinationObjects
from scripts.restore.parameters import RUNTIME,load_parameters


class Destination:
    def __init__(self,selected,reader,source,objects,runtime):
        require(type(runtime) is HostRuntime and type(runtime.control) is RestoreControl
                and type(reader) is BackupReader and type(source) is SourceObjects
                and type(objects) is DestinationObjects,'RESTORE_REAL_DESTINATION_REQUIRED')
        c=runtime.host.config;m=selected.bundle['manifest']
        require(reader.bucket==c['backups_bucket'] and source.bucket==c['source_data_bucket']
                and objects.bucket==c['data_bucket'] and m['source']['data_bucket']==source.bucket
                and m['source']['account_id']==c['account'] and m['source']['region']==c['region']
                and m['source']['project']==c['project'],'RESTORE_DESTINATION_SCOPE_MISMATCH')
        require(m['compatibility']['nextcloud_image']==c['images']['nextcloud']
                and m['compatibility']['postgres_image']==c['images']['postgres'],'RESTORE_DESTINATION_IMAGE_MISMATCH')
        self.selected,self.reader,self.source,self.objects,self.runtime=selected,reader,source,objects,runtime
        self.parameters=None;self.mappings=None
        self.objects_observed=False;self.object_selection=None

    def inspect_target(self):
        self.runtime.guard();c=self.runtime.host.config;actual=self.runtime.host.identity()
        return dict(actual,data_bucket=c['data_bucket'])

    def assert_identity_and_control(self,identity):
        require(self.inspect_target()==identity,'RESTORE_TARGET_CONTROL_LOST')

    def assert_empty_and_exclusive(self):
        self.runtime.assert_empty()
        self.objects.assert_protections()
        self.assert_selected_objects()
        artifacts=self.selected.bundle['manifest']['artifacts']
        minimum=sum(a['size_bytes'] for a in artifacts.values())+artifacts['local_files']['size_bytes']
        minimum+=max(16*artifacts['database']['size_bytes'],512*1024**2)+512*1024**2
        require(shutil.disk_usage(self.runtime.store.root).free>=minimum,'RESTORE_DISK_CAPACITY_INSUFFICIENT')

    def assert_selected_objects(self):
        c=self.runtime.host.config
        observed=retained_objects(self.source,self.objects,self.selected.bundle['inventory'],
                                  c['backups_bucket'],c['state_bucket'])
        if self.objects_observed:
            require(observed==self.object_selection,'RESTORE_OBJECT_DESTINATION_CHANGED')
        else:
            self.objects_observed=True;self.object_selection=observed

    def download_and_verify(self):
        self.runtime.guard()
        self.downloads=download_selected(self.reader,self.selected,self.runtime.store,self.runtime.operation)

    def restore_private_files(self):
        self.runtime.guard();reviewed={}
        with SafeRoot(self.runtime.reviewed_root/'docker') as safe:
            for name in RUNTIME:
                with safe.open(name) as stream:reviewed[name]=stream.read(1024**2+1)
        self.parameters,self.file_result=stage_private(self.runtime.directory,self.selected.bundle['manifest'],reviewed)
        self.runtime.install_runtime(self.parameters,self.selected.bundle['manifest'])

    def revalidate_pre_database(self,resume):
        self.runtime.guard();m=self.selected.bundle['manifest'];directory=self.runtime.directory
        # Re-read every byte against the already selected exact S3 versions. No
        # re-download, overwrite, extraction or installation during revalidation.
        for artifact in m['artifacts'].values():
            verified_file(directory,artifact['key'].rsplit('/',1)[1],artifact,lambda stream:None)
        a=m['artifacts']['recovery_parameters']
        self.parameters=verified_file(directory,a['key'].rsplit('/',1)[1],a,
            lambda stream:load_parameters(stream.read(8*1024**2+1),m))
        reviewed={}
        with SafeRoot(self.runtime.reviewed_root/'docker') as safe:
            for name in RUNTIME:
                with safe.open(name) as stream:reviewed[name]=stream.read(1024**2+1)
        require(self.parameters.runtime_code==reviewed,'RESTORE_RUNTIME_REVIEW_REQUIRED')
        resume.assert_trees(directory)
        self.objects.assert_protections()
        self.assert_selected_objects()
        self.runtime.revalidate_pre_database(self.parameters,m)

    def restore_roles_and_database(self):
        self.runtime.guard();container=self.runtime.start_database()
        adapter=PostgreSQLDestination(self.runtime.host,self.runtime.api,self.runtime.supervisor,
                                      self.runtime.operation,container)
        a=self.selected.bundle['manifest']['artifacts']['database']
        from scripts.restore.private_files import verified_file
        self.database_result=verified_file(self.runtime.directory,a['key'].rsplit('/',1)[1],a,
                                           lambda stream:restore_database(adapter,self.parameters,stream))

    def copy_exact_object_versions(self):
        self.runtime.guard();c=self.runtime.host.config
        self.assert_selected_objects()
        self.mappings=copy_selected(self.source,self.objects,self.selected.bundle['inventory'],
                                    c['backups_bucket'],c['state_bucket'])
        require(len(json.dumps(self.mappings).encode())<=900*1024,'RESTORE_VERSION_MAP_LIMIT')
        self.runtime.store.write(self.runtime.operation+'/object-version-map.json',self.mappings)

    def remap_storage_explicitly(self):
        self.runtime.guard();require(self.mappings is not None,'RESTORE_OBJECTS_NOT_VERIFIED')
        self.runtime.remap(self.selected.bundle['manifest'])
        self.runtime.publish_html(self.selected.bundle['manifest']['artifacts']['local_files'])

    def start_without_install(self):
        self.runtime.compose('up','-d','--no-deps','--pull','never','--no-build','nextcloud')

    def check_application_and_restart(self,read_only=False):
        self.runtime.guard()
        manifest=self.selected.bundle['manifest'];maintenance=manifest['consistency']['maintenance_initially_enabled']
        self.runtime.wait_healthy(('postgres','redis','nextcloud'),nextcloud_maintenance=maintenance)
        self.runtime.application_status(manifest)
        if not read_only:
            self.runtime.compose('restart','nextcloud')
            self.runtime.wait_healthy(('postgres','redis','nextcloud'),nextcloud_maintenance=maintenance)
            self.runtime.application_status(manifest)
        # Health/status alone never attest login/download/user data recovery.
        return {'services_healthy':True,'restore_proven':False,'destroy_authorized':False}
