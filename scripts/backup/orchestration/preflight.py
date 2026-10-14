"""Pure synthetic observation checks. No collector, network, SSH or operational gate.

This is a testable checklist, not authenticated evidence. The closed input shape
does not accept credentials, dumps, configuration contents or arbitrary details.
"""
from datetime import datetime, timezone

from .gates import blocked_result, literal, require

CONFIG = {'account', 'region', 'project', 'environment', 'instance_id', 'instance_profile',
          'data_bucket', 'backups_bucket', 'installation_identity_sha256', 'repository_commit'}
OBSERVATION = {'synthetic', 'observed_at', 'account', 'region', 'role_name', 'role_active',
               'permissions_checked', 'instance_count', 'instance_id', 'instance_profile', 'tags',
               'instance_running', 'status_checks_ok', 'data_bucket', 'backups_bucket',
               'storage_versioned', 'storage_aes256', 'storage_tls', 'public_access_blocked',
               'nextcloud_version', 'postgres_major', 'dump_client_major', 'installed',
               'installation_identity_sha256', 'repository_commit', 'dependencies_healthy',
               'maintenance', 'journal_terminal', 'local_lock_available', 'writer_fencing_reviewed',
               'host_key_independently_verified', 'credentials_configured', 'age_verified',
               'recipient_custody_reviewed', 'free_bytes', 'required_bytes', 'session_seconds',
               'required_seconds'}
FLAGS = ('role_active', 'permissions_checked', 'instance_running', 'status_checks_ok',
         'storage_versioned', 'storage_aes256', 'storage_tls', 'public_access_blocked', 'installed',
         'dependencies_healthy', 'journal_terminal', 'local_lock_available', 'writer_fencing_reviewed',
         'host_key_independently_verified', 'credentials_configured', 'age_verified', 'recipient_custody_reviewed')


def evaluate(config, observation, now=None):
    require(isinstance(config, dict) and set(config) == CONFIG, 'PREFLIGHT_CONFIGURATION_SHAPE')
    require(isinstance(observation, dict) and set(observation) == OBSERVATION, 'PREFLIGHT_OBSERVATION_SHAPE')
    require(observation['synthetic'] is True, 'SYNTHETIC_OBSERVATION_REQUIRED')
    patterns = {'account': r'[0-9]{12}', 'region': r'[a-z]{2}(-[a-z]+)+-[0-9]+',
                'project': r'[a-z][a-z0-9-]{1,63}', 'environment': r'[a-z][a-z0-9-]{1,63}',
                'instance_id': r'i-[0-9a-f]{17}',
                'instance_profile': r'arn:aws:iam::[0-9]{12}:instance-profile/[a-z0-9-]+',
                'data_bucket': r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',
                'backups_bucket': r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',
                'installation_identity_sha256': r'[0-9a-f]{64}', 'repository_commit': r'[0-9a-f]{40}'}
    require(all(literal(config[k], pattern) for k, pattern in patterns.items()), 'PREFLIGHT_CONFIGURATION_INVALID')
    require(config['data_bucket'] != config['backups_bucket'], 'STORAGE_SEPARATION')
    require(config['instance_profile'] == f"arn:aws:iam::{config['account']}:instance-profile/"
            f"{config['project']}-{config['environment']}-ec2", 'INSTANCE_PROFILE_CONFIGURATION')
    require(literal(observation['observed_at'], r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'),
            'OBSERVATION_TIME_INVALID')
    try:
        observed = datetime.fromisoformat(observation['observed_at'].replace('Z', '+00:00'))
    except ValueError:
        require(False, 'OBSERVATION_TIME_INVALID')
    now = datetime.now(timezone.utc) if now is None else now
    require(isinstance(now, datetime) and now.tzinfo is not None, 'CHECK_TIME_INVALID')
    age = (now - observed).total_seconds()
    require(0 <= age <= 300, 'OBSERVATION_STALE_OR_FUTURE')
    for key in sorted(CONFIG - {'project', 'environment'}):
        require(observation[key] == config[key], 'TARGET_' + key.upper() + '_MISMATCH')
    require(type(observation['instance_count']) is int and observation['instance_count'] == 1, 'INSTANCE_NOT_UNIQUE')
    require(observation['role_name'] == config['project'] + '-' + config['environment'] + '-backup', 'BACKUP_ROLE_MISMATCH')
    tags = observation['tags']
    require(isinstance(tags, dict) and len(tags) <= 50
            and all(literal(k, r'[A-Za-z0-9_.:/-]{1,128}') and isinstance(v, str) and len(v) <= 256
                    for k, v in tags.items())
            and tags.get('Project') == config['project'] and tags.get('Environment') == config['environment'],
            'TARGET_TAGS_MISMATCH')
    for key in FLAGS:
        require(observation[key] is True, 'PREFLIGHT_' + key.upper())
    require(observation['maintenance'] is False, 'MAINTENANCE_ALREADY_ENABLED')
    require(observation['nextcloud_version'] == '34.0.4'
            and type(observation['postgres_major']) is int and observation['postgres_major'] == 17
            and type(observation['dump_client_major']) is int and observation['dump_client_major'] == 17,
            'PINNED_VERSION_MISMATCH')
    for key in ('free_bytes', 'required_bytes', 'session_seconds', 'required_seconds'):
        require(type(observation[key]) is int and 0 < observation[key] <= 2 ** 50, 'RESOURCE_BUDGET_INVALID')
    require(observation['free_bytes'] >= observation['required_bytes'], 'INSUFFICIENT_STAGING_DISK')
    require(observation['required_seconds'] <= observation['session_seconds'] <= 3600, 'SESSION_BUDGET_INSUFFICIENT')
    # Even all-true synthetic declarations cannot pass a production gate.
    return {**blocked_result('SYNTHETIC_PREFLIGHT_CONFORMANT'), 'synthetic_checks': 'passed',
            'authenticated_evidence': False}
