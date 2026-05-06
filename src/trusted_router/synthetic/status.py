from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, iso_now, utcnow

CURRENT_SAMPLE_TTL_SECONDS = 5 * 60
# Last 24 hourly bars. We just started running synthetic monitoring;
# nothing further back has data, and dragging out 90 days of "no data"
# bars makes the page look broken. As the service ages we can extend
# this — for now keep the strip honest.
STATUS_HISTORY_HOURS = 24
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
WINDOW_SECONDS = {"5m": 5 * 60, "24h": 24 * 60 * 60}
API_PROBES = {"tls_health", "attestation_nonce", "openai_sdk_pong", "responses_pong"}
COMPONENT_DEFINITIONS = (
    {
        "id": "canonical_api",
        "name": "Canonical API",
        "description": "api.quillrouter.com chat, Responses, TLS, and attestation checks.",
    },
    {
        "id": "eu_regional_api",
        "name": "EU Regional API",
        "description": "api-europe-west4.quillrouter.com regional attested gateway checks.",
    },
    {
        "id": "attestation",
        "name": "Attestation",
        "description": "Nonce and digest verification for public attested gateways.",
    },
    {
        "id": "billing_settlement",
        "name": "Billing and Settlement",
        "description": "Authorize, settle, and accounting path used by the gateway.",
    },
    {
        "id": "provider_fallback",
        "name": "Provider Fallback",
        "description": "Fail-first route selection and rollover to the next healthy provider.",
    },
)


def status_snapshot(samples: list[SyntheticProbeSample]) -> dict[str, Any]:
    now = utcnow()
    ordered = sorted(samples, key=lambda sample: sample.created_at, reverse=True)
    current = _current_status(ordered, now=now)
    five_minute = _window_rollup(ordered, now=now, seconds=WINDOW_SECONDS["5m"])
    twenty_four_hour = _window_rollup(ordered, now=now, seconds=WINDOW_SECONDS["24h"])
    daily = _daily_rollups(ordered)
    components = _components(ordered, now=now)
    overall_status = _aggregate_component_statuses([component["status"] for component in components])
    return {
        "generated_at": iso_now(),
        "overall_status": overall_status,
        "overall_status_label": _status_label(overall_status),
        "overall_status_class": _status_class(overall_status),
        "summary": _summary(overall_status),
        "current": current,
        "components": components,
        "recent_events": _recent_events(ordered, now=now),
        "windows": {
            "5m": five_minute,
            "24h": twenty_four_hour,
        },
        "daily": daily,
        "samples": [sample.public_dict() for sample in ordered[:100]],
    }


def history_payload(samples: list[SyntheticProbeSample], window: str) -> dict[str, Any]:
    snapshot = status_snapshot(samples)
    if window == "daily":
        return {"window": "daily", "data": snapshot["daily"]}
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
        groups.append(
            {
                "target": target,
                "probe_type": probe_type,
                "status": group_status,
                "uptime_percent": _uptime_percent(statuses),
                "sample_count": len(rows),
                "p50_latency_milliseconds": _percentile(latencies, 50),
                "p95_latency_milliseconds": _percentile(latencies, 95),
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
) -> list[dict[str, Any]]:
    rows = []
    for definition in COMPONENT_DEFINITIONS:
        component_id = str(definition["id"])
        component_samples = [
            sample for sample in samples if component_id in _sample_component_ids(sample)
        ]
        day_cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["24h"])
        current_samples = _latest_recent_component_samples(component_samples, now=now)
        day_samples = [
            sample for sample in component_samples if _parse_time(sample.created_at) >= day_cutoff
        ]
        status = _aggregate_status([sample.status for sample in current_samples])
        if not current_samples and component_samples:
            status = "unknown"
        latencies = [
            sample.latency_milliseconds
            for sample in day_samples
            if sample.latency_milliseconds is not None
        ]
        last_checked_at = max((sample.created_at for sample in component_samples), default=None)
        rows.append(
            {
                **definition,
                "status": status,
                "status_label": _status_label(status),
                "status_class": _status_class(status),
                "uptime_24h_percent": _uptime_percent([sample.status for sample in day_samples])
                if day_samples
                else None,
                "sample_count_24h": len(day_samples),
                "p50_latency_milliseconds": _percentile(latencies, 50),
                "p95_latency_milliseconds": _percentile(latencies, 95),
                "last_checked_at": last_checked_at,
                "monitor_regions": sorted({sample.monitor_region for sample in day_samples}),
                "history": _component_history(component_samples, now=now),
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


def _sample_component_ids(sample: SyntheticProbeSample) -> list[str]:
    ids = []
    if sample.target == "canonical" and sample.probe_type in API_PROBES:
        ids.append("canonical_api")
    if sample.target == "europe-west4" and sample.probe_type in API_PROBES:
        ids.append("eu_regional_api")
    if sample.probe_type == "attestation_nonce":
        ids.append("attestation")
    if sample.target == "control-plane" and sample.probe_type == "gateway_authorize_settle":
        ids.append("billing_settlement")
    if sample.target == "control-plane" and sample.probe_type == "provider_fallback":
        ids.append("provider_fallback")
    return ids


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

    history = []
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
        # Threshold-based, not "≥2 down" — see module-level constants.
        if uptime >= STATUS_HISTORY_UP_MIN_UPTIME:
            status = "up"
        elif uptime >= STATUS_HISTORY_DEGRADED_MIN_UPTIME:
            status = "degraded"
        else:
            # Distinguish trust failures from generic outages — the
            # attestation probe maps to `trust_degraded`, which we'd
            # rather not flatten to "down" in the bar coloring.
            status = "trust_degraded" if any(s == "trust_degraded" for s in statuses) else "down"

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


def _recent_events(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["24h"])
    events = []
    for sample in samples:
        if _parse_time(sample.created_at) < cutoff or sample.status == "up":
            continue
        component_names = [
            str(definition["name"])
            for definition in COMPONENT_DEFINITIONS
            if str(definition["id"]) in _sample_component_ids(sample)
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
    up = sum(1 for status in statuses if status == "up")
    return round((up / len(statuses)) * 100.0, 4)


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
