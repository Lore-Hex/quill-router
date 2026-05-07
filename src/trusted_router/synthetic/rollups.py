from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections import Counter
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, iso_now
from trusted_router.synthetic.components import sample_component_ids

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
    component_ids = sample_component_ids(sample) or ["uncategorized"]
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
) -> SyntheticRollup:
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
    apply_sample_to_rollup(rollup, sample)
    return rollup


def apply_sample_to_rollup(rollup: SyntheticRollup, sample: SyntheticProbeSample) -> None:
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
    if sample.latency_milliseconds is not None:
        _increment_histogram(rollup.latency_histogram, sample.latency_milliseconds)
    if sample.ttfb_milliseconds is not None:
        _increment_histogram(rollup.ttfb_histogram, sample.ttfb_milliseconds)
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
        error_counts.update(rollup.error_counts)
        cost_microdollars += rollup.cost_microdollars
        if rollup.last_checked_at and (last_checked_at is None or rollup.last_checked_at > last_checked_at):
            last_checked_at = rollup.last_checked_at
    return {
        "sample_count": sample_count,
        "status_counts": status_counts,
        "p50_latency_milliseconds": percentile_from_histogram(latency_histogram, 50),
        "p95_latency_milliseconds": percentile_from_histogram(latency_histogram, 95),
        "p50_ttfb_milliseconds": percentile_from_histogram(ttfb_histogram, 50),
        "p95_ttfb_milliseconds": percentile_from_histogram(ttfb_histogram, 95),
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
    return _parse_time(sample.created_at) >= now - dt.timedelta(days=days)


def _increment_histogram(histogram: dict[str, int], value: int) -> None:
    key = str(max(int(value), 0))
    histogram[key] = histogram.get(key, 0) + 1


def _merge_histograms(target: dict[str, int], source: dict[str, int]) -> None:
    for key, count in source.items():
        target[key] = target.get(key, 0) + count


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
