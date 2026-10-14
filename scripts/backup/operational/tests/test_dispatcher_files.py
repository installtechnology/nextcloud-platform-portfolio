"""Executable formatting and installation guards; no authorized command invoked."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[4]
FILES=('nextcloud-recovery','nextcloud-recovery-ssh')

class DispatcherFilesTests(unittest.TestCase):
    def test_linux_shebang_and_lf(self):
        for name in FILES:
            with self.subTest(name=name):
                data=(ROOT/'scripts/backup/operational'/name).read_bytes()
                self.assertTrue(data.startswith(b'#!/bin/sh\n'))
                self.assertNotIn(b'\r',data)
    def test_explicit_git_lf_attributes(self):
        attrs=(ROOT/'.gitattributes').read_text().splitlines()
        for name in FILES:
            self.assertIn('scripts/backup/operational/'+name+' text eol=lf',attrs)
    def test_root_executable_installation(self):
        import yaml
        tasks=yaml.safe_load((ROOT/'ansible/roles/recovery_operational/tasks/main.yml').read_text())
        task=next(t for t in tasks if t['name']=='Install fixed dispatchers')
        self.assertEqual(task['loop'],list(FILES))
        self.assertEqual(task['ansible.builtin.copy']['mode'],'0755')
        self.assertEqual(task['ansible.builtin.copy']['owner'],'root')
        self.assertEqual(task['ansible.builtin.copy']['group'],'root')
    def test_assert_conditions_are_strings(self):
        import yaml
        tasks=yaml.safe_load((ROOT/'ansible/roles/recovery_operational/tasks/main.yml').read_text())
        for task in tasks:
            if 'ansible.builtin.assert' in task:
                self.assertTrue(all(isinstance(v,str) for v in task['ansible.builtin.assert']['that']))

    def test_proxy_forwards_short_buffered_handshake_without_full_read(self):
        from unittest.mock import MagicMock, patch
        from types import SimpleNamespace
        import threading
        from scripts.backup.operational import proxy
        incoming = MagicMock()
        incoming.read1.side_effect = [b'SSH-2.0-synthetic\r\n', b'']
        completed = threading.Event()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.shutdown.side_effect = lambda *_: completed.set()
        def receive(_):
            self.assertTrue(completed.wait(2))
            return b''
        connection.recv.side_effect = receive
        with patch.object(proxy, 'os', SimpleNamespace(name='posix')), patch.object(proxy, 'Path') as path, patch.object(proxy, 'socket', SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, SHUT_WR=1, socket=lambda *_: connection)):
            path.return_value.is_absolute.return_value = True
            proxy.relay('/synthetic/stream.sock', incoming, MagicMock())
        incoming.read.assert_not_called()
        connection.sendall.assert_called_once_with(b'SSH-2.0-synthetic\r\n')
