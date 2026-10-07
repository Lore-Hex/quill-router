"""Dormant warm worker. Claims run inside available executor slots, never a queue.

No scheduling/deployment entry point is installed here. Operators must measure
capacity and approve lease/recovery targets before enabling this mode.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage_gcp_async_admission import (
    claim_health_publish,
    claim_housekeeping,
    publish_health,
)
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

logger = logging.getLogger(__name__)


class _ObservedOutbox(SpannerSettleOutbox):
    """Observe existing fenced return values without changing resolution SQL."""
    resolution: str | None = None

    def mark(self, *args: Any, **kwargs: Any) -> str | None:
        result = super().mark(*args, **kwargs)
        self.resolution = result
        logger.info("async_drain.resolution status=%s fence_miss=%s", result, int(result is None))
        return result

    def park(self, *args: Any, **kwargs: Any) -> bool:
        result = super().park(*args, **kwargs)
        self.resolution = "park" if result else None
        logger.info("async_drain.resolution status=park parked=%s fence_miss=%s", int(result), int(not result))
        return result


def _apply_slot(outbox: SpannerSettleOutbox, shard: int, lease: int,
                deadline: float, stop: threading.Event | None = None) -> tuple[int, str, int]:
    # The slot has already started. There is no claimed executor queue tail.
    # Stop prevents new claims; work claimed before stop still resolves normally.
    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
        return 0, "", 0
    rows = outbox.claim_shard(shard=shard, lease_seconds=lease)
    if not rows:
        return 0, "", 0
    row = rows[0]
    # Claim RPCs can consume the lease/budget. Never start expired work, even
    # though the reservation fence would still prevent double billing.
    now = dt.datetime.now(dt.UTC)
    if (time.monotonic() >= deadline or row.leased_until is None
            or dt.datetime.fromisoformat(row.leased_until.replace('Z', '+00:00')) <= now):
        return 1, "deferred", 0
    started = time.monotonic()
    error_note = None
    apply_error = None
    try:
        outcome = drain.apply_frozen_settle(row)
    except Exception as exc:
        outcome, apply_error = ApplyOutcome.ERROR, exc
        error_note = f"{type(exc).__name__}: {exc}"
    observed = _ObservedOutbox(outbox._database, outbox._pt, async_fence=outbox._async_fence)
    try:
        drain._resolve_row(observed, row, outcome, error_note=error_note, apply_error=apply_error)
    except Exception:
        outcome = "resolve_error"
    # Aggregate only: never emit authorization/key/workspace IDs here.
    if observed.resolution == "done" and row.created_at is not None:
        try:
            created = dt.datetime.fromisoformat(row.created_at.replace('Z', '+00:00'))
            latency = (dt.datetime.now(dt.UTC) - created).total_seconds()
            logger.info("async_drain.completed latency_seconds=%.6f", latency)
        except (ValueError, TypeError):
            logger.warning("async_drain.completion_timestamp_invalid")
    elapsed = time.monotonic() - started
    logger.info("async_drain.timing outcome=%s service_seconds=%.6f observed_at=%.6f",
                outcome, elapsed, time.time())
    return 1, outcome, int(row.actual_cost_micro) if outcome == ApplyOutcome.SETTLED_NOW else 0


def drain_pass(limit: int, *, settings: Settings, start_shard: int = 0,
               stop: threading.Event | None = None) -> dict[str, Any]:
    if not settings.settle_outbox_fast_drain_enabled:
        return drain.drain_settle_outbox(limit,
            reap_snapshot_booking_enabled=settings.reap_snapshot_booking_enabled)
    budget, lease = settings.settle_outbox_pass_budget_seconds, settings.settle_outbox_lease_seconds
    if not 0 < budget < lease:
        raise ValueError("fast drain budget must be below lease")
    outbox = drain.spanner_settle_outbox()
    limit = max(1, min(int(limit), 500))
    concurrency = min(settings.settle_outbox_worker_concurrency, settings.settle_outbox_claim_batch)
    if not 1 <= concurrency <= 32:
        raise ValueError("fast drain concurrency outside bound")
    started = time.monotonic()
    deadline = started + budget
    claimed = recovered = empty = 0
    outcomes: Counter[str] = Counter()
    shard = start_shard % 16
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="settle-drain") as pool:
        while (claimed < limit and time.monotonic() < deadline and empty < 16
               and not (stop is not None and stop.is_set())):
            # At most one row per running slot, even when claim_batch is large.
            width = min(concurrency, limit - claimed)
            futures = [pool.submit(_apply_slot, outbox, (shard + i) % 16, lease, deadline, stop)
                       for i in range(width)]
            shard = (shard + width) % 16
            for future in futures:
                count, outcome, amount = future.result()
                claimed += count
                recovered += amount
                empty = 0 if count else empty + 1
                if outcome:
                    outcomes[outcome] += 1
    purged = reaped = 0
    # Durable five-minute claim: fast polls and multiple replicas share this
    # cadence. A crash delays housekeeping to the next interval, never bursts it.
    try:
        if claim_housekeeping(outbox._database):
            purged, reaped = drain.housekeeping(outbox,
                reap_snapshot_booking_enabled=settings.reap_snapshot_booking_enabled)
    except Exception:
        logger.warning("async_drain.housekeeping_failed")
    try:
        if claim_health_publish(outbox._database, settings.settle_outbox_health_publish_interval_seconds):
            publish_health(outbox._database)
    except Exception:
        # Failed publication must age out, never synthesize healthy evidence.
        logger.warning("async_drain.health_publish_failed")
    logger.info("async_drain.pass claimed=%s outcomes=%s elapsed_seconds=%.6f",
                claimed, dict(outcomes), time.monotonic() - started)
    return dict(claimed=claimed, outcomes=dict(outcomes), recovered_micro=recovered,
                purged=purged, reaped=reaped, deferred=outcomes.get("deferred", 0))


def run_worker(settings: Settings, stop: threading.Event, *, limit: int = 500) -> None:
    """Callable warm-worker loop; stop interrupts polling, not an in-flight apply.

    Existing Spanner apply RPC/retry bounds still apply. A pass budget prevents
    new starts, not cancellation of an already running money transaction.
    """
    if not settings.settle_outbox_fast_drain_enabled:
        raise ValueError("continuous drain requires explicit fast mode")
    shard = 0
    while not stop.is_set():
        try:
            drain_pass(limit, settings=settings, start_shard=shard, stop=stop)
        except Exception:
            logger.warning("async_drain.pass_failed")
        shard = (shard + 1) % 16
        stop.wait(settings.settle_outbox_poll_interval_seconds)
