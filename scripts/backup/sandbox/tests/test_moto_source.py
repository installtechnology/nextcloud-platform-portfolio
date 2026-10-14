"""Unmodified, hash-pinned official Moto method; dependencies are synthetic."""
import ast
import copy
import hashlib
import itertools
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from scripts.backup.sealing.inventory import inventory
from scripts.backup.sealing.common import SealFailure
from scripts.backup.sealing.tests.support import SOURCE, TEMPLATE

SOURCE_SHA256 = 'b25f031c5e785212801af5aec1b8d1732736478a424c59a0383f5f1e52a20175'

class FakeKey:
    def __init__(self, name, version_id, when):
        self.name, self.version_id, self.last_modified = name, version_id, when

class FakeDeleteMarker:
    def __init__(self, name, version_id, when):
        self.name, self.version_id, self.last_modified = name, version_id, when

class Keys:
    def __init__(self, values): self.values = values
    def iterlists(self): return [(k, [v for v in self.values if v.name == k]) for k in sorted({v.name for v in self.values})]

class Backend:
    def __init__(self, values): self.bucket = type('Bucket', (), {'keys': Keys(values)})()
    def get_bucket(self, name): return self.bucket

class MotoSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('SANDBOX_TEST_MOTO_SOURCE')
        if not path: raise unittest.SkipTest('Official Moto source must be supplied explicitly; no download or fallback')
        with Path(path).open('rb') as stream:
            source = stream.read(1048577)
        if len(source) > 1048576 or hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
            raise RuntimeError('MOTO_SOURCE_DIGEST_REJECTED')
        tree = ast.parse(source)
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'list_object_versions']
        if len(functions) != 1: raise RuntimeError('MOTO_SOURCE_METHOD_REJECTED')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), functions[0]], type_ignores=[])
        ns = {'copy':copy, 'itertools':itertools, 'unix_time_millis':lambda t:int(t.timestamp()*1000), 'FakeKey':FakeKey}
        # Only the audited function definition, never Moto module imports or initialization.
        exec(compile(ast.fix_missing_locations(module), 'official-moto-5.1.22', 'exec'), ns)
        Backend.list_object_versions = ns['list_object_versions']

    def values(self, count=4, deleted=False):
        now = datetime(2026,1,1,tzinfo=timezone.utc)
        return [(FakeDeleteMarker if deleted and i == 0 else FakeKey)('synthetic.txt', str(i), now-timedelta(seconds=i)) for i in range(count)]

    def pages(self, values, limit):
        backend = Backend(values); marker, vid = None, None; pages = []
        for _ in range(len(values)+1):
            result = backend.list_object_versions('fixture-data', max_keys=limit, key_marker=marker, version_id_marker=vid)
            pages.append(result)
            marker, vid = result[3:5]
            if marker is None: return pages
        self.fail('Pagination failed to terminate')

    def wire(self, pages):
        def record(v): return {'Key':v.name, 'VersionId':v.version_id, 'IsLatest':v.is_latest, 'Size':1, 'ETag':'"synthetic"'}
        return [{'Versions':[record(v) for v in p[0]], 'DeleteMarkers':[record(v) for v in p[2]], 'IsTruncated':p[3] is not None,
                 **({'NextKeyMarker':p[3], 'NextVersionIdMarker':p[4]} if p[3] is not None else {})} for p in pages]

    def observe(self, responses):
        class API:
            def list_versions(self,*args): return responses.pop(0)
            def head(self,bucket,key,vid): return {'VersionId':vid,'ContentLength':1,'ETag':'"synthetic"'}
        return inventory(API(), SOURCE, TEMPLATE['backup_id'], 'capture-fixture')

    def test_original_two_page_reproducer(self):
        pages=self.pages(self.values(2),1)
        self.assertTrue(pages[0][0][0].is_latest)
        self.assertFalse(pages[1][0][0].is_latest)
        self.assertEqual(pages[0][3:5], ('synthetic.txt','0'))

    def test_current_delete_marker_old_objects(self):
        pages=self.pages(self.values(deleted=True),1)
        self.assertTrue(pages[0][2][0].is_latest)
        self.assertFalse(pages[1][0][0].is_latest)
        self.assertEqual(self.observe(self.wire(pages))['document']['absent_current_keys'], ['synthetic.txt'])

    def test_page_limits_and_default_boundary(self):
        for count,limit in ((4,1),(4,2),(4,3),(4,4),(4,1000),(1001,1000)):
            with self.subTest(count=count,limit=limit):
                pages=self.pages(self.values(count),limit)
                records=[v for p in pages for v in p[0]+p[2]]
                self.assertEqual(len(records),count)
                self.assertEqual(len({v.version_id for v in records}),count)
                self.assertEqual(sum(v.is_latest for v in records),1)
                self.assertTrue(all(len(p[0]+p[2]) <= limit for p in pages))
                self.assertEqual(self.observe(self.wire(pages))['document']['objects'][0]['version_id'],'0')

    def test_multiple_unicode_keys_and_old_delete_marker(self):
        values=self.values()
        now=datetime(2026,1,2,tzinfo=timezone.utc)
        values += [FakeKey('á /%25', 'opaque/+==', now),FakeDeleteMarker('á /%25','older',now-timedelta(seconds=1))]
        result=self.observe(self.wire(self.pages(values,1)))
        self.assertEqual(result['document']['object_count'],2)
        self.assertEqual(len(result['delete_markers']),0)

    def test_invalid_duplicate_current_exact_reason(self):
        pages=self.wire(self.pages(self.values(2),1));pages[1]['Versions'][0]['IsLatest']=True
        with self.assertRaises(SealFailure) as error: self.observe(pages)
        self.assertEqual(error.exception.code,'AMBIGUOUS_CURRENT_VERSION')

    def test_invalid_missing_current_exact_reason(self):
        pages=self.wire(self.pages(self.values(2),2));pages[0]['Versions'][0]['IsLatest']=False
        with self.assertRaises(SealFailure) as error: self.observe(pages)
        self.assertEqual(error.exception.code,'AMBIGUOUS_CURRENT_VERSION')

    def test_invalid_truncated_listing_exact_reason(self):
        pages=self.wire(self.pages(self.values(2),1));pages[0].pop('NextKeyMarker')
        with self.assertRaises(SealFailure) as error: self.observe(pages)
        self.assertEqual(error.exception.code,'INCOMPLETE_LISTING')

if __name__ == '__main__': unittest.main()
