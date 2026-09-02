from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import Counter
from typing import Any

from trusted_router.storage_models import (
    FUTURE_SAMPLE_SKEW_SECONDS,
    SyntheticProbeSample,
    SyntheticRollup,
    iso_now,
)
from trusted_router.synthetic.components import (
    UNCATEGORIZED_COMPONENT,
    sample_component_ids,
)

ROLLUP_PERIODS = {"hour", "day", "month"}
ROLLUP_RETENTION_MONTHS = 24
RAW_SYNTHETIC_RETENTION_DAYS = 14


def rollup_period_start(created_at: str, period: str) -> str:
    parsed = _parse_time(created_at)
    if period == "hour":
        value = parsed.replace(minute=0, second=0, microsecond=0)
    elif period == "day":
        value = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        value = parsed.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unsupported synthetic rollup period: {period}")
    return value.isoformat().replace("+00:00", "Z")


def sample_rollup_ids(sample: SyntheticProbeSample) -> list[tuple[str, str]]:
    component_ids = sample_component_ids(sample) or [UNCATEGORIZED_COMPONENT]
    return [
        (period, component_id)
        for period in ("hour", "day", "month")
        for component_id in component_ids
    ]


def new_rollup_for_sample(
    sample: SyntheticProbeSample,
    *,
    period: str,
    component: str,
    bucket: bool = True,
) -> SyntheticRollup:
    """Build a one-sample rollup. `bucket=False` keeps exact-millisecond
    keys; use it only for transient rollups that are never persisted (the
    status page's raw-sample windows), so they agree with the exact
    percentiles computed from the same samples. Anything written to a
    store must stay bucketed."""
    rollup = SyntheticRollup(
        id=rollup_id(
            period=period,
            period_start=rollup_period_start(sample.created_at, period),
            component=component,
            target=sample.target,
            probe_type=sample.probe_type,
            monitor_region=sample.monitor_region,
            target_region=sample.target_region,
        ),
        period=period,
        period_start=rollup_period_start(sample.created_at, period),
        component=component,
        target=sample.target,
        probe_type=sample.probe_type,
        monitor_region=sample.monitor_region,
        target_region=sample.target_region,
    )
    apply_sample_to_rollup(rollup, sample, bucket=bucket)
    return rollup


def apply_sample_to_rollup(
    rollup: SyntheticRollup,
    sample: SyntheticProbeSample,
    *,
    bucket: bool = True,
) -> None:
    if bucket:
        # A rollup written before bucketing carries one key per distinct
        # millisecond. Fold it here, so the body shrinks on the very next
        # read-modify-write rather than growing until the period rolls over.
        for histogram in _rollup_histograms(rollup):
            _compact_histogram_in_place(histogram)
    rollup.sample_count += 1
    if sample.status == "up":
        rollup.up_count += 1
    elif sample.status == "down":
        rollup.down_count += 1
    elif sample.status == "degraded":
        rollup.degraded_count += 1
    elif sample.status == "routing_degraded":
        rollup.routing_degraded_count += 1
    elif sample.status == "trust_degraded":
        rollup.trust_degraded_count += 1
    else:
        rollup.unknown_count += 1
    for histogram, value in (
        (rollup.latency_histogram, sample.latency_milliseconds),
        (rollup.ttfb_histogram, sample.ttfb_milliseconds),
        (rollup.dns_histogram, sample.dns_milliseconds),
        (rollup.tcp_connect_histogram, sample.tcp_connect_milliseconds),
        (rollup.tls_handshake_histogram, sample.tls_handshake_milliseconds),
        (rollup.gateway_processing_histogram, sample.gateway_processing_milliseconds),
    ):
        if value is not None:
            _increment_histogram(histogram, value, bucket=bucket)
    if sample.error_type:
        rollup.error_counts[sample.error_type] = rollup.error_counts.get(sample.error_type, 0) + 1
    rollup.cost_microdollars += sample.cost_microdollars
    if rollup.last_checked_at is None or sample.created_at > rollup.last_checked_at:
        rollup.last_checked_at = sample.created_at
    rollup.updated_at = iso_now()


