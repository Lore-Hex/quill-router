from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, iso_now, utcnow

STATUS_ORDER = {
    "up": 0,
    "degraded": 1,
    "routing_degraded": 1,
    "trust_degraded": 2,
    "down": 3,
    "unknown": 4,
}
WINDOW_SECONDS = {"5m": 5 * 60, "24h": 24 * 60 * 60}


def status_snapshot(samples: list[SyntheticProbeSample]) -> dict[str, Any]:
    now = utcnow()
    ordered = sorted(samples, key=lambda sample: sample.created_at, reverse=True)
    current = _current_status(ordered, now=now)
    five_minute = _window_rollup(ordered, now=now, seconds=WINDOW_SECONDS["5m"])
    twenty_four_hour = _window_rollup(ordered, now=now, seconds=WINDOW_SECONDS["24h"])
    daily = _daily_rollups(ordered)
    return {
        "generated_at": iso_now(),
        "overall_status": current["overall_status"],
        "current": current,
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
    latest: dict[tuple[str, str], SyntheticProbeSample] = {}
    for sample in samples:
        key = (sample.target, sample.probe_type)
        if key not in latest:
            latest[key] = sample
    rows = []
    overall = "unknown"
    for sample in latest.values():
        age = max((now - _parse_time(sample.created_at)).total_seconds(), 0)
        status = "unknown" if age > 180 else sample.status
        overall = _worse_status(overall, status)
        row = sample.public_dict()
        row["age_seconds"] = int(age)
        row["effective_status"] = status
        rows.append(row)
    rows.sort(key=lambda row: (str(row["target"]), str(row["probe_type"])))
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
