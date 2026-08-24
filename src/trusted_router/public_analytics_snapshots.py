"""Validation shared by public consumers of precomputed analytics snapshots."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

PUBLIC_ANALYTICS_SNAPSHOT_MAX_AGE_SECONDS = 600


def current_public_analytics_snapshot(
    name: str,
    *,
    reader: Callable[[str], dict[str, Any] | None],
    now: dt.datetime | None = None,
    max_age_seconds: int = PUBLIC_ANALYTICS_SNAPSHOT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    payload = reader(name)
    if not isinstance(payload, dict):
        return None
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        return None
    try:
        generated = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.UTC)
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    age = (current.astimezone(dt.UTC) - generated.astimezone(dt.UTC)).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None
    return payload
