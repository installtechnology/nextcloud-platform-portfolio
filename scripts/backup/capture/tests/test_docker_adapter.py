"""Docker adapter mocks only; does not execute Docker or prove isolation operationally."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.backup.capture.docker_backend import DockerBackend
from scripts.backup.capture.storage import CaptureFailure


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = object.__new__(DockerBackend)
        self.backend.project = 'nc-capture-test-fixture'
        self.backend.fixture_root = self.root
        self.backend.images = {'nextcloud': 'nextcloud:synthetic-pin', 'postgres': 'postgres:synthetic-pin'}
        self.backend.containers = {}
        self.calls = []
        self.internal = True
        self.volume_labels = {'platform.capture.isolated': 'true'}
        self.external = False
        self.items = {}
        for service in ('nextcloud', 'postgres', 'redis'):
            self.items[service] = {'Id': service, 'Image': service + '-synthetic-image-id',
                'Config': {'Labels': {'com.docker.compose.service': service, 'platform.capture.isolated': 'true'},
                           'Image': self.backend.images.get(service, 'redis:synthetic-pin'),
                           'Env': [], 'Cmd': ['apache2-foreground'], 'StopSignal': 'SIGWINCH'},
                'State': {'Running': True, 'ExitCode': 0},
                'HostConfig': {'Privileged': False, 'NetworkMode': 'bridge', 'PortBindings': {}},
                'NetworkSettings': {'Networks': {'fixture-net': {}}},
                'Mounts': [{'Type': 'volume', 'Name': service + '-volume',
                            'Source': str(self.root / service), 'Destination': '/var/www/html' if service == 'nextcloud' else '/fixture'}]}
        def run(*args, **kwargs):
            self.calls.append((args, kwargs))
            if args[:2] == ('ps', '-aq'):
                ids = list(self.items)
                if len(args) == 2 and self.external:
                    ids.append('external')
                return '\n'.join(ids).encode()
            if args[0] == 'inspect':
                info = self.items.get(args[1], copy.deepcopy(self.items['nextcloud']))
                if args[1] == 'external':
                    info['Id'] = 'external'
                return json.dumps([info]).encode()
            if args[:2] == ('network', 'inspect'):
                return json.dumps([{'Internal': self.internal}]).encode()
            if args[:2] == ('volume', 'inspect'):
                return json.dumps([{'Driver': 'local', 'Options': None, 'Labels': self.volume_labels}]).encode()
            if args[0] in ('stop', 'start'):
                self.items[args[-1]]['State']['Running'] = args[0] == 'start'
                return b''
            return b''
        self.backend.run = run

    def reject(self, code):
        with self.assertRaises(CaptureFailure) as caught:
            self.backend.isolated()
        self.assertEqual(caught.exception.code, code)

    def test_mock_fixture_allowed(self):
        self.assertEqual(set(self.backend.isolated()), {'nextcloud', 'postgres', 'redis'})

    def test_public_network_rejected(self):
        self.internal = False
        self.reject('EXTERNAL_ACCESS_NOT_FENCED')

    def test_public_port_rejected(self):
        self.items['nextcloud']['HostConfig']['PortBindings'] = {'80/tcp': [{'HostPort': '80'}]}
        self.reject('EXTERNAL_ACCESS_NOT_FENCED')

    def test_privileged_rejected(self):
        self.items['nextcloud']['HostConfig']['Privileged'] = True
        self.reject('EXTERNAL_ACCESS_NOT_FENCED')

    def test_unlabeled_volume_rejected(self):
        self.volume_labels = {}
        self.reject('ISOLATED_VOLUME_REQUIRED')

    def test_external_mount_consumer_rejected(self):
        self.external = True
        self.reject('EXTERNAL_WRITER_FOUND')

    def test_image_mismatch_rejected(self):
        self.items['nextcloud']['Config']['Image'] = 'nextcloud:unapproved'
        self.reject('PINNED_IMAGE_MISMATCH')

    def test_missing_container_rejected(self):
        del self.items['redis']
        self.reject('ISOLATED_THREE_SERVICE_FIXTURE_REQUIRED')

    def test_cron_writer_rejected(self):
        self.items['redis']['Config']['Labels']['com.docker.compose.service'] = 'cron'
        self.reject('UNCONTROLLED_WRITER_OR_FIXTURE')

    def test_forced_exit_not_considered_drain(self):
        self.backend.isolated()
        self.items['nextcloud']['State']['ExitCode'] = 137
        with self.assertRaises(CaptureFailure) as caught:
            self.backend.stop_writer('nextcloud')
        self.assertEqual(caught.exception.code, 'WRITER_NOT_GRACEFULLY_DRAINED')

    def test_pg_dump_custom_create_ownership_not_suppressed(self):
        self.backend.isolated()
        self.backend.db = 'fixture'
        class Runner:
            def stream(inner, argv, output, **kwargs):
                self.assertIn('--format=custom', argv)
                self.assertIn('--create', argv)
                self.assertIn('--host=/var/run/postgresql', argv)
                self.assertIn('--username=postgres', argv)
                self.assertNotIn('--no-owner', argv)
                self.assertNotIn('--no-acl', argv)
                self.assertNotIn('password', ' '.join(argv))
                output.write(b'PGDMP synthetic')
        self.backend.runner = Runner()
        output = io.BytesIO()
        self.backend.dump(output)
        self.assertTrue(output.getvalue().startswith(b'PGDMP'))

    def test_invalid_dump_header_rejected(self):
        path = self.root / 'dump'
        path.write_bytes(b'invalid')
        with self.assertRaises(CaptureFailure) as caught:
            self.backend.check_dump(path)
        self.assertEqual(caught.exception.code, 'DUMP_FORMAT_INVALID')

    def test_catalog_failure_is_sanitized_and_not_restore_proof(self):
        self.backend.isolated()
        path = self.root / 'dump'
        path.write_bytes(b'PGDMP synthetic truncated payload')
        class Runner:
            def stream(inner, *args, **kwargs):
                raise CaptureFailure('LOCAL_COMMAND_FAILED')
        self.backend.runner = Runner()
        with self.assertRaises(CaptureFailure) as caught:
            self.backend.check_dump(path)
        self.assertEqual(caught.exception.code, 'DUMP_CATALOG_INVALID')

    def test_postgres_tools_always_use_local_unix_socket(self):
        self.backend.isolated()
        self.backend.exec_pg('pg_dumpall', '--globals-only', '--no-role-passwords')
        argv = self.calls[-1][0]
        self.assertIn('--host=/var/run/postgresql', argv)
        self.assertIn('--username=postgres', argv)
        self.assertIn('--no-role-passwords', argv)

    def test_peer_gate_covers_other_databases_and_globals(self):
        self.backend.db = 'fixture'
        queries = []
        def pg(*args):
            queries.append(args[-1])
            return b'{"peer_sessions":1}'
        self.backend.exec_pg = pg
        self.assertEqual(self.backend.database_metadata()['peer_sessions'], 1)
        self.assertIn("backend_type='client backend'", queries[0])
        self.assertNotIn('WHERE datname=current_database() AND pid', queries[0])
