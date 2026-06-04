"""Cached measured-performance accessors for the per-model and per-provider
public pages.

The /leaderboard route has its own cached snapshot; these accessors give the
render-only dashboard functions the same measured data (p50/p95 TTFT/TTFB,
throughput, uptime) sliced per model or per provider — behind a short TTL so a
model/provider page view never triggers a live store scan. Pass
`test_mode=True` (settings.environment == "test") to bypass the cache so the
per-test STORE reset isn't masked by a stale snapshot.
"""

from __future__ import annotations

import time
from typing import Any

from trusted_router.storage import STORE
from trusted_router.storage_models import utcnow
from trusted_router.synthetic.leaderboard import aggregate_leaderboard

_SAMPLE_LIMIT = 8_000
_TTL_SECONDS = 60
_CACHE: tuple[float, dict[str, Any]] | None = None


def measured_snapshot(*, test_mode: bool = False) -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    if not test_mode and _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1]
    samples = STORE.provider_benchmark_samples(date=None, limit=_SAMPLE_LIMIT)
    payload = aggregate_leaderboard(samples, min_samples=1)
    payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
    if not test_mode:
        _CACHE = (now, payload)
    return payload


def measured_for_model(model_id: str, *, test_mode: bool = False) -> list[dict[str, Any]]:
    """Per-provider measured rows for one model (a model can be served by more
    than one provider; each is a distinct sample group)."""
    snapshot = measured_snapshot(test_mode=test_mode)
    return [row for row in snapshot["models"] if row["model"] == model_id]


def measured_for_provider(provider: str, *, test_mode: bool = False) -> dict[str, Any]:
    snapshot = measured_snapshot(test_mode=test_mode)
    provider_row = next(
        (row for row in snapshot["providers"] if row["provider"] == provider), None
    )
    models = [row for row in snapshot["models"] if row["provider"] == provider]
    return {"provider_row": provider_row, "models": models}
