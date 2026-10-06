"""Canonical ClickHouse row shape for provider benchmark samples.

Shared by the live outbox ingester and its tests so every writer coerces a
payload the same way: a historical string ``"429"`` must become the same
UInt16 the query path compares against, or the two disagree by construction.
"""

from __future__ import annotations

import datetime as dt
import math
import struct
from typing import Any


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        # The ClickHouse column is Float32. Round here so ingestion and any
        # later comparison fingerprint the same stored value.
        return struct.unpack("!f", struct.pack("!f", parsed))[0]
    except (OverflowError, TypeError, ValueError):
        return None


def normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Canonical row shape; ``None`` when the payload cannot be a sample."""
    created = raw.get("created_at") or ""
    try:
        parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    parsed = parsed.astimezone(dt.UTC)

    status_code = _optional_int(raw.get("error_status"))

    provider, model = raw.get("provider"), raw.get("model")
    if not provider or not model:
        return None

    return {
        "id": str(raw.get("id") or ""),
        "created_at": parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "provider": str(provider),
        "model": str(model),
        "provider_name": str(raw.get("provider_name") or ""),
        "status": str(raw.get("status") or ""),
        "usage_type": str(raw.get("usage_type") or ""),
        "source": str(raw.get("source") or ""),
        "workspace_id": str(raw.get("workspace_id") or ""),
        "streamed": 1 if raw.get("streamed") else 0,
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "total_cost_microdollars": int(raw.get("total_cost_microdollars") or 0),
        "speed_tokens_per_second": _optional_float(raw.get("speed_tokens_per_second")),
        "elapsed_milliseconds": _optional_int(raw.get("elapsed_milliseconds")),
        "first_token_milliseconds": _optional_int(raw.get("first_token_milliseconds")),
        "ttfb_milliseconds": _optional_int(raw.get("ttfb_milliseconds")),
        "finish_reason": raw.get("finish_reason"),
        "error_type": raw.get("error_type"),
        "error_status": status_code,
        "error_message": raw.get("error_message"),
        "region": raw.get("region"),
        "app": str(raw.get("app") or ""),
    }
