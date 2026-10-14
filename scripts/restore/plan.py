"""Describe isolated restore requirements. This module cannot execute a restore."""
from dataclasses import dataclass
from scripts.backup.sealing.common import require


@dataclass(frozen=True)
class Destination:
    account: str
    region: str
    instance: str
    data_bucket: str
    staging_root: str


def prepare(selected,destination,source_instance,state_bucket):
    m=selected.bundle['manifest'];source=m['source']
    require(destination.account==source['account_id'] and destination.region==source['region'],
            'RESTORE_DESTINATION_SCOPE_MISMATCH')
    require(destination.instance!=source_instance and destination.data_bucket not in
            {source['data_bucket'],state_bucket,*[a['bucket'] for a in m['artifacts'].values()]},
            'RESTORE_ORIGIN_TARGET_REJECTED')
    require(destination.instance.startswith('i-') and destination.data_bucket
            and destination.staging_root.startswith('/') and '..' not in destination.staging_root.split('/'),
            'RESTORE_DESTINATION_INVALID')
    return {'backup_id':selected.backup_id,'completion_version_id':selected.completion_version,
            'manifest_sha256':selected.manifest_sha256,'restore_proven':False,'destroy_authorized':False,
            'steps':['verify_destination_identity_empty_volumes_database_and_empty_or_exact_verified_bucket',
                     'download_verified_exact_artifact_versions_on_destination_only',
                     'restore_roles_credentials_and_postgresql17_custom_dump',
                     'restore_nextcloud_config_identity_and_local_files_without_install',
                     'copy_exact_source_versions_to_empty_bucket_or_verify_retained_bytes_without_writes',
                     'approve_explicit_storage_remap_and_reconcile_marker',
                     'start_services_and_validate_login_file_hashes_and_restart',
                     'publish_separate_versioned_restore_evidence'],
            'destination_data_bucket':destination.data_bucket,
            'objects':[{'key':o['key'],'source_version_id':o['version_id'],'size_bytes':o['size_bytes']}
                       for o in selected.bundle['inventory']['objects']],
            'must_remain_absent':list(selected.bundle['inventory']['absent_current_keys']),
            'operational_execution_available':False}
