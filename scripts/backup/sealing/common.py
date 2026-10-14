"""Bounded, sanitized primitives shared by injected PR5 components."""
from dataclasses import dataclass
import hashlib
import json
import re
import time

from ..capture.storage import CaptureFailure
from ..json_input import parse_json
from ..limits import Limits
from ..semantics import timestamp


class SealFailure(CaptureFailure):
    """Only fixed codes are allowed in public errors; never remote exception text."""


def require(condition, code):
    if not condition:
        raise SealFailure(code)


@dataclass(frozen=True)
class Bounds:
    pages: int = 1000
    records: int = 20000
    artifact_bytes: int = 8 * 1024 ** 3
    total_artifact_bytes: int = 32 * 1024 ** 3
    chunk_bytes: int = 1024 ** 2
    attempts: int = 3
    timeout_seconds: int = 600
    part_bytes: int = 8 * 1024 ** 2
    multipart_threshold: int = 16 * 1024 ** 2

    def __post_init__(self):
        require(all(type(v) is int and v > 0 for v in vars(self).values()), 'INVALID_LIMITS')
        require(self.attempts <= 5 and self.records <= 20000 and self.pages <= 1000
                and self.chunk_bytes <= 8 * 1024 ** 2 and 5 * 1024 ** 2 <= self.part_bytes <= 64 * 1024 ** 2
                and self.artifact_bytes <= 8 * 1024 ** 3 and self.total_artifact_bytes <= 32 * 1024 ** 3,
                'INVALID_LIMITS')
        require(self.timeout_seconds <= 3600 and self.multipart_threshold <= 64 * 1024 ** 2, 'INVALID_LIMITS')


def json_bytes(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    # The same parser bounds documents produced here; no oversized private JSON.
    parse_json(data, Limits())
    return data


def sha(value):
    return hashlib.sha256(value).hexdigest()


def instant(value):
    """Strict bounded UTC receipt time, compared without dropping fractional precision."""
    require(isinstance(value, str) and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z', value),
        'CAPTURE_TIME_INVALID')
    try:
        return timestamp(value)
    except (ValueError, OverflowError):
        raise SealFailure('CAPTURE_TIME_INVALID') from None


def version(value):
    require(isinstance(value, str) and 0 < len(value) <= 1024 and value.strip()
            and value != 'null' and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value), 'INVALID_VERSION')
    return value


def backup_id(value):
    require(isinstance(value, str) and re.fullmatch(
        r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', value),
        'INVALID_BACKUP_ID')
    return value


class Budget:
    def __init__(self, bounds, cancelled=lambda: False):
        self.end = time.monotonic() + bounds.timeout_seconds
        self.cancelled = cancelled

    def check(self):
        require(not self.cancelled(), 'TRANSFER_CANCELLED')
        require(time.monotonic() < self.end, 'TRANSFER_TIMEOUT')
