from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from typing import Any

from trusted_router.storage_models import Generation


def json_body(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def member_id(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}#{user_id}"


def byok_id(workspace_id: str, provider: str) -> str:
    return f"{workspace_id}#{provider}"


def workspace_key_id(workspace_id: str, key_hash: str) -> str:
    return f"{workspace_id}#{key_hash}"


def generation_workspace_id(generation: Generation) -> str:
    return f"{generation.workspace_id}#{generation.created_at[:10]}#{generation.created_at}#{generation.id}"


def reverse_time_key(created_at: str) -> str:
    parsed = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    epoch_ms = int(parsed.timestamp() * 1000)
    return f"{9_999_999_999_999 - epoch_ms:013d}"


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        normalized = f"{normalized}@trustedrouter.local"
    return normalized
