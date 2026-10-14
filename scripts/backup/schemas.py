"""Local v1/v2 schemas only. No reference retrieval or network."""
import json
from pathlib import Path

from .errors import ValidationFailure, EXIT_INTERNAL

SCHEMAS = {'manifest': 'manifest-v1.schema.json', 'inventory': 'object-inventory-v1.schema.json',
           'completion': 'completion-v1.schema.json', 'evidence': 'restore-evidence-v1.schema.json'}


def canonical(value):
    # Type tagging respects JSON Schema equality: true differs from 1, 1 equals 1.0.
    if isinstance(value, bool):
        return ('bool', value)
    if isinstance(value, (int, float)):
        return ('number', value)
    if isinstance(value, dict):
        return ('object', tuple(sorted((k, canonical(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ('array', tuple(map(canonical, value)))
    return (type(value).__name__, value)


def safe_schema(schema):
    stack = [schema]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if '$ref' in item or '$dynamicRef' in item:
                raise ValidationFailure('SCHEMA_REFERENCE_FORBIDDEN', EXIT_INTERNAL)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


class SchemaCatalog:
    def __init__(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker, validators
            from jsonschema.exceptions import ValidationError
            import rfc3339_validator  # noqa: F401 -- require the optional date checker explicitly.
        except ImportError:
            raise ValidationFailure('DEPENDENCY_MISSING', EXIT_INTERNAL) from None

        def unique_items(validator, enabled, instance, schema):
            if enabled and isinstance(instance, list):
                seen = set()
                for value in instance:
                    key = canonical(value)
                    if key in seen:
                        yield ValidationError('DUPLICATE_ITEM')
                        return
                    seen.add(key)

        checker = FormatChecker()
        if 'date-time' not in checker.checkers:
            raise ValidationFailure('FORMAT_CHECKER_MISSING', EXIT_INTERNAL)
        engine = validators.extend(Draft202012Validator, {'uniqueItems': unique_items})
        base = Path(__file__).resolve().parents[2] / 'contracts/recovery/schemas'
        self.validators = {}
        self.v2 = {}
        for role, filename in SCHEMAS.items():
            schema = json.loads((base / filename).read_text(encoding='utf-8'))
            safe_schema(schema)
            Draft202012Validator.check_schema(schema)
            self.validators[role] = engine(schema, format_checker=checker)

        for role in ('manifest', 'completion', 'evidence'):
            filename = SCHEMAS[role].replace('-v1.', '-v2.')
            schema = json.loads((base / filename).read_text(encoding='utf-8'))
            safe_schema(schema)
            Draft202012Validator.check_schema(schema)
            self.v2[role] = engine(schema, format_checker=checker)

    def validate(self, role, document):
        version = document.get('schema_version') if isinstance(document, dict) else None
        if type(version) is not int or version not in (1, 2) or (role == 'inventory' and version != 1):
            raise ValidationFailure('SCHEMA_INVALID', document=role, keyword='const')
        validator = self.v2[role] if version == 2 else self.validators[role]
        error = next(validator.iter_errors(document), None)
        if error is not None:
            # Never expose error.message, instance values or untrusted property names.
            raise ValidationFailure('SCHEMA_INVALID', document=role, keyword=error.validator)
