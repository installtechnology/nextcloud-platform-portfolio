"""Portable official SSM plugin; no package installer or remote scripts.

The expected package digest must be independently reviewed before provisioning.
No locally calculated download digest is described as publisher authentication.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request
from .fence_policy import require
from .ssh_channel import verified_plugin

VERSION='1.2.707.0'
URL=f'https://s3.amazonaws.com/session-manager-downloads/plugin/{VERSION}/ubuntu_64bit/session-manager-plugin.deb'
MAXIMUM=32*1024**2


def prepare():
    require(sys.platform=='linux' and sys.version_info[:2]==(3,12),'PLUGIN_PLATFORM_REJECTED')
    digest=os.environ.get('SSM_PLUGIN_PACKAGE_SHA256','')
    require(re.fullmatch(r'[a-f0-9]{64}',digest) is not None,'REVIEWED_PLUGIN_PACKAGE_DIGEST_REQUIRED')
    root=Path(os.environ['RUNNER_TEMP']).resolve()/'nextcloud-ssm-plugin'
    require(root.parent.is_dir() and not root.exists(),'PLUGIN_PRIVATE_DIRECTORY_REQUIRED')
    os.umask(0o077);root.mkdir(mode=0o700);package=root/'package.deb'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    h=hashlib.sha256();size=0
    with opener.open(URL,timeout=30) as response,package.open('xb') as output:
        require(response.geturl()==URL,'PLUGIN_SOURCE_REDIRECT_REJECTED')
        while chunk:=response.read(65536):
            size+=len(chunk);require(size<=MAXIMUM,'PLUGIN_PACKAGE_LIMIT');output.write(chunk);h.update(chunk)
    require(h.hexdigest()==digest,'PLUGIN_PACKAGE_DIGEST_MISMATCH')
    # dpkg-deb merely exposes the archive; no install, hooks or maintainer scripts.
    process=subprocess.Popen(['/usr/bin/dpkg-deb','--fsys-tarfile',str(package)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    selected=False;total=0;binary=root/'session-manager-plugin'
    try:
        with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
            for member in archive:
                total+=max(member.size,0);require(total<=128*1024**2,'PLUGIN_ARCHIVE_LIMIT')
                if member.name.lstrip('./')!='usr/local/sessionmanagerplugin/bin/session-manager-plugin':continue
                require(not selected and member.isfile() and 0<member.size<=MAXIMUM,'PLUGIN_ARCHIVE_INVALID')
                selected=True
                with archive.extractfile(member) as stream,binary.open('xb') as output:
                    while chunk:=stream.read(65536):output.write(chunk)
        require(process.wait(timeout=10)==0 and selected,'PLUGIN_ARCHIVE_INVALID')
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=10)
        process.stdout.close();package.unlink(missing_ok=True)
    os.chmod(binary,0o700)
    with binary.open('rb') as stream:binary_digest=hashlib.file_digest(stream,'sha256').hexdigest()
    verified_plugin(binary,binary_digest,VERSION)
    with Path(os.environ['GITHUB_ENV']).open('a',encoding='utf-8') as env:
        env.write(f'SSM_PLUGIN_PATH={binary}\nSSM_PLUGIN_SHA256={binary_digest}\n')


if __name__=='__main__':
    try:prepare();print('PINNED_SSM_PLUGIN_PREPARED')
    except BaseException:raise SystemExit('SSM_PLUGIN_PREPARATION_REJECTED') from None
