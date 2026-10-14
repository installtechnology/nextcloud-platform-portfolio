"""Actual cached PHP/pinned Config.php, synthetic configuration, no containers."""
import json
from pathlib import Path
from scripts.backup.capture.tests import test_read_state as legacy


class OperationalStateReaderTests(legacy.StateReaderTests):
    def setUp(self):
        super().setUp()
        reader = Path(__file__).resolve().parents[1] / 'read-state.php'
        self.helper.write_text(reader.read_text(encoding='utf-8').replace('/var/www/html', self.root.as_posix()), encoding='utf-8')

    def test_sanitized_storage_metadata(self):
        result = self.run_reader()
        self.assertEqual(result.returncode, 0)
        value = json.loads(result.stdout)
        self.assertEqual(value['objectstore_bucket'], 'fixture-data')
        self.assertEqual(value['objectstore_region'], 'us-east-1')
        self.assertEqual(value['objectstore_class'], '\\OC\\Files\\ObjectStore\\S3')
        self.assertIs(value['static_credentials'], False)
        self.assertNotIn(b'synthetic-sensitive-value', result.stdout + result.stderr)

    def test_missing_region_fails_without_diagnostics(self):
        del self.objectstore['arguments']['region']
        self.write_config()
        (self.root/'config/.platform-storage.json').write_text(json.dumps(self.objectstore))
        result = self.run_reader()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, b'', b''))
