"""Explicit destination preparation only; never run by importing the module."""
from scripts.restore.host_scope import load
from scripts.backup.sealing.common import require
from scripts.backup.operational.sdk import dependencies


def prepare(host,api):
    host.identity()
    require(not api.containers(all=True) and not api.volumes().get('Volumes'),'RESTORE_IMAGE_TARGET_NOT_EMPTY')
    changed=False
    for reference in host.config['images'].values():
        repo,tag=reference.split('@',1)
        repository=repo.split(':',1)[0];expected=repository+'@'+tag
        # Anonymous public pull. Explicit auth avoids discovery of stored Docker credentials.
        try:observed=api.inspect_image(reference)
        except Exception as error:
            from docker.errors import ImageNotFound
            require(isinstance(error,ImageNotFound),'RESTORE_IMAGE_INSPECTION_FAILED')
            host.identity();api.pull(repository,tag=tag,auth_config={'username':'','password':''})
            observed=api.inspect_image(reference);changed=True
        require(expected in observed.get('RepoDigests',[]),'RESTORE_PREPARED_IMAGE_DIGEST_REJECTED')
    return changed


def main():
    try:
        dependencies();host=load();import docker
        api=docker.APIClient(base_url='unix:///var/run/docker.sock',version='1.45',timeout=600)
        try:changed=prepare(host,api)
        finally:api.close()
        print('RESTORE_PINNED_IMAGES_PREPARED' if changed else 'RESTORE_PINNED_IMAGES_PRESENT')
    except BaseException:raise SystemExit('RESTORE_IMAGE_PREPARATION_REJECTED') from None


if __name__=='__main__':main()
