"""Backend-neutral assembly of API-key display counter snapshots."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_counters import key_usage_shard_count
from trusted_router.storage_models import ApiKey, ApiKeyUsageSnapshot


def api_key_from_json(raw: Any) -> ApiKey:
    """Decode a forward-compatible API-key entity body."""
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    known = {field.name for field in dataclasses.fields(ApiKey)}
    return ApiKey(**{key: value for key, value in data.items() if key in known})


def api_key_usage_snapshot(
    api_key: ApiKey,
    rows: Sequence[Sequence[Any]],
) -> ApiKeyUsageSnapshot:
    """Combine the configured typed shards, failing closed when incomplete.

    ``rows`` use the tr_key_limit display column order beginning with shard.
    A completely missing typed row keeps the pre-cutover JSON fallback used by
    the old page.  Once any configured shard exists, every configured shard
    must exist; silently displaying a partial sum would under-report spend.
    """
    shard_count = key_usage_shard_count(api_key)
    if not rows:
        return ApiKeyUsageSnapshot(
            api_key=api_key,
            usage_microdollars=api_key.usage_microdollars,
            byok_usage_microdollars=api_key.byok_usage_microdollars,
            reserved_microdollars=api_key.reserved_microdollars,
            windows={"daily": 0, "weekly": 0, "monthly": 0},
        )

    ordered_rows = sorted(rows, key=lambda row: int(row[0]))
    if [int(row[0]) for row in ordered_rows] != list(range(shard_count)):
        raise RuntimeError("configured tr_key_limit usage shard set is incomplete")

    floors = window_floors(utcnow())

    def current_window_usage(usage_index: int, start_index: int, window: str) -> int:
        return sum(
            int(row[usage_index] or 0)
            for row in ordered_rows
            if row[start_index] is not None and row[start_index] >= floors[window]
        )

    return ApiKeyUsageSnapshot(
        api_key=api_key,
        usage_microdollars=sum(int(row[1]) for row in ordered_rows),
        byok_usage_microdollars=sum(int(row[2]) for row in ordered_rows),
        reserved_microdollars=sum(int(row[3]) for row in ordered_rows),
        windows={
            "daily": current_window_usage(4, 5, "daily"),
            "weekly": current_window_usage(6, 7, "weekly"),
            "monthly": current_window_usage(8, 9, "monthly"),
        },
    )
