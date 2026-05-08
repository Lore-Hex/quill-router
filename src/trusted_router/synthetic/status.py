from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, iso_now, utcnow
from trusted_router.synthetic.components import (
    COMPONENT_DEFINITIONS,
    sample_component_ids,
)
from trusted_router.synthetic.rollups import merge_rollups, new_rollup_for_sample

CURRENT_SAMPLE_TTL_SECONDS = 5 * 60
STATUS_HISTORY_HOURS = 48
# Uptime thresholds for per-bucket coloring. Single-sample blips
# shouldn't paint a whole hour red; tune the cutoffs to roughly match
# what status.anthropic.com / GitHub status surface.
STATUS_HISTORY_UP_MIN_UPTIME = 99.5
STATUS_HISTORY_DEGRADED_MIN_UPTIME = 95.0
STATUS_ORDER = {
    "up": 0,
    "degraded": 1,
    "routing_degraded": 1,
    "trust_degraded": 2,
    "down": 3,
    "unknown": 4,
}
WINDOW_SECONDS = {"5m": 5 * 60, "24h": 24 * 60 * 60, "48h": 48 * 60 * 60}


def status_snapshot(
    samples: list[SyntheticProbeSample],
    *,
    rollups: list[SyntheticRollup] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    # `now` defaults to wall-clock so production callers don't need to
    # pass it; tests inject a fixed timestamp so daily-rollup bucketing
    # is deterministic regardless of when the test happens to run
    # (a sample created `now - 2h` falls into a different daily bucket
    # depending on whether it's morning vs. late-night UTC).
    if now is None:
        now = utcnow()
    precomputed_rollups = rollups or []
    ordered = sorted(samples, key=lambda sample: sample.created_at, reverse=True)
    current = _current_status(ordered, now=now)
    five_minute = _window_rollup(ordered, now=now, seconds=WINDOW_SECONDS["5m"])
    twenty_four_hour = _window_rollup_with_rollup_backfill(
        ordered,
        precomputed_rollups,
        now=now,
        seconds=WINDOW_SECONDS["24h"],
    )
    forty_eight_hour = _window_rollup_with_rollup_backfill(
        ordered,
        precomputed_rollups,
        now=now,
        seconds=WINDOW_SECONDS["48h"],
    )
    daily = _rollup_history(precomputed_rollups, period="day") or _daily_rollups(ordered)
    monthly = _rollup_history(precomputed_rollups, period="month")
    components = _components(ordered, now=now, rollups=precomputed_rollups)
    overall_status = _aggregate_component_statuses([component["status"] for component in components])
    return {
        "generated_at": iso_now(),
        "overall_status": overall_status,
        "overall_status_label": _status_label(overall_status),
        "overall_status_class": _status_class(overall_status),
        "summary": _summary(overall_status),
        "headline_metrics": _headline_metrics(ordered, now=now),
        "current": current,
        "components": components,
        "recent_events": _recent_events(ordered, now=now),
        "windows": {
            "5m": five_minute,
            "24h": twenty_four_hour,
            "48h": forty_eight_hour,
        },
        "daily": daily,
        "monthly": monthly,
        "samples": [sample.public_dict() for sample in ordered[:100]],
    }


def _headline_metrics(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> dict[str, Any]:
    in_region_latencies = _gateway_latency_values(samples, now=now, in_region=True)
    global_latencies = _gateway_latency_values(samples, now=now)
    canonical_latencies = _gateway_latency_values(samples, now=now, target="canonical")
    primary_latencies = in_region_latencies or global_latencies
    return {
        "gateway_overhead_p50_milliseconds": _percentile(primary_latencies, 50),
        "gateway_overhead_sample_count": len(primary_latencies),
        "gateway_overhead_scope": "in_region" if in_region_latencies else "global",
        "in_region_gateway_overhead_p50_milliseconds": _percentile(in_region_latencies, 50),
        "in_region_gateway_overhead_sample_count": len(in_region_latencies),
        "global_gateway_overhead_p50_milliseconds": _percentile(global_latencies, 50),
        "global_gateway_overhead_sample_count": len(global_latencies),
        "canonical_gateway_overhead_p50_milliseconds": _percentile(canonical_latencies, 50),
        "canonical_gateway_overhead_sample_count": len(canonical_latencies),
        # Human-friendly label for the headline-metric subtitle; the
        # actual rollup window stays at WINDOW_SECONDS["5m"] above.
        "window": "last 5 min",
    }


def _gateway_latency_values(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
    in_region: bool = False,
    target: str | None = None,
) -> list[int]:
    cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["5m"])
    rows = []
    for sample in samples:
        if sample.probe_type != "tls_health" or sample.status != "up":
            continue
        if sample.latency_milliseconds is None or _parse_time(sample.created_at) < cutoff:
            continue
        if target is not None and sample.target != target:
            continue
        if in_region and (
            not sample.target_region or sample.monitor_region != sample.target_region
        ):
            continue
        rows.append(sample.latency_milliseconds)
    return rows


def history_payload(
    samples: list[SyntheticProbeSample],
    window: str,
    *,
    rollups: list[SyntheticRollup] | None = None,
) -> dict[str, Any]:
    snapshot = status_snapshot(samples, rollups=rollups)
    if window == "daily":
        return {"window": "daily", "data": snapshot["daily"]}
    if window == "monthly":
        return {"window": "monthly", "data": snapshot["monthly"]}
    if window in snapshot["windows"]:
        return {"window": window, "data": snapshot["windows"][window]}
    return {"window": window, "data": {}}


def _current_status(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    latest: dict[tuple[str, str, str], SyntheticProbeSample] = {}
    for sample in samples:
        key = (sample.monitor_region, sample.target, sample.probe_type)
        if key not in latest:
            latest[key] = sample
    rows = []
    overall = "unknown"
    for sample in latest.values():
        age = max((now - _parse_time(sample.created_at)).total_seconds(), 0)
        status = "unknown" if age > CURRENT_SAMPLE_TTL_SECONDS else sample.status
        overall = _worse_status(overall, status)
        row = sample.public_dict()
        row["age_seconds"] = int(age)
        row["effective_status"] = status
        row["effective_status_label"] = _status_label(status)
        rows.append(row)
    rows.sort(key=lambda row: (str(row["target"]), str(row["probe_type"]), str(row["monitor_region"])))
    return {
        "overall_status": overall if rows else "unknown",
        "checks": rows,
    }


def _window_rollup(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
    seconds: int,
) -> dict[str, Any]:
    cutoff = now - dt.timedelta(seconds=seconds)
    rows = [sample for sample in samples if _parse_time(sample.created_at) >= cutoff]
    return _rollup(rows)


def _daily_rollups(samples: list[SyntheticProbeSample]) -> list[dict[str, Any]]:
    by_day: dict[str, list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        by_day[sample.created_at[:10]].append(sample)
    return [
        {"date": day, **_rollup(rows)}
        for day, rows in sorted(by_day.items(), reverse=True)
    ]


def _rollup_history(rollups: list[SyntheticRollup], *, period: str) -> list[dict[str, Any]]:
    by_period: dict[str, list[SyntheticRollup]] = defaultdict(list)
    for rollup in rollups:
        if rollup.period == period:
            by_period[rollup.period_start].append(rollup)
    rows: list[dict[str, Any]] = []
    for period_start, period_rollups in sorted(by_period.items(), reverse=True):
        merged = merge_rollups(period_rollups)
        status_counts = _int_dict(merged["status_counts"])
        rows.append(
            {
                "period": period,
                "period_start": period_start,
                "status": _aggregate_status_counts(status_counts),
                "uptime_percent": _uptime_percent_counts(status_counts),
                "sample_count": int(merged["sample_count"]),
                "group_count": len(period_rollups),
                "p50_latency_milliseconds": merged["p50_latency_milliseconds"],
                "p95_latency_milliseconds": merged["p95_latency_milliseconds"],
                "p50_ttfb_milliseconds": merged["p50_ttfb_milliseconds"],
                "p95_ttfb_milliseconds": merged["p95_ttfb_milliseconds"],
                "top_error": merged["top_error"],
                "last_checked_at": merged["last_checked_at"],
                "cost_microdollars": merged["cost_microdollars"],
            }
        )
    return rows


def _rollup_window_from_rollups(
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
    seconds: int,
) -> dict[str, Any] | None:
    rows = _hour_rollups_in_window(rollups, now=now, seconds=seconds)
    if not rows:
        return None
    return _rollup_from_rollups(rows)


def _window_rollup_with_rollup_backfill(
    samples: list[SyntheticProbeSample],
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
    seconds: int,
) -> dict[str, Any]:
    cutoff = now - dt.timedelta(seconds=seconds)
    raw_rows = [sample for sample in samples if _parse_time(sample.created_at) >= cutoff]
    raw_hour_keys = {sample.created_at[:13] for sample in raw_rows}
    backfill_rollups = [
        rollup
        for rollup in _hour_rollups_in_window(rollups, now=now, seconds=seconds)
        if rollup.period_start[:13] not in raw_hour_keys
    ]
    combined_rollups = [
        new_rollup_for_sample(sample, period="hour", component="status_window")
        for sample in raw_rows
    ]
    combined_rollups.extend(backfill_rollups)
    if not combined_rollups:
        return _rollup([])
    return _rollup_from_rollups(combined_rollups)


def _hour_rollups_in_window(
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
    seconds: int,
) -> list[SyntheticRollup]:
    cutoff = now - dt.timedelta(seconds=seconds)
    return [
        rollup
        for rollup in rollups
        if rollup.period == "hour"
        and cutoff <= _parse_time(rollup.period_start) <= now
    ]


def _rollup_from_rollups(rollups: list[SyntheticRollup]) -> dict[str, Any]:
    by_target_probe: dict[tuple[str, str], list[SyntheticRollup]] = defaultdict(list)
    for rollup in rollups:
        by_target_probe[(rollup.target, rollup.probe_type)].append(rollup)

    groups = []
    overall = "unknown"
    total_samples = 0
    for (target, probe_type), rows in sorted(by_target_probe.items()):
        merged = merge_rollups(rows)
        status_counts = _int_dict(merged["status_counts"])
        group_status = _aggregate_status_counts(status_counts)
        overall = _worse_status(overall, group_status)
        total_samples += int(merged["sample_count"])
        groups.append(
            {
                "target": target,
                "probe_type": probe_type,
                "status": group_status,
                "uptime_percent": _uptime_percent_counts(status_counts),
                "sample_count": int(merged["sample_count"]),
                "p50_latency_milliseconds": merged["p50_latency_milliseconds"],
                "p95_latency_milliseconds": merged["p95_latency_milliseconds"],
                "p50_ttfb_milliseconds": merged["p50_ttfb_milliseconds"],
                "p95_ttfb_milliseconds": merged["p95_ttfb_milliseconds"],
                "last_checked_at": merged["last_checked_at"],
            }
        )

    return {
        "overall_status": overall if groups else "unknown",
        "sample_count": total_samples,
        "groups": groups,
    }


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(raw) for key, raw in value.items()}


def _rollup(samples: list[SyntheticProbeSample]) -> dict[str, Any]:
    by_target_probe: dict[tuple[str, str], list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        by_target_probe[(sample.target, sample.probe_type)].append(sample)

    groups = []
    overall = "unknown"
    for (target, probe_type), rows in sorted(by_target_probe.items()):
        statuses = [sample.status for sample in rows]
        group_status = _aggregate_status(statuses)
        overall = _worse_status(overall, group_status)
        latencies = [
            sample.latency_milliseconds
            for sample in rows
            if sample.latency_milliseconds is not None
        ]
        ttfbs = [
            sample.ttfb_milliseconds
            for sample in rows
            if sample.ttfb_milliseconds is not None
        ]
        groups.append(
            {
                "target": target,
                "probe_type": probe_type,
                "status": group_status,
                "uptime_percent": _uptime_percent(statuses),
                "sample_count": len(rows),
                "p50_latency_milliseconds": _percentile(latencies, 50),
                "p95_latency_milliseconds": _percentile(latencies, 95),
                "p50_ttfb_milliseconds": _percentile(ttfbs, 50),
                "p95_ttfb_milliseconds": _percentile(ttfbs, 95),
                "last_checked_at": max(sample.created_at for sample in rows),
            }
        )

    return {
        "overall_status": overall if groups else "unknown",
        "sample_count": len(samples),
        "groups": groups,
    }


def _components(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
    rollups: list[SyntheticRollup] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    precomputed_rollups = rollups or []
    for definition in COMPONENT_DEFINITIONS:
        component_id = str(definition["id"])
        component_samples = [
            sample for sample in samples if component_id in sample_component_ids(sample)
        ]
        component_rollups = [
            rollup for rollup in precomputed_rollups if rollup.component == component_id
        ]
        component_hour_rollups = [
            rollup for rollup in component_rollups if rollup.period == "hour"
        ]
        day_rollups = _hour_rollups_in_window(
            component_rollups, now=now, seconds=WINDOW_SECONDS["24h"]
        )
        day_rollup = merge_rollups(day_rollups) if day_rollups else None
        day_status_counts = _int_dict(day_rollup["status_counts"]) if day_rollup else {}
        day_cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["24h"])
        five_minute_cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["5m"])
        current_samples = _latest_recent_component_samples(component_samples, now=now)
        day_samples = [
            sample for sample in component_samples if _parse_time(sample.created_at) >= day_cutoff
        ]
        five_minute_samples = [
            sample for sample in component_samples if _parse_time(sample.created_at) >= five_minute_cutoff
        ]
        status = _aggregate_status([sample.status for sample in current_samples])
        if not current_samples and component_samples:
            status = "unknown"
        latencies = [
            sample.latency_milliseconds
            for sample in day_samples
            if sample.latency_milliseconds is not None
        ]
        gateway_latencies = [
            sample.latency_milliseconds
            for sample in five_minute_samples
            if sample.probe_type == "tls_health"
            and sample.status == "up"
            and sample.latency_milliseconds is not None
        ]
        in_region_gateway_latencies = [
            sample.latency_milliseconds
            for sample in five_minute_samples
            if sample.probe_type == "tls_health"
            and sample.status == "up"
            and sample.latency_milliseconds is not None
            and sample.target_region
            and sample.monitor_region == sample.target_region
        ]
        primary_gateway_latencies = in_region_gateway_latencies or gateway_latencies
        last_checked_values = [sample.created_at for sample in component_samples]
        last_checked_values.extend(
            rollup.last_checked_at
            for rollup in component_rollups
            if rollup.last_checked_at is not None
        )
        last_checked_at = max(last_checked_values, default=None)
        rows.append(
            {
                **definition,
                "status": status,
                "status_label": _status_label(status),
                "status_class": _status_class(status),
                "uptime_24h_percent": _uptime_percent_counts(day_status_counts)
                if day_rollup
                else (_uptime_percent([sample.status for sample in day_samples]) if day_samples else None),
                "sample_count_24h": int(day_rollup["sample_count"]) if day_rollup else len(day_samples),
                "p50_latency_milliseconds": _percentile(primary_gateway_latencies, 50),
                "p95_latency_milliseconds": _percentile(primary_gateway_latencies, 95),
                "in_region_p50_latency_milliseconds": _percentile(in_region_gateway_latencies, 50),
                "in_region_p95_latency_milliseconds": _percentile(in_region_gateway_latencies, 95),
                "global_p50_latency_milliseconds": _percentile(gateway_latencies, 50),
                "global_p95_latency_milliseconds": _percentile(gateway_latencies, 95),
                "end_to_end_p50_latency_milliseconds": day_rollup["p50_latency_milliseconds"]
                if day_rollup
                else _percentile(latencies, 50),
                "end_to_end_p95_latency_milliseconds": day_rollup["p95_latency_milliseconds"]
                if day_rollup
                else _percentile(latencies, 95),
                "latency_breakdown_5m": _latency_breakdown(five_minute_samples),
                "last_checked_at": last_checked_at,
                "monitor_regions": sorted(
                    {rollup.monitor_region for rollup in day_rollups}
                    if day_rollups
                    else {sample.monitor_region for sample in day_samples}
                ),
                "history": _component_history_from_rollups(
                    component_hour_rollups,
                    now=now,
                    fallback_samples=component_samples,
                )
                if component_hour_rollups
                else _component_history(component_samples, now=now),
            }
        )
    return rows


def _latency_breakdown(samples: list[SyntheticProbeSample]) -> list[dict[str, Any]]:
    by_probe: dict[str, list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        by_probe[sample.probe_type].append(sample)
    rows: list[dict[str, Any]] = []
    for probe_type, probe_samples in sorted(by_probe.items()):
        latencies = [
            sample.latency_milliseconds
            for sample in probe_samples
            if sample.latency_milliseconds is not None
        ]
        ttfbs = [
            sample.ttfb_milliseconds
            for sample in probe_samples
            if sample.ttfb_milliseconds is not None
        ]
        statuses = [sample.status for sample in probe_samples]
        rows.append(
            {
                "probe_type": probe_type,
                "status": _aggregate_status(statuses),
                "uptime_percent": _uptime_percent(statuses),
                "sample_count": len(probe_samples),
                "p50_latency_milliseconds": _percentile(latencies, 50),
                "p95_latency_milliseconds": _percentile(latencies, 95),
                "p50_ttfb_milliseconds": _percentile(ttfbs, 50),
                "p95_ttfb_milliseconds": _percentile(ttfbs, 95),
            }
        )
    return rows


def _latest_recent_component_samples(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
) -> list[SyntheticProbeSample]:
    latest: dict[tuple[str, str, str], SyntheticProbeSample] = {}
    for sample in samples:
        key = (sample.monitor_region, sample.target, sample.probe_type)
        if key not in latest:
            latest[key] = sample
    cutoff = now - dt.timedelta(seconds=CURRENT_SAMPLE_TTL_SECONDS)
    return [
        sample
        for sample in latest.values()
        if _parse_time(sample.created_at) >= cutoff
    ]


def _component_history(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> list[dict[str, Any]]:
    """Build hourly bars for the past `STATUS_HISTORY_HOURS` hours.

    Per-bucket status uses an uptime-percent threshold rather than a
    raw "≥2 down samples" rule — single-sample blips at the edge of the
    timeout window shouldn't paint an hour red when actual uptime is
    99.95%. Each row carries enough context (uptime %, sample count,
    p50, top error type, distinct probes) for the template's hover
    tooltip to mirror status.anthropic.com / status.github.com style
    bar tooltips."""
    by_hour: dict[str, list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        bucket_key = sample.created_at[:13]  # YYYY-MM-DDTHH
        by_hour[bucket_key].append(sample)

    base = now.replace(minute=0, second=0, microsecond=0)
    hour_keys = [
        (base - dt.timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        for offset in reversed(range(STATUS_HISTORY_HOURS))
    ]

    history: list[dict[str, Any]] = []
    for hour_key in hour_keys:
        rows = by_hour.get(hour_key, [])
        bucket_start = dt.datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=dt.UTC)
        if not rows:
            history.append(
                {
                    "bucket_start": bucket_start.isoformat(),
                    "status": "unknown",
                    "status_label": "No data",
                    "status_class": "unknown",
                    "uptime_percent": None,
                    "sample_count": 0,
                    "p50_latency_milliseconds": None,
                    "top_error": None,
                    "title": _history_title_hourly(bucket_start, "unknown", None, 0, None),
                }
            )
            continue

        statuses = [sample.status for sample in rows]
        uptime = _uptime_percent(statuses)
        status = _history_status(uptime, has_trust_degraded=any(s == "trust_degraded" for s in statuses))

        latencies = [
            sample.latency_milliseconds
            for sample in rows
            if sample.latency_milliseconds is not None
        ]
        error_types = [sample.error_type for sample in rows if sample.error_type]
        top_error: str | None = None
        if error_types:
            counts: dict[str, int] = {}
            for et in error_types:
                counts[et] = counts.get(et, 0) + 1
            top_error = max(counts, key=lambda key: counts[key])

        history.append(
            {
                "bucket_start": bucket_start.isoformat(),
                "status": status,
                "status_label": _status_label(status),
                "status_class": _status_class(status),
                "uptime_percent": uptime,
                "sample_count": len(rows),
                "p50_latency_milliseconds": _percentile(latencies, 50),
                "top_error": top_error,
                "title": _history_title_hourly(bucket_start, status, uptime, len(rows), top_error),
            }
        )
    return history


def _component_history_from_rollups(
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
    fallback_samples: list[SyntheticProbeSample] | None = None,
) -> list[dict[str, Any]]:
    by_hour: dict[str, list[SyntheticRollup]] = defaultdict(list)
    for rollup in rollups:
        if rollup.period != "hour":
            continue
        by_hour[rollup.period_start[:13]].append(rollup)
    fallback_by_hour: dict[str, list[SyntheticProbeSample]] = defaultdict(list)
    for sample in fallback_samples or []:
        fallback_by_hour[sample.created_at[:13]].append(sample)

    base = now.replace(minute=0, second=0, microsecond=0)
    hour_keys = [
        (base - dt.timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        for offset in reversed(range(STATUS_HISTORY_HOURS))
    ]

    history: list[dict[str, Any]] = []
    for hour_key in hour_keys:
        rows = by_hour.get(hour_key, [])
        bucket_start = dt.datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=dt.UTC)
        if not rows:
            fallback_rows = fallback_by_hour.get(hour_key, [])
            if fallback_rows:
                history.append(_sample_history_bucket(bucket_start, fallback_rows))
                continue
            history.append(
                {
                    "bucket_start": bucket_start.isoformat(),
                    "status": "unknown",
                    "status_label": "No data",
                    "status_class": "unknown",
                    "uptime_percent": None,
                    "sample_count": 0,
                    "p50_latency_milliseconds": None,
                    "top_error": None,
                    "title": _history_title_hourly(bucket_start, "unknown", None, 0, None),
                }
            )
            continue

        history.append(_rollup_history_bucket(bucket_start, rows))
    return history


def _sample_history_bucket(
    bucket_start: dt.datetime,
    rows: list[SyntheticProbeSample],
) -> dict[str, Any]:
    statuses = [sample.status for sample in rows]
    uptime = _uptime_percent(statuses)
    status = _history_status(uptime, has_trust_degraded=any(s == "trust_degraded" for s in statuses))

    latencies = [
        sample.latency_milliseconds
        for sample in rows
        if sample.latency_milliseconds is not None
    ]
    error_types = [sample.error_type for sample in rows if sample.error_type]
    top_error: str | None = None
    if error_types:
        counts: dict[str, int] = {}
        for error_type in error_types:
            counts[error_type] = counts.get(error_type, 0) + 1
        top_error = max(counts, key=lambda key: counts[key])

    return {
        "bucket_start": bucket_start.isoformat(),
        "status": status,
        "status_label": _status_label(status),
        "status_class": _status_class(status),
        "uptime_percent": uptime,
        "sample_count": len(rows),
        "p50_latency_milliseconds": _percentile(latencies, 50),
        "top_error": top_error,
        "title": _history_title_hourly(bucket_start, status, uptime, len(rows), top_error),
    }


def _rollup_history_bucket(
    bucket_start: dt.datetime,
    rows: list[SyntheticRollup],
) -> dict[str, Any]:
    merged = merge_rollups(rows)
    status_counts = _int_dict(merged["status_counts"])
    uptime = _uptime_percent_counts(status_counts)
    status = _history_status(
        uptime,
        has_trust_degraded=status_counts.get("trust_degraded", 0) > 0,
    )
    sample_count = int(merged["sample_count"])
    top_error = merged["top_error"]
    return {
        "bucket_start": bucket_start.isoformat(),
        "status": status,
        "status_label": _status_label(status),
        "status_class": _status_class(status),
        "uptime_percent": uptime,
        "sample_count": sample_count,
        "p50_latency_milliseconds": merged["p50_latency_milliseconds"],
        "top_error": top_error,
        "title": _history_title_hourly(bucket_start, status, uptime, sample_count, top_error),
    }


def _history_status(uptime: float, *, has_trust_degraded: bool) -> str:
    # Threshold-based, not "≥2 down" — see module-level constants.
    if uptime >= STATUS_HISTORY_UP_MIN_UPTIME:
        return "up"
    if uptime >= STATUS_HISTORY_DEGRADED_MIN_UPTIME:
        return "degraded"
    # Distinguish trust failures from generic outages — the attestation
    # probe maps to `trust_degraded`, which we avoid flattening to "down".
    return "trust_degraded" if has_trust_degraded else "down"


def _recent_events(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["24h"])
    events = []
    for sample in samples:
        if _parse_time(sample.created_at) < cutoff or sample.status == "up":
            continue
        component_names = [
            str(definition["name"])
            for definition in COMPONENT_DEFINITIONS
            if str(definition["id"]) in sample_component_ids(sample)
        ]
        events.append(
            {
                "id": sample.id,
                "component": component_names[0] if component_names else sample.target,
                "status": sample.status,
                "status_label": _status_label(sample.status),
                "status_class": _status_class(sample.status),
                "probe_type": sample.probe_type,
                "target": sample.target,
                "monitor_region": sample.monitor_region,
                "created_at": sample.created_at,
                "latency_milliseconds": sample.latency_milliseconds,
                "error_type": sample.error_type,
            }
        )
    return events[:8]


def _aggregate_component_statuses(statuses: list[str]) -> str:
    known = [status for status in statuses if status != "unknown"]
    if not known:
        return "unknown"
    overall = known[0]
    for status in known[1:]:
        overall = _worse_status(overall, status)
    return overall


def _aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    counts = {status: statuses.count(status) for status in set(statuses)}
    return _aggregate_status_counts(counts)


def _aggregate_status_counts(counts: dict[str, int]) -> str:
    if counts.get("down", 0) >= 2:
        return "down"
    if counts.get("trust_degraded", 0) > 0:
        return "trust_degraded"
    if counts.get("routing_degraded", 0) > 0:
        return "routing_degraded"
    if counts.get("degraded", 0) > 0:
        return "degraded"
    if counts.get("up", 0) > 0:
        return "up"
    return "unknown"


def _worse_status(left: str, right: str) -> str:
    if left == "unknown":
        return right
    if right == "unknown":
        return left
    return left if STATUS_ORDER.get(left, 4) >= STATUS_ORDER.get(right, 4) else right


def _uptime_percent(statuses: list[str]) -> float:
    if not statuses:
        return 0.0
    counts = {status: statuses.count(status) for status in set(statuses)}
    return _uptime_percent_counts(counts)


def _uptime_percent_counts(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return round((counts.get("up", 0) / total) * 100.0, 4)


def _status_label(status: str) -> str:
    return {
        "up": "Operational",
        "degraded": "Degraded",
        "routing_degraded": "Routing degraded",
        "trust_degraded": "Trust degraded",
        "down": "Major outage",
        "unknown": "Unknown",
    }.get(status, status.replace("_", " ").title())


def _status_class(status: str) -> str:
    return status.replace("_", "-")


def _summary(status: str) -> dict[str, str]:
    if status == "up":
        return {
            "headline": "All Systems Operational",
            "detail": "Synthetic checks are passing for the public API, attestation, billing, and provider fallback.",
        }
    if status == "down":
        return {
            "headline": "Major System Outage",
            "detail": "One or more critical synthetic checks are failing.",
        }
    if status == "trust_degraded":
        return {
            "headline": "Trust Verification Degraded",
            "detail": "Inference may still work, but an attestation check is failing and should be treated as critical.",
        }
    if status in {"degraded", "routing_degraded"}:
        return {
            "headline": "Degraded Performance",
            "detail": "One or more routing, provider, or regional checks are degraded.",
        }
    return {
        "headline": "Status Unknown",
        "detail": "Synthetic checks have not reported enough recent data yet.",
    }


def _history_title_hourly(
    bucket_start: dt.datetime,
    status: str,
    uptime: float | None,
    sample_count: int,
    top_error: str | None,
) -> str:
    """Bare `title` attribute fallback. Renders even when JS is off /
    on touch devices that ignore custom hover popups. The richer
    formatted tooltip is built in the template from the same data."""
    label = bucket_start.strftime("%Y-%m-%d %H:00 UTC")
    if uptime is None:
        return f"{label} — no data"
    parts = [f"{label}", f"{_status_label(status)}", f"{uptime:.2f}% uptime", f"{sample_count} samples"]
    if top_error:
        parts.append(f"top error: {top_error}")
    return " · ".join(parts)


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((percentile / 100) * len(ordered)) - 1))
    return ordered[index]


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed
