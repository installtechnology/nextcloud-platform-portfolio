"""Reviewed wheel closure. No network/install on import or audit command."""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from .fence_policy import require
from ..orchestration.linux.prepare_posix import safe_bytes,unique,verify_wheel,download_wheel

HERE=Path(__file__).with_suffix('')


def audit(profile='runtime'):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import canonicalize_name,parse_wheel_filename
    from pip._vendor.packaging.tags import cpython_tags,compatible_tags
    data=json.loads(safe_bytes(HERE/'provenance.json',256*1024),object_pairs_hook=unique)
    require(data['target']=='ubuntu-24.04-cpython-3.12-linux-amd64','DEPENDENCY_TARGET_MISMATCH')
    require(profile in ('runtime','qualification'),'DEPENDENCY_PROFILE_REJECTED')
    runtime=data['records'];require(len(runtime)==20,'DEPENDENCY_CLOSURE_MISMATCH')
    extras=data.get('qualification_records',[]) if profile=='qualification' else []
    require(profile!='qualification' or len(extras)==8,'QUALIFICATION_CLOSURE_MISMATCH')
    records=runtime+extras
    versions={r['name']:r['version'] for r in records};require(len(versions)==len(records),'DEPENDENCY_DUPLICATE')
    expected=[f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in runtime]
    lines=[l for l in safe_bytes(HERE/'requirements.lock',32768).decode().splitlines() if l and not l.startswith('#')]
    require(lines==expected,'DEPENDENCY_LOCK_MISMATCH')
    environment={'implementation_name':'cpython','implementation_version':'3.12.0','os_name':'posix','platform_machine':'x86_64',
        'platform_release':'','platform_system':'Linux','platform_version':'','python_full_version':'3.12.0','python_version':'3.12','sys_platform':'linux','extra':''}
    platforms=['manylinux_2_17_x86_64','manylinux2014_x86_64','manylinux_2_28_x86_64']
    tags=set(cpython_tags((3,12),['cp312'],platforms))|set(compatible_tags((3,12),'cp312',platforms))
    for r in records:
        import re
        url=urlsplit(r['url']);name,version,_,wheel_tags=parse_wheel_filename(r['filename'])
        require(url.scheme=='https' and url.netloc=='files.pythonhosted.org' and not url.query and not url.fragment
                and url.path.endswith('/'+r['filename']) and '/' not in r['filename'] and '\\' not in r['filename']
                and r['metadata_url']==f"https://pypi.org/pypi/{r['name']}/{r['version']}/json"
                and re.fullmatch(r'[a-f0-9]{64}',r['sha256']) and 0<r['size_bytes']<=32*1024**2
                and canonicalize_name(name)==r['name'] and str(version)==r['version'] and bool(wheel_tags&tags)
                and '3.12.0' in SpecifierSet(r['requires_python'] or ''),'DEPENDENCY_RECORD_INVALID')
        active=[]
        for raw in r['requires_dist']:
            d=Requirement(raw)
            if d.marker and not d.marker.evaluate(environment):continue
            n=canonicalize_name(d.name)
            require(d.url is None and not d.extras and n in versions and versions[n] in d.specifier,'DEPENDENCY_CLOSURE_MISMATCH');active.append(raw)
        require(active==r['active_dependencies_python312'],'DEPENDENCY_METADATA_MISMATCH')
    return records


def prepare(root,profile='runtime'):
    # Explicit future runner invocation only; never called by local tests.
    require(sys.platform=='linux' and sys.version_info[:2]==(3,12),'CPYTHON312_LINUX_REQUIRED')
    require('VERSION_ID="24.04"' in Path('/etc/os-release').read_text(),'UBUNTU2404_REQUIRED')
    require(profile in ('runtime','qualification'),'DEPENDENCY_PROFILE_REJECTED')
    if profile=='qualification':
        from ..orchestration.linux.prepare_posix import check_platform
        check_platform() # No OIDC, AWS credentials, application secrets or .aws files.
    root=Path(root);require(root.is_absolute() and root.parent==Path(os.environ['RUNNER_TEMP']).resolve()
                           and root.name==('nextcloud-operational' if profile=='runtime' else 'nextcloud-operational-qualification'),
                           'PRIVATE_RUNNER_DIRECTORY_REQUIRED')
    os.umask(0o077);root.mkdir(mode=0o700)
    wheels=root/'wheels';wheels.mkdir(mode=0o700);records=audit(profile)
    lock=HERE/'requirements.lock'
    if profile=='qualification':
        lock=root/'requirements.lock'
        with lock.open('x',encoding='utf-8') as stream:
            stream.write('\n'.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in records)+'\n')
    for record in records:download_wheel(wheels,record)
    require({p.name for p in wheels.iterdir()}=={r['filename'] for r in records},'WHEELHOUSE_SET_MISMATCH')
    for record in records:verify_wheel(wheels/record['filename'],record)
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL')}
    env.update(PIP_CONFIG_FILE='/dev/null',PYTHONNOUSERSITE='1',PIP_DISABLE_PIP_VERSION_CHECK='1')
    subprocess.run([sys.executable,'-I','-m','venv',str(root/'venv')],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    python=str(root/'venv/bin/python')
    subprocess.run([python,'-I','-m','pip','--isolated','install','--no-index','--require-hashes',
        '--only-binary=:all:','--no-deps','--no-cache-dir','--find-links',str(wheels),'-r',str(lock)],
        env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run([python,'-I','-m','pip','check'],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':
    try:
        if len(sys.argv)==1:audit();print('OPERATIONAL_DEPENDENCY_LOCK_VALID')
        elif len(sys.argv)==3 and sys.argv[1]=='prepare':prepare(sys.argv[2]);print('OPERATIONAL_VENV_PREPARED')
        elif len(sys.argv)==3 and sys.argv[1]=='prepare-qualification':prepare(sys.argv[2],'qualification');print('QUALIFICATION_VENV_PREPARED')
        else:raise RuntimeError('DEPENDENCY_ARGUMENTS_REJECTED')
    except Exception:raise SystemExit('DEPENDENCY_PREPARATION_REJECTED') from None
