"""Backend-neutral assembly of API-key display counter snapshots."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
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
# One small row per chat-browser key, so issuance reads only that user's keys
# in that workspace (a key-range read on the primary key) instead of every key
# in the workspace. The id is <hex digest of workspace and user>.<key hash>.
# A fixed-width lowercase hex prefix sorts the same under a locale collation as
# under bytewise ("C") order, so on Postgres the range id >= prefix AND id <
# prefix + 1 is both exact and an index seek on the primary key whatever the
# database collation. A <workspace>#<user># prefix is neither: a locale
# collation treats '#' as ignorable, so a range on it matches the wrong rows,
# and LIKE can seek only under the C collation.
CHAT_BROWSER_KEY_INDEX_KIND = "chat_browser_key_by_member"
_CHAT_BROWSER_KEY_INDEX_DIGITS = 64


def chat_browser_key_index_prefix(workspace_id: str, user_id: str) -> str:
    return hashlib.sha256(f"{workspace_id}\n{user_id}".encode()).hexdigest()


def chat_browser_key_index_upper(prefix: str) -> str:
    """The smallest id above every id that starts with ``prefix``: the digest
    plus one. (Past the largest digest, any letter after f is above it.)"""
    following = int(prefix, 16) + 1
    if following >= 16**_CHAT_BROWSER_KEY_INDEX_DIGITS:
        return "g"
    return format(following, f"0{_CHAT_BROWSER_KEY_INDEX_DIGITS}x")


def chat_browser_key_index_id(workspace_id: str, user_id: str, key_hash: str) -> str:
    return f"{chat_browser_key_index_prefix(workspace_id, user_id)}.{key_hash}"


def chat_browser_key_index_body(
    workspace_id: str, user_id: str, key_hash: str, expires_at: str | None,
) -> dict[str, Any]:
    """The row names its owner (the id is a digest) and keeps the key's
    expiry, so finished rows can be removed without reading the keys."""
    return {
        "key_id": key_hash,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "expires_at": expires_at,
    }


def chat_browser_key_index_expired(body: dict[str, Any], expired_before: str) -> bool:
    expires_at = body.get("expires_at")
    if not expires_at:
        return False
    try:
        moment = dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        cutoff = dt.datetime.fromisoformat(expired_before.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=dt.UTC)
    return moment <= cutoff


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
