import unittest
from types import SimpleNamespace
from scripts.backup.operational.transport import ArtifactTransport
from scripts.backup.sealing.common import SealFailure


class TransportGateTests(unittest.TestCase):
    def test_simulator_cannot_activate_real_transport(self):
        with self.assertRaisesRegex(SealFailure, '^OPERATIONAL_ADAPTERS_REQUIRED$'):
            ArtifactTransport(SimpleNamespace(simulation=True), SimpleNamespace(synthetic=True), 'synthetic', 'synthetic')
    def test_flag_cannot_activate_real_transport(self):
        with self.assertRaisesRegex(SealFailure, '^OPERATIONAL_ADAPTERS_REQUIRED$'):
            ArtifactTransport(SimpleNamespace(simulation=False), SimpleNamespace(synthetic=False), 'synthetic', 'synthetic')
    def test_completion_unconditionally_requires_live_producer(self):
        obj = object.__new__(ArtifactTransport)
        with self.assertRaisesRegex(SealFailure, '^LIVE_CAPTURE_RECEIPT_REQUIRED$'):
            obj.publish_completion({'backup_complete': True})
