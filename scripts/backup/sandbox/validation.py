"""Pure checks on injected snapshots; never invokes Docker, AWS, capture or restore."""
import json
from functools import wraps
from pathlib import Path
from ..capture.storage import CaptureFailure
from .spec import HERE, IMAGES, NETWORKS, environment, project_id, require


def compose_model():
    return json.loads((HERE / 'compose.json').read_text(encoding='utf-8'))


def sanitized(function):
    @wraps(function)
    def check(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure('SANDBOX_INPUT_INVALID') from None
    return check


@sanitized
def validate_compose(document):
    require(isinstance(document, dict) and set(document) == {'name','services','networks','volumes','secrets'}, 'SANDBOX_COMPOSE_INVALID')
    require(document['name'] == 'nc-s3-imds-test-${SANDBOX_ID:?16 lowercase hexadecimal characters required}', 'SANDBOX_PROJECT_REQUIRED')
    require(set(document['services']) == set(IMAGES), 'SANDBOX_FIVE_SERVICES_REQUIRED')
    require(set(document['networks']) == {'db_internal','storage_internal'}, 'SANDBOX_NETWORKS_REQUIRED')
    require(set(document['volumes']) == {'nextcloud_html','postgres_data'}, 'SANDBOX_VOLUMES_REQUIRED')
    for network in document['networks'].values():
        require(network.get('internal') is True and network.get('enable_ipv6') is False
                and set(network) == {'internal','enable_ipv6','labels'}, 'SANDBOX_ISOLATION_REQUIRED')
    for name, service in document['services'].items():
        require(service['image'] == IMAGES[name] and service.get('platform') == 'linux/amd64'
                and service.get('pull_policy') == 'never', 'SANDBOX_IMAGE_MISMATCH')
        require(not set(service) & {'ports','network_mode','privileged','devices','build','env_file','pid','ipc','extends','profiles','cap_add','dns','dns_search','dns_opt','extra_hosts','volumes_from'}, 'SANDBOX_ISOLATION_REQUIRED')
        require(set(service['networks']) == NETWORKS[name], 'SANDBOX_NETWORKS_REQUIRED')
        environment(name, service.get('environment', {}), template=True)
        for mount in service.get('volumes', []):
            if isinstance(mount, str):
                require(mount in {'nextcloud_html:/var/www/html','postgres_data:/var/lib/postgresql/data'}, 'SANDBOX_MOUNT_REJECTED')
            else:
                require(mount.get('type') == 'bind' and mount.get('read_only') is True
                        and mount.get('bind') == {'create_host_path': False}
                        and mount['source'].startswith('${SANDBOX_ROOT:?Private sandbox directory required}/')
                        and '..' not in mount['source'].split('/'), 'SANDBOX_MOUNT_REJECTED')
    # No arbitrary commands/env/entrypoints/mounts accepted beyond the reviewed file.
    require(document == compose_model(), 'SANDBOX_REVIEWED_COMPOSE_REQUIRED')
    return {'code': 'SANDBOX_STATIC_MODEL_VALID', 'runtime_allowed': False,
            'backup_complete': False, 'functional_restore': 'not_verified'}


@sanitized
def validate_snapshot(project, root, snapshot):
    """A valid injected model does not attest a daemon, firewall or continuous fencing."""
    project_id(project)
    supplied_root = Path(root)
    root = supplied_root.resolve(strict=True)
    require(root.name == project and root == supplied_root.absolute() and not supplied_root.is_symlink()
            and not root.is_relative_to(HERE.parents[2])
            and not any(part.lower() in {'.aws','.ssh','.terraform','nextcloud-platform'} for part in root.parts),
            'SANDBOX_ROOT_REQUIRED')
    require(isinstance(snapshot, dict) and snapshot.get('origin') == 'synthetic-local-v1', 'SANDBOX_ORIGIN_REQUIRED')
    controls = snapshot.get('egress_controls', {})
    require(set(controls) == {'external_routes_blocked','imds_ipv4_blocked','imds_ipv6_blocked','exclusive_vm'}
            and all(v is True for v in controls.values()), 'SANDBOX_EGRESS_NOT_PROVEN')
    all_containers = snapshot['containers']
    require(isinstance(all_containers, list) and len(all_containers) <= 64, 'SANDBOX_SNAPSHOT_INVALID')
    found, owned_networks, owned_mounts = {}, set(), set()
    for info in all_containers:
        labels = info['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != project:
            continue
        name = labels.get('com.docker.compose.service')
        require(name in IMAGES and name not in found and labels.get('platform.capture.isolated') == 'true'
                and labels.get('platform.sandbox.kind') == 's3-imds-v1', 'SANDBOX_FIVE_SERVICES_REQUIRED')
        image = snapshot['images'][name]
        repo, digest = IMAGES[name].split('@')
        repo = repo.rsplit(':', 1)[0]
        require(info['Config']['Image'] == IMAGES[name] and info['Image'] == image['Id']
                and image['Os'] == 'linux' and image['Architecture'] == 'amd64'
                and repo + '@' + digest in image['RepoDigests'], 'SANDBOX_IMAGE_MISMATCH')
        host = info['HostConfig']
        require(not host.get('Privileged') and not host.get('PortBindings') and not host.get('CapAdd')
                and host.get('NetworkMode') != 'host' and host.get('PidMode','') not in ('host',)
                and host.get('IpcMode','') != 'host' and not host.get('Devices')
                and not any(str(host.get(key,'')).startswith('container:') for key in ('NetworkMode','PidMode','IpcMode'))
                and not any(host.get(key) for key in ('ExtraHosts','Dns','DnsSearch','DnsOptions','VolumesFrom')),
                'SANDBOX_ISOLATION_REQUIRED')
        if name in ('s3-emulator', 'imds-emulator'):
            require(info['Config'].get('User') == '1000:1000' and host.get('ReadonlyRootfs') is True
                    and host.get('CapDrop') == ['ALL']
                    and any(v in ('no-new-privileges', 'no-new-privileges:true') for v in host.get('SecurityOpt', [])),
                    'SANDBOX_AUXILIARY_CONTEXT_REQUIRED')
        require(len(info['Config']['Env']) == len({v.split('=',1)[0] for v in info['Config']['Env']}), 'SANDBOX_ENV_INVALID')
        environment(name, dict(v.split('=', 1) for v in info['Config']['Env']))
        networks = info['NetworkSettings']['Networks']
        roles = set()
        for network_id in networks:
            network = snapshot['networks'][network_id]
            require(network['Internal'] is True and network['Driver'] == 'bridge'
                    and not network.get('EnableIPv6')
                    and network['Labels'].get('com.docker.compose.project') == project
                    and network['Labels'].get('platform.sandbox.kind') == 's3-imds-v1', 'SANDBOX_ISOLATION_REQUIRED')
            roles.add(network['Labels']['com.docker.compose.network'])
        require(roles == NETWORKS[name], 'SANDBOX_NETWORKS_REQUIRED')
        owned_networks.update(networks)
        for mount in info['Mounts']:
            if mount['Type'] == 'volume':
                volume = snapshot['volumes'][mount['Name']]
                expected_volume = 'nextcloud_html' if name == 'nextcloud' else 'postgres_data'
                require(name in ('nextcloud','postgres') and volume['Driver'] == 'local' and not volume.get('Options')
                        and mount['Name'] == project + '_' + expected_volume
                        and mount.get('Destination') == ('/var/www/html' if name == 'nextcloud' else '/var/lib/postgresql/data')
                        and volume['Labels'].get('com.docker.compose.project') == project
                        and volume['Labels'].get('platform.capture.isolated') == 'true'
                        and volume['Labels'].get('com.docker.compose.volume') == expected_volume, 'SANDBOX_MOUNT_REJECTED')
            elif mount['Type'] == 'bind':
                source = Path(mount['Source'])
                resolved = source.resolve(strict=True)
                allowed = {(root / m['source'].split('}/',1)[1], m['target'])
                           for m in compose_model()['services'][name]['volumes'] if isinstance(m,dict)}
                # Docker Compose file secrets become readonly binds at these paths.
                allowed.update((root / 'secrets' / key, '/run/secrets/' + key)
                               for key in compose_model()['services'][name].get('secrets', []))
                require(not mount.get('RW') and (source,mount.get('Destination')) in allowed
                        and resolved.is_relative_to(root) and source.is_file()
                        and not any(p.is_symlink() for p in (source,*source.parents) if p.is_relative_to(root)),
                        'SANDBOX_MOUNT_REJECTED')
            elif mount['Type'] == 'tmpfs':
                require(mount.get('Destination') in {item.split(':',1)[0]
                        for item in compose_model()['services'][name].get('tmpfs', [])}, 'SANDBOX_MOUNT_REJECTED')
            else:
                require(False, 'SANDBOX_MOUNT_REJECTED')
            if mount['Type'] != 'tmpfs':
                owned_mounts.add(mount['Source'])
        found[name] = info
    require(set(found) == set(IMAGES) and len({i['Id'] for i in found.values()}) == 5, 'SANDBOX_FIVE_SERVICES_REQUIRED')
    for info in all_containers:
        if info['Id'] not in {i['Id'] for i in found.values()}:
            require(not owned_networks.intersection(info['NetworkSettings']['Networks'])
                    and not any(m['Source'] in owned_mounts for m in info['Mounts']), 'SANDBOX_EXTERNAL_WRITER_FOUND')
    return {'code': 'SANDBOX_SNAPSHOT_MODEL_VALID', 'runtime_allowed': False,
            'backup_complete': False, 'functional_restore': 'not_verified'}
