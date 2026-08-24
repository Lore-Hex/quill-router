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

import logging
import time
from threading import Lock
from typing import Any

from trusted_router.benchmark_samples import (
    PUBLIC_BENCHMARK_RECENT_MINUTES,
    PUBLIC_BENCHMARK_SAMPLE_LIMIT,
    public_benchmark_samples,
)
from trusted_router.public_analytics_snapshots import current_public_analytics_snapshot
from trusted_router.storage import STORE
from trusted_router.storage_models import utcnow
from trusted_router.synthetic.leaderboard import aggregate_leaderboard

_TTL_SECONDS = 300
_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_LOCK = Lock()

logger = logging.getLogger(__name__)


def _empty_snapshot() -> dict[str, Any]:
    payload = aggregate_leaderboard([], min_samples=1)
    payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
    return payload


def measured_snapshot(*, test_mode: bool = False) -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    if not test_mode and _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1]

    with _CACHE_LOCK:
        now = time.monotonic()
        if not test_mode and _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
            return _CACHE[1]

        stale_payload = _CACHE[1] if _CACHE is not None else None
        try:
            if test_mode:
                samples = public_benchmark_samples(
                    limit=PUBLIC_BENCHMARK_SAMPLE_LIMIT,
                    recent_minutes=PUBLIC_BENCHMARK_RECENT_MINUTES,
                )
                payload = aggregate_leaderboard(samples, min_samples=1)
                payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
            else:
                reader = getattr(STORE, "public_analytics_snapshot", None)
                precomputed = (
                    current_public_analytics_snapshot("leaderboard", reader=reader)
                    if callable(reader)
                    else None
                )
                payload = precomputed or stale_payload or _empty_snapshot()
        except Exception as exc:
            payload = stale_payload or _empty_snapshot()
            logger.warning(
                "Measured analytics refresh failed; serving %s snapshot (%s)",
                "stale" if stale_payload is not None else "empty",
                type(exc).__name__,
            )

        if not test_mode:
            # Cache failures too so a backend incident cannot turn public page
            # traffic into a retry storm. The next normal TTL refresh retries.
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
