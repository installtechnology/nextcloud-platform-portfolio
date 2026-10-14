"""Bounded strict UTF-8 JSON, duplicate-aware before schema evaluation."""
import json
import math
from decimal import Decimal, DecimalException

from .errors import ValidationFailure
from .limits import Limits
from .safe_files import signature


def parse_json(data, limits=Limits()):
    if len(data) > limits.json_bytes:
        raise ValidationFailure('JSON_LIMIT')
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise ValidationFailure('INVALID_JSON') from None
    # Bound nesting before the recursive decoder gets control. Strings/escapes
    # must not be interpreted as structural braces.
    depth, quoted, escaped, bare, nodes = 0, False, False, False, 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            bare = False
            nodes += 1
        elif char in '[{':
            bare = False
            nodes += 1
            depth += 1
            if depth > limits.depth:
                raise ValidationFailure('JSON_LIMIT')
        elif char in ']}':
            bare = False
            depth -= 1
        elif char in ' \t\r\n,:':
            bare = False
        elif not bare:
            bare = True
            nodes += 1
        if nodes > limits.nodes:
            raise ValidationFailure('JSON_LIMIT')

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValidationFailure('DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def integer(value):
        if len(value.lstrip('-')) > limits.integer_digits:
            raise ValidationFailure('JSON_LIMIT')
        return int(value)

    def constant(_value):
        raise ValidationFailure('INVALID_JSON')

    def number(value):
        # All numeric fields in v1 are integers. Do not let binary float rounding
        # turn 1.00000000000000001 into an accepted integer or count.
        if len(value) > 1024:
            raise ValidationFailure('JSON_LIMIT')
        try:
            exact = Decimal(value)
        except DecimalException:
            raise ValidationFailure('INVALID_JSON') from None
        if not exact.is_finite() or exact != exact.to_integral_value():
            raise ValidationFailure('INVALID_JSON')
        if exact.adjusted() >= limits.integer_digits:
            raise ValidationFailure('JSON_LIMIT')
        return int(exact)

    try:
        document = json.loads(text, object_pairs_hook=pairs, parse_int=integer,
                              parse_float=number, parse_constant=constant)
    except (ValueError, RecursionError):
        raise ValidationFailure('INVALID_JSON') from None
    stack = [(document, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.nodes or depth > limits.depth:
            raise ValidationFailure('JSON_LIMIT')
        if isinstance(value, str):
            if len(value) > limits.string_chars:
                raise ValidationFailure('JSON_LIMIT')
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise ValidationFailure('INVALID_JSON') from None
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValidationFailure('INVALID_JSON')
        elif isinstance(value, dict):
            stack.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif isinstance(value, list):
            if len(value) > limits.array_items:
                raise ValidationFailure('JSON_LIMIT')
            stack.extend((item, depth + 1) for item in value)
    if not isinstance(document, dict):
        raise ValidationFailure('JSON_OBJECT_REQUIRED')
    return document


def load_document(root, path, role, limits=Limits()):
    try:
        with root.open(path) as stream:
            before = signature(stream)
            if before[2] > limits.json_bytes:
                raise ValidationFailure('JSON_LIMIT')
            data = stream.read(limits.json_bytes + 1)
            if signature(stream) != before or len(data) != before[2]:
                raise ValidationFailure('FILE_CHANGED')
        return parse_json(data, limits)
    except ValidationFailure as error:
        raise ValidationFailure(error.code, error.exit_code, document=role) from None
