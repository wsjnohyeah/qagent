from __future__ import annotations

import secrets
import time
import uuid


def uuid7(now_ms: int | None = None) -> str:
    """Generate an RFC 9562 UUIDv7 without an external runtime dependency."""
    timestamp_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < 1 << 48:
        raise ValueError("UUIDv7 timestamp is outside the 48-bit range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))

