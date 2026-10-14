"""Explicit runner preparation only; imports never download or install anything."""
import email
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
import zipfile

HERE = Path(__file__).resolve().parent
MAX_WHEEL = 8 * 1024 * 1024
PUBLIC_CODES = frozenset({
    'CPYTHON312_LINUX_AMD64_REQUIRED', 'CREDENTIAL_FREE_RUNNER_REQUIRED',
    'DEPENDENCY_CLOSURE_MISMATCH', 'DEPENDENCY_PROJECTION_MISMATCH', 'DUPLICATE_JSON_KEY',
    'DUPLICATE_PACKAGE', 'INPUT_CHANGED', 'INVALID_ARGUMENTS', 'INVALID_JSON_NUMBER',
    'LOCK_PROVENANCE_MISMATCH', 'PIP_PARSER_REQUIRED', 'POSIX_PREPARATION_ERROR',
    'PREPARATION_PROCESS_FAILED', 'PRIVATE_RUNNER_DIRECTORY_REQUIRED', 'PYTHON_REQUIREMENT_MISMATCH',
    'RECORD_COUNT', 'RECORD_INVALID', 'TARGET_MISMATCH', 'TOP_LEVEL_MISMATCH', 'UBUNTU2404_REQUIRED',
    'UNSAFE_FILENAME', 'UNSAFE_INPUT_FILE', 'UNSUPPORTED_DEPENDENCY', 'UNTRUSTED_METADATA_URL',
    'UNTRUSTED_WHEEL_URL', 'WHEEL_ARCHIVE_LIMIT', 'WHEEL_CHANGED', 'WHEEL_DOWNLOAD_LIMIT',
    'WHEEL_DUPLICATE_ENTRY', 'WHEEL_FILE_INVALID', 'WHEEL_HASH_MISMATCH', 'WHEEL_HTTP_STATUS',
    'WHEEL_LIMIT', 'WHEEL_METADATA_INVALID', 'WHEEL_METADATA_MISMATCH', 'WHEEL_REDIRECT_REJECTED',
    'WHEEL_TARGET_MISMATCH', 'WHEEL_UNSAFE_ARCHIVE', 'WHEELHOUSE_SET_MISMATCH'})


class PreparationFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise PreparationFailure(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def safe_bytes(path, limit):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit, 'UNSAFE_INPUT_FILE')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                and (info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino), 'INPUT_CHANGED')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(len(data) <= limit and (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'INPUT_CHANGED')
    return data


def load_spec(lock=HERE / 'requirements-posix-py312-linux-amd64.lock',
              provenance=HERE / 'dependencies.provenance.json'):
    # pip already present is a runner prerequisite. Its vendored parser is used
    # before installation; missing parser fails closed, never installs packaging.
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.specifiers import SpecifierSet
        from pip._vendor.packaging.tags import cpython_tags, compatible_tags
        from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    except ImportError:
        raise PreparationFailure('PIP_PARSER_REQUIRED') from None
    document = json.loads(safe_bytes(provenance, 128 * 1024), object_pairs_hook=unique,
                          parse_constant=lambda value: require(False, 'INVALID_JSON_NUMBER'))
    require(document['target'] == 'ubuntu-24.04-cpython-3.12-linux-amd64', 'TARGET_MISMATCH')
    require(set(document['top_level']) == {'jsonschema==4.23.0', 'rfc3339-validator==0.1.4'}, 'TOP_LEVEL_MISMATCH')
    records = document['records']
    require(isinstance(records, list) and len(records) == 8, 'RECORD_COUNT')
    versions = {record['name']: record['version'] for record in records}
    require(len(versions) == len(records), 'DUPLICATE_PACKAGE')
    expected = [f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in records]
    actual = [line for line in safe_bytes(lock, 16384).decode().splitlines() if line and not line.startswith('#')]
    require(actual == expected, 'LOCK_PROVENANCE_MISMATCH')
    environment = {'implementation_name': 'cpython', 'implementation_version': '3.12.0',
                   'os_name': 'posix', 'platform_machine': 'x86_64', 'platform_release': '',
                   'platform_system': 'Linux', 'platform_version': '', 'python_full_version': '3.12.0',
                   'python_version': '3.12', 'sys_platform': 'linux', 'extra': ''}
    platforms = ['manylinux_2_17_x86_64', 'manylinux2014_x86_64', 'manylinux_2_28_x86_64']
    tags = set(cpython_tags((3, 12), ['cp312'], platforms)) | set(compatible_tags((3, 12), 'cp312', platforms))
    names = set()
    for record in records:
        require(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', record['name'])
                and re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', record['version'])
                and re.fullmatch(r'[0-9a-f]{64}', record['sha256']), 'RECORD_INVALID')
        require(type(record['size_bytes']) is int and 0 < record['size_bytes'] <= MAX_WHEEL, 'WHEEL_LIMIT')
        filename = record['filename']
        require('/' not in filename and '\\' not in filename and filename not in names, 'UNSAFE_FILENAME')
        names.add(filename)
        url = urlsplit(record['url'])
        require(url.scheme == 'https' and url.netloc == 'files.pythonhosted.org'
                and not url.query and not url.fragment and url.path.endswith('/' + filename), 'UNTRUSTED_WHEEL_URL')
        require(record['metadata_url'] == f"https://pypi.org/pypi/{record['name']}/{record['version']}/json", 'UNTRUSTED_METADATA_URL')
        name, version, _, wheel_tags = parse_wheel_filename(filename)
        require(canonicalize_name(name) == record['name'] and str(version) == record['version']
                and bool(wheel_tags & tags), 'WHEEL_TARGET_MISMATCH')
        require('3.12.0' in SpecifierSet(record['requires_python'] or ''), 'PYTHON_REQUIREMENT_MISMATCH')
        active = []
        for raw in record['requires_dist']:
            dependency = Requirement(raw)
            if dependency.marker and not dependency.marker.evaluate(environment):
                continue
            require(dependency.url is None and not dependency.extras, 'UNSUPPORTED_DEPENDENCY')
            name = canonicalize_name(dependency.name)
            require(name in versions and versions[name] in dependency.specifier, 'DEPENDENCY_CLOSURE_MISMATCH')
            active.append(raw)
        require(active == record['active_dependencies_python312'], 'DEPENDENCY_PROJECTION_MISMATCH')
    require(versions.get('jsonschema') == '4.23.0' and versions.get('rfc3339-validator') == '0.1.4', 'TOP_LEVEL_MISMATCH')
    return records


def verify_wheel(path, record):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import canonicalize_name
    digest = hashlib.sha256()
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == record['size_bytes'], 'WHEEL_FILE_INVALID')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino), 'WHEEL_CHANGED')
        size = 0
        while chunk := stream.read(65536):
            size += len(chunk)
            require(size <= record['size_bytes'], 'WHEEL_CHANGED')
            digest.update(chunk)
        require(size == record['size_bytes'] and digest.hexdigest() == record['sha256'], 'WHEEL_HASH_MISMATCH')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            require(len(entries) <= 5000 and sum(entry.file_size for entry in entries) <= 64 * 1024 ** 2, 'WHEEL_ARCHIVE_LIMIT')
            require(len({entry.filename for entry in entries}) == len(entries), 'WHEEL_DUPLICATE_ENTRY')
            for entry in entries:
                name = PurePosixPath(entry.filename)
                require(not name.is_absolute() and '..' not in name.parts and '\\' not in entry.filename
                        and not stat.S_ISLNK(entry.external_attr >> 16), 'WHEEL_UNSAFE_ARCHIVE')
            metadata = [entry for entry in entries if entry.filename.endswith('.dist-info/METADATA')]
            require(len(metadata) == 1 and metadata[0].file_size <= 128 * 1024, 'WHEEL_METADATA_INVALID')
            with archive.open(metadata[0]) as source:
                raw = source.read(128 * 1024 + 1)
            require(len(raw) <= 128 * 1024, 'WHEEL_METADATA_INVALID')
            parsed = email.message_from_bytes(raw)
            require(len(parsed.get_all('Name', [])) == len(parsed.get_all('Version', [])) == 1
                    and len(parsed.get_all('Requires-Python', [])) <= 1, 'WHEEL_METADATA_INVALID')
            require(canonicalize_name(parsed['Name'] or '') == record['name'] and parsed['Version'] == record['version'], 'WHEEL_METADATA_MISMATCH')
            require(str(SpecifierSet(parsed['Requires-Python'] or '')) == str(SpecifierSet(record['requires_python'] or '')),
                    'WHEEL_METADATA_MISMATCH')
            require(sorted(str(Requirement(raw)) for raw in parsed.get_all('Requires-Dist', []))
                    == sorted(str(Requirement(raw)) for raw in record['requires_dist']), 'WHEEL_METADATA_MISMATCH')
        after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'WHEEL_CHANGED')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PreparationFailure('WHEEL_REDIRECT_REJECTED')


