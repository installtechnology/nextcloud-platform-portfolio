"""Confined Nextcloud file aliases; synthetic archives only."""
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from scripts.backup.capture.archive import archive_tree
from scripts.backup.capture.storage import CaptureFailure

class ArchiveLinks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'html';self.root.mkdir()
        (self.root/'license').write_bytes(b'synthetic license')
    def link(self,target,name='alias'):
        try:(self.root/name).symlink_to(target)
        except OSError:self.skipTest('symlink creation unavailable')
    def rejected(self):
        with self.assertRaisesRegex(CaptureFailure,'^LOCAL_LINK_REQUIRES_REVIEW$'):
            archive_tree(self.root,io.BytesIO())
    def test_sibling_regular_file_preserved_as_link(self):
        self.link('license');out=io.BytesIO();archive_tree(self.root,out)
        with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
            member=tar.getmember('alias');self.assertTrue(member.issym());self.assertEqual(member.linkname,'license')
            self.assertEqual(tar.extractfile('license').read(),b'synthetic license')
    def test_absolute_target_rejected(self):self.link(str(self.root/'license'));self.rejected()
    def test_parent_traversal_rejected(self):self.link('../outside');self.rejected()
    def test_subdirectory_target_rejected(self):self.link('nested/file');self.rejected()
    def test_same_directory_chain_preserved(self):
        self.link('license','second');self.link('second');out=io.BytesIO();archive_tree(self.root,out)
        with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
            self.assertEqual(tar.getmember('alias').linkname,'second')
            self.assertEqual(tar.getmember('second').linkname,'license')
    def test_cycle_rejected(self):self.link('second');self.link('alias','second');self.rejected()
    def test_chain_external_target_rejected(self):self.link('second');self.link('../outside','second');self.rejected()
    def test_chain_limit_rejected(self):
        for i in range(17):self.link('license' if i==16 else 'link'+str(i+1),'link'+str(i))
        self.rejected()
    def test_directory_target_rejected(self):
        (self.root/'directory').mkdir();self.link('directory');self.rejected()
    def test_dangling_target_rejected(self):self.link('missing');self.rejected()
    def test_link_swap_during_archive_rejected(self):
        self.link('license');(self.root/'other').write_bytes(b'other');original=tarfile.TarFile.addfile
        def swapped(archive,member,fileobj=None):
            original(archive,member,fileobj)
            if member.issym():
                (self.root/'alias').unlink();(self.root/'alias').symlink_to('other')
        with patch.object(tarfile.TarFile,'addfile',swapped):
            with self.assertRaisesRegex(CaptureFailure,'^LOCAL_TREE_CHANGED$'):archive_tree(self.root,io.BytesIO())
