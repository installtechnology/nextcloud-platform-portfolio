"""Explicit future sandbox server; no Moto import until main is authorized locally."""
import logging
import os
import re
from pathlib import Path
import stat


def main():
    if (os.name != 'posix' or os.geteuid() != 1000
            or os.environ.get('PLATFORM_SYNTHETIC_SANDBOX') != 's3-imds-v1'
            or not re.fullmatch(r'nc-s3-imds-test-[a-f0-9]{16}', os.environ.get('PLATFORM_SANDBOX_PROJECT', ''))
            or os.environ.get('MOTO_S3_CUSTOM_ENDPOINTS') != 'https://s3.sandbox.test:5000'
            or any(k.startswith('AWS_') or k.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                   or (k.startswith('MOTO_') and k != 'MOTO_S3_CUSTOM_ENDPOINTS') for k in os.environ)):
        raise RuntimeError('SANDBOX_SERVER_CONTEXT_REJECTED')
    for name in ('server.crt', 'server.key'):
        info = Path('/fixture', name).lstat()
        if not stat.S_ISREG(info.st_mode) or (name.endswith('key') and info.st_mode & 0o077):
            raise RuntimeError('SANDBOX_TLS_INPUT_REJECTED')
    import moto
    if moto.__version__ != '5.1.22':
        raise RuntimeError('MOTO_VERSION_MISMATCH')
    # Avoid request/error bodies reaching container logs. Diagnostics via tests only.
    logging.disable(logging.CRITICAL)
    from moto.server import main as serve
    serve(['-H', '0.0.0.0', '-p', '5000', '-c', '/fixture/server.crt', '-k', '/fixture/server.key'])


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"code":"SANDBOX_S3_START_FAILED","backup_complete":false}')
        raise SystemExit(2)
