"""Exercise fresh-install guard offline with fixture config, never calls AWS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

docker = Path(__file__).resolve().parents[1]
php = str(Path(sys.argv[1]).resolve())
env = dict(os.environ, NEXTCLOUD_S3_BUCKET='fixture-nextcloud-data', NEXTCLOUD_S3_REGION='us-east-1')
def run(directory, overrides=None):
    return subprocess.run([php, '-n', str(docker / 'storage-guard.php'), str(directory)],
                          env=dict(env, **(overrides or {})), capture_output=True).returncode

with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    assert run(directory) == 0
    marker = (directory / '.platform-storage.json').read_bytes()
    assert run(directory) == 0
    assert (directory / '.platform-storage.json').read_bytes() == marker
    assert run(directory, {'NEXTCLOUD_S3_BUCKET': 'other-data-bucket'}) != 0
    assert run(directory, {'NEXTCLOUD_S3_REGION': 'eu-west-1'}) != 0
    assert run(directory, {'NEXTCLOUD_S3_BUCKET': ''}) != 0
    (directory / 'config.php').write_text("<?php $CONFIG = ['installed' => true];", encoding='utf-8')
    assert run(directory) != 0  # Local installation cannot be converted.
    (directory / 'zz-platform.config.php').write_bytes((docker / 'platform.config.php').read_bytes())
    assert run(directory) == 0  # Existing matching S3 installation can restart.
    assert run(directory, {'NEXTCLOUD_S3_BUCKET': 'other-data-bucket'}) != 0
    (directory / '.platform-storage.json').unlink()
    assert run(directory) != 0  # No adoption of an existing installation.
    assert not (directory / '.platform-storage.json').exists()
    identity = json.loads(marker)
    assert 'key' not in identity['arguments'] and 'secret' not in identity['arguments']
print('PASS: instalação nova, rerun idempotente, rejeição de instalação local/troca de bucket/região e ausência de credenciais.')