def download_wheel(directory, record):
    # Ignore proxies inherited from runner configuration; never contact another host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    target = directory / record['filename']
    with opener.open(record['url'], timeout=30) as response, target.open('xb') as output:
        require(response.status == 200, 'WHEEL_HTTP_STATUS')
        size = 0
        while chunk := response.read(65536):
            size += len(chunk)
            require(size <= record['size_bytes'], 'WHEEL_DOWNLOAD_LIMIT')
            output.write(chunk)
    verify_wheel(target, record)


def check_platform():
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12)
            and sys.platform == 'linux' and platform.machine() == 'x86_64', 'CPYTHON312_LINUX_AMD64_REQUIRED')
    fields = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    require(fields.get('ID', '').strip('"') == 'ubuntu' and fields.get('VERSION_ID', '').strip('"') == '24.04', 'UBUNTU2404_REQUIRED')
    require(not any(os.environ.get(key) for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
            'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE',
            'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
            'ACTIONS_ID_TOKEN_REQUEST_URL', 'SSH_PRIVATE_KEY', 'NEXTCLOUD_ADMIN_PASSWORD',
            'POSTGRES_ADMIN_PASSWORD', 'POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'GRAFANA_ADMIN_PASSWORD')),
            'CREDENTIAL_FREE_RUNNER_REQUIRED')
    require(not any((Path.home() / '.aws' / name).exists() for name in ('credentials', 'config')), 'CREDENTIAL_FREE_RUNNER_REQUIRED')


def prepare(root):
    check_platform()
    records = load_spec()
    require(root.is_absolute() and root.name.startswith('nextcloud-posix-')
            and root.parent == Path(os.environ['RUNNER_TEMP']).resolve(), 'PRIVATE_RUNNER_DIRECTORY_REQUIRED')
    os.umask(0o077)
    try:
        root.mkdir(mode=0o700)  # Existing path, including a symlink, is never reused.
    except FileExistsError:
        raise PreparationFailure('PRIVATE_RUNNER_DIRECTORY_REQUIRED') from None
    wheels = root / 'wheels'
    wheels.mkdir(mode=0o700)
    for record in records:
        download_wheel(wheels, record)
    require({path.name for path in wheels.iterdir()} == {r['filename'] for r in records}, 'WHEELHOUSE_SET_MISMATCH')
    private_lock = root / 'requirements.lock'
    with private_lock.open('x') as stream:
        stream.write('\n'.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in records) + '\n')
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR')}
    env.update(PIP_CONFIG_FILE='/dev/null', PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONNOUSERSITE='1')
    def run(arguments):
        try:
            result = subprocess.run(arguments, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise PreparationFailure('PREPARATION_PROCESS_FAILED') from None
        require(result.returncode == 0, 'PREPARATION_PROCESS_FAILED')
    venv = root / 'venv'
    run([sys.executable, '-I', '-m', 'venv', str(venv)])
    python = str(venv / 'bin/python')
    run([python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install', '--no-index',
         '--find-links', str(wheels), '--require-hashes', '--only-binary=:all:', '--no-deps', '--no-cache-dir',
         '-r', str(private_lock)])
    run([python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check'])
    expected = {r['name']: r['version'] for r in records}
    assertion = ('import sys,importlib.metadata as m; from pathlib import Path; '
                 'assert sys.version_info[:2]==(3,12) and sys.prefix!=sys.base_prefix; '
                 'assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve(); '
                 'expected=' + repr(expected) + '; '
                 'assert all(m.version(k)==v and Path(m.distribution(k).locate_file("")).resolve().is_relative_to(Path(sys.prefix).resolve()) for k,v in expected.items())')
    run([python, '-I', '-c', assertion, str(venv)])
    import pip
    return {'code': 'POSIX_VENV_PREPARED', 'packages_verified': len(records), 'pip_parser_version': pip.__version__,
            'lock_sha256': hashlib.sha256(safe_bytes(HERE / 'requirements-posix-py312-linux-amd64.lock', 16384)).hexdigest(),
            'python': platform.python_version(), 'backup_complete': False, 'operationally_homologated': False}


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        require(len(args) == 1, 'INVALID_ARGUMENTS')
        report, status = prepare(Path(args[0])), 0
    except PreparationFailure as error:
        code = str(error)
        # Exception bodies never become diagnostics; codes are fixed by this module.
        status = 2
        if code not in PUBLIC_CODES:
            code = 'POSIX_PREPARATION_ERROR'
            status = 3
        report = {'code': code, 'operationally_homologated': False}
    except Exception:
        report, status = {'code': 'POSIX_PREPARATION_ERROR', 'operationally_homologated': False}, 3
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
