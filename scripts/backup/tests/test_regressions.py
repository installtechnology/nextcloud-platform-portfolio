"""Exercise every PR1 oracle regression through the production schema/semantics engine."""
import importlib.util
from pathlib import Path
import unittest

from scripts.backup.errors import ValidationFailure
from scripts.backup.schemas import SchemaCatalog
from scripts.backup.semantics import validate_semantics

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('contract_oracle', REPO / 'contracts/recovery/tests/test_contract.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class Adapter:
    def __init__(self, validator):
        self.validator = validator

    def validate(self, document):
        self.validator.validate(document)

    def is_valid(self, document):
        return self.validator.is_valid(document)

    def iter_errors(self, document):
        return self.validator.iter_errors(document)


class ProductionRegressions(oracle.ContractTests):
    @classmethod
    def setUpClass(cls):
        cls.validators = {role: Adapter(validator) for role, validator in SchemaCatalog().validators.items()}

    def setUp(self):
        # PR1 oracle expects a list. Production intentionally fails on the first error.
        def errors(bundle):
            try:
                validate_semantics(bundle)
                return []
            except ValidationFailure as error:
                return [error.code]
        from unittest.mock import patch
        replacement = patch.object(oracle, 'semantic_example_errors', side_effect=errors)
        replacement.start()
        self.addCleanup(replacement.stop)


class PrecisionTests(unittest.TestCase):
    def test_nanosecond_time_order_not_truncated(self):
        bundle = oracle.sample_bundle()
        bundle['manifest']['created_at'] = '2026-10-09T12:05:01.123456789Z'
        bundle['completion']['completed_at'] = '2026-10-09T12:05:01.123456788Z'
        with self.assertRaises(ValidationFailure) as caught:
            validate_semantics(bundle)
        self.assertEqual(caught.exception.code, 'TIME_ORDER')
