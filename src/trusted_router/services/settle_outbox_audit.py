"""Content-free row references shared by the worker and router watchdog."""
from __future__ import annotations

import re


def row_ref(payload_hash: str | None) -> str:
    # async_settle_handler.intent freezes billing.canonical_hash(terminal): SHA-256.
    # Fail closed on malformed storage rather than logging arbitrary content.
    return payload_hash[:16] if payload_hash and re.fullmatch(r"[0-9a-f]{64}", payload_hash) else "none"
