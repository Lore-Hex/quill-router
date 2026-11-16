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
            typed_usage_available=False,
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


# ── Per-user chat-browser key index ──────────────────────────────────────
# One small row per chat-browser key, keyed <workspace>#<user>#<key hash>, so
# issuance reads only that user's keys in that workspace (a key-range read on
# the primary key) instead of every key in the workspace.
CHAT_BROWSER_KEY_INDEX_KIND = "chat_browser_key_by_member"


def chat_browser_key_index_prefix(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}#{user_id}#"


def chat_browser_key_index_id(workspace_id: str, user_id: str, key_hash: str) -> str:
    return f"{chat_browser_key_index_prefix(workspace_id, user_id)}{key_hash}"


def is_users_chat_browser_key(api_key: ApiKey, workspace_id: str, user_id: str) -> bool:
    """Index rows are hints; the key record itself must agree."""
    return api_key.workspace_id == workspace_id and api_key.creator_user_id == user_id


def api_key_usage_from_typed(
    api_key: ApiKey,
    typed: dict[str, Any] | None,
) -> ApiKeyUsageSnapshot:
    """A snapshot from a typed point-read (or the JSON copy when there is no
    typed row yet, as ``api_key_usage_snapshot`` does)."""
    if typed is None:
        return api_key_usage_snapshot(api_key, ())
    return ApiKeyUsageSnapshot(
        api_key=api_key,
        usage_microdollars=int(typed["usage"]),
        byok_usage_microdollars=int(typed["byok_usage"]),
        reserved_microdollars=int(typed["reserved"]),
        windows=dict(typed["windows"]),
    )