def rollup_id(
    *,
    period: str,
    period_start: str,
    component: str,
    target: str,
    probe_type: str,
    monitor_region: str,
    target_region: str | None,
) -> str:
    key = "|".join(
        [
            period,
            period_start,
            component,
            target,
            probe_type,
            monitor_region,
            target_region or "",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def merge_rollups(rollups: list[SyntheticRollup]) -> dict[str, Any]:
    status_counts: dict[str, int] = {
        "up": 0,
        "down": 0,
        "degraded": 0,
        "routing_degraded": 0,
        "trust_degraded": 0,
        "unknown": 0,
    }
    latency_histogram: dict[str, int] = {}
    ttfb_histogram: dict[str, int] = {}
    dns_histogram: dict[str, int] = {}
    tcp_connect_histogram: dict[str, int] = {}
    tls_handshake_histogram: dict[str, int] = {}
    gateway_processing_histogram: dict[str, int] = {}
    error_counts: Counter[str] = Counter()
    cost_microdollars = 0
    sample_count = 0
    last_checked_at: str | None = None
    for rollup in rollups:
        sample_count += rollup.sample_count
        status_counts["up"] += rollup.up_count
        status_counts["down"] += rollup.down_count
        status_counts["degraded"] += rollup.degraded_count
        status_counts["routing_degraded"] += rollup.routing_degraded_count
        status_counts["trust_degraded"] += rollup.trust_degraded_count
        status_counts["unknown"] += rollup.unknown_count
        _merge_histograms(latency_histogram, rollup.latency_histogram)
        _merge_histograms(ttfb_histogram, rollup.ttfb_histogram)
        _merge_histograms(dns_histogram, rollup.dns_histogram)
        _merge_histograms(tcp_connect_histogram, rollup.tcp_connect_histogram)
        _merge_histograms(tls_handshake_histogram, rollup.tls_handshake_histogram)
        _merge_histograms(
            gateway_processing_histogram,
            rollup.gateway_processing_histogram,
        )
        error_counts.update(rollup.error_counts)
        cost_microdollars += rollup.cost_microdollars
        if rollup.last_checked_at and (
            last_checked_at is None or rollup.last_checked_at > last_checked_at
        ):
            last_checked_at = rollup.last_checked_at
    return {
        "sample_count": sample_count,
        "status_counts": status_counts,
        "p50_latency_milliseconds": percentile_from_histogram(latency_histogram, 50),
        "p95_latency_milliseconds": percentile_from_histogram(latency_histogram, 95),
        "p50_ttfb_milliseconds": percentile_from_histogram(ttfb_histogram, 50),
        "p95_ttfb_milliseconds": percentile_from_histogram(ttfb_histogram, 95),
        "p50_dns_milliseconds": percentile_from_histogram(dns_histogram, 50),
        "p95_dns_milliseconds": percentile_from_histogram(dns_histogram, 95),
        "p50_tcp_connect_milliseconds": percentile_from_histogram(tcp_connect_histogram, 50),
        "p95_tcp_connect_milliseconds": percentile_from_histogram(tcp_connect_histogram, 95),
        "p50_tls_handshake_milliseconds": percentile_from_histogram(tls_handshake_histogram, 50),
        "p95_tls_handshake_milliseconds": percentile_from_histogram(tls_handshake_histogram, 95),
        "p50_gateway_processing_milliseconds": percentile_from_histogram(
            gateway_processing_histogram, 50
        ),
        "p95_gateway_processing_milliseconds": percentile_from_histogram(
            gateway_processing_histogram, 95
        ),
        "top_error": error_counts.most_common(1)[0][0] if error_counts else None,
        "last_checked_at": last_checked_at,
        "cost_microdollars": cost_microdollars,
    }


def percentile_from_histogram(histogram: dict[str, int], percentile: int) -> int | None:
    total = sum(histogram.values())
    if total <= 0:
        return None
    threshold = max(1, math.ceil(total * percentile / 100))
    seen = 0
    for raw_value, count in sorted(histogram.items(), key=lambda item: int(item[0])):
        seen += count
        if seen >= threshold:
            return int(raw_value)
    return None


def rollup_is_within_retention(
    rollup: SyntheticRollup,
    *,
    now: dt.datetime,
    months: int = ROLLUP_RETENTION_MONTHS,
) -> bool:
    parsed = _parse_time(rollup.period_start)
    cutoff_month = (now.year * 12 + now.month - 1) - months + 1
    rollup_month = parsed.year * 12 + parsed.month - 1
    return rollup_month >= cutoff_month


def raw_sample_is_within_retention(
    sample: SyntheticProbeSample,
    *,
    now: dt.datetime,
    days: int = RAW_SYNTHETIC_RETENTION_DAYS,
) -> bool:
    """Bounded on BOTH sides. The lower bound is retention. The upper
    bound excludes future-dated poison: a sample dated past the skew
    budget (e.g. a year-7748 conformance fixture written to a live
    store) would otherwise sort first in every newest-first read forever
    — retention alone never expires a row dated in the future."""
    created = _parse_time(sample.created_at)
    if created > now + dt.timedelta(seconds=FUTURE_SAMPLE_SKEW_SECONDS):
        return False
    return created >= now - dt.timedelta(days=days)


#: Millisecond values below this are stored exactly; above it they are
#: rounded to HISTOGRAM_SIGNIFICANT_DIGITS significant digits. Keys stay
#: integer milliseconds (as strings), so every reader that sorts keys with
#: int() and walks counts keeps working, and pre-bucketing rows — whose keys
#: are just finer-grained integers — need no migration: `compact_histogram`
#: folds them into buckets lazily, on the row's next write.
HISTOGRAM_EXACT_BELOW = 100
HISTOGRAM_SIGNIFICANT_DIGITS = 2


def histogram_bucket(value: int) -> int:
    """Map a millisecond value to its bucket representative.

    Exact below HISTOGRAM_EXACT_BELOW, then log-linear: 2 significant
    digits, rounded to nearest (ties up). Each decade contributes at most
    90 keys, so a histogram over 0..10^7 ms holds fewer than 600 keys
    instead of one per distinct millisecond — which on the Postgres/DSQL
    and Bigtable backends is the size of the body re-read and re-written
    on EVERY sample for the rest of the period. The map is monotone
    (a <= b implies bucket(a) <= bucket(b)) and idempotent, so a percentile
    taken from bucketed counts is exactly bucket(exact percentile). Rounding
    is to nearest, so the error is at most half a step (10^(digits-2) / 2):
    within +/-5% above 100 ms, exact below. Nearest keeps the published
    p50/p95 unbiased; it can sit slightly under the true value as well as
    over it, unlike `client_reliability.LATENCY_BUCKETS`, which reports
    bucket upper bounds. (Buckets straddling a decade boundary, e.g. the one
    keyed 1000 covering 995..1049, are wider than a step but never exceed
    the 5% bound.)
    """
    value = max(int(value), 0)
    if value < HISTOGRAM_EXACT_BELOW:
        return value
    step = 10 ** (len(str(value)) - HISTOGRAM_SIGNIFICANT_DIGITS)
    return (value + step // 2) // step * step


def _bucket_key(raw_key: str) -> str:
    """The stored key for a histogram key. A key that is not an integer
    literal cannot be folded; it is kept verbatim, exactly as every writer
    kept it before bucketing, so one odd historical key never aborts a
    sample write, a ClickHouse recompute, or a backfill."""
    try:
        return str(histogram_bucket(int(raw_key)))
    except (TypeError, ValueError):
        return str(raw_key)


def compact_histogram(histogram: dict[str, int]) -> dict[str, int]:
    """Fold every integer key into its bucket, preserving total count."""
    compacted: dict[str, int] = {}
    for raw_key, count in histogram.items():
        key = _bucket_key(raw_key)
        compacted[key] = compacted.get(key, 0) + int(count)
    return compacted


def _compact_histogram_in_place(histogram: dict[str, int]) -> None:
    if all(_bucket_key(key) == key for key in histogram):
        return
    compacted = compact_histogram(histogram)
    histogram.clear()
    histogram.update(compacted)


def _rollup_histograms(rollup: SyntheticRollup) -> tuple[dict[str, int], ...]:
    return (
        rollup.latency_histogram,
        rollup.ttfb_histogram,
        rollup.dns_histogram,
        rollup.tcp_connect_histogram,
        rollup.tls_handshake_histogram,
        rollup.gateway_processing_histogram,
    )


def _increment_histogram(histogram: dict[str, int], value: int, *, bucket: bool = True) -> None:
    key = str(histogram_bucket(value) if bucket else max(int(value), 0))
    histogram[key] = histogram.get(key, 0) + 1


def _merge_histograms(target: dict[str, int], source: dict[str, int]) -> None:
    # A plain key-sum on purpose: merging never changes precision. Persisted
    # rows arrive bucketed (or legacy-exact until their next write), transient
    # status-window rows arrive exact, and a mixed walk still lands within
    # half a bucket of the exact percentile because every element moved by
    # at most that much.
    for key, count in source.items():
        target[key] = target.get(key, 0) + count


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
