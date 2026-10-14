"""Fixed defensive bounds, not instructions for production capacity sizing."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    json_bytes: int = 8 * 1024 * 1024
    depth: int = 32
    nodes: int = 100_000
    string_chars: int = 4096
    array_items: int = 20_000
    inventory_objects: int = 10_000
    integer_digits: int = 128
    artifact_bytes: int = 8 * 1024 * 1024 * 1024
    total_artifact_bytes: int = 32 * 1024 * 1024 * 1024
    chunk_bytes: int = 1024 * 1024
