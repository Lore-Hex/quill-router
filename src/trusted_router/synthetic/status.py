from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, iso_now, utcnow

CURRENT_SAMPLE_TTL_SECONDS = 5 * 60
STATUS_HISTORY_DAYS = 90
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
    by_day: dict[str, list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        by_day[sample.created_at[:10]].append(sample)

    days = [
        (now.date() - dt.timedelta(days=offset)).isoformat()
        for offset in reversed(range(STATUS_HISTORY_DAYS))
    ]
    history = []
    for day in days:
        rows = by_day.get(day, [])
        status = _aggregate_status([sample.status for sample in rows]) if rows else "unknown"
        uptime = _uptime_percent([sample.status for sample in rows]) if rows else None
        history.append(
            {
                "date": day,
                "status": status,
                "status_label": _status_label(status),
                "status_class": _status_class(status),
                "uptime_percent": uptime,
                "sample_count": len(rows),
                "title": _history_title(day, status, uptime),
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


def _history_title(day: str, status: str, uptime: float | None) -> str:
    if uptime is None:
        return f"{day}: no data"
    return f"{day}: {_status_label(status)} ({uptime:.2f}% uptime)"


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
