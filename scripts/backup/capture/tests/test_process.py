"""Transport limits with mocks; POSIX process timeout tests require Linux."""
import io
import os
import sys
import unittest
from unittest.mock import patch

from scripts.backup.capture.process import Runner
from scripts.backup.capture.storage import CaptureFailure


class TransportTests(unittest.TestCase):
    def test_small_output_returns_bytes(self):
        def fake(argv, output, timeout, input_bytes):
            output.write(b'synthetic')
        with patch.object(Runner, 'stream', side_effect=fake):
            self.assertEqual(Runner().run(['fixture-command']), b'synthetic')

    def test_output_limit_during_write(self):
        def fake(argv, output, timeout, input_bytes):
            output.write(bytes(1024 * 1024 + 1))
        with patch.object(Runner, 'stream', side_effect=fake), self.assertRaises(CaptureFailure) as caught:
            Runner().run(['fixture-command'])
        self.assertEqual(caught.exception.code, 'COMMAND_OUTPUT_LIMIT')

    @unittest.skipUnless(os.name == 'posix', 'POSIX local subprocess timeout pending')
    def test_actual_child_timeout(self):
        with self.assertRaises(CaptureFailure) as caught:
            Runner().run([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=0.1)
        self.assertEqual(caught.exception.code, 'COMMAND_TIMEOUT')

    @unittest.skipUnless(os.name == 'posix', 'POSIX local subprocess streaming pending')
    def test_actual_child_input_and_output(self):
        result = Runner().run([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], input_bytes=b'synthetic')
        self.assertEqual(result, b'synthetic')

    @unittest.skipUnless(os.name == 'posix', 'POSIX environment filtering pending')
    def test_actual_child_environment_has_no_inherited_credentials(self):
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'synthetic-private-value', 'POSTGRES_PASSWORD': 'synthetic-private-value'}):
            result = Runner().run([sys.executable, '-c', 'import os; print("AWS_SECRET_ACCESS_KEY" in os.environ or "POSTGRES_PASSWORD" in os.environ)'])
        self.assertEqual(result.strip(), b'False')
