from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import replace
from typing import Any

from trusted_router.config import Settings, get_settings
from trusted_router.storage_models import (
    FUTURE_SAMPLE_SKEW_SECONDS,
    SyntheticProbeSample,
    SyntheticRollup,
    iso_now,
    utcnow,
)
from trusted_router.synthetic.components import (
    COMPONENT_DEFINITIONS,
    COMPONENT_PROBE_TARGETS,
    GATEWAY_REGION_TARGET_NAMES,
    REGIONAL_GATEWAY_PROBES,
    SLO_DEFINITIONS,
    UNCATEGORIZED_COMPONENT,
    applicable_component_definitions,
    component_name,
    component_probe_types,
    published_gateway_region_components,
    published_machine_region_components,
    rollup_slo_class_ids,
    sample_component_ids,
    sample_slo_class_ids,
)
from trusted_router.synthetic.rollups import merge_rollups, new_rollup_for_sample

CURRENT_SAMPLE_TTL_SECONDS = 5 * 60
# Regional monitor jobs run every three minutes so normal Cloud Run startup
# latency remains inside this five-minute freshness contract. A sample that
# crosses the contract is degraded; only two missed freshness windows plus a
# small scheduling allowance are a silent-probe failure.
SILENT_PROBE_TTL_SECONDS = (2 * CURRENT_SAMPLE_TTL_SECONDS) + 60
IMAGE_GENERATION_SAMPLE_TTL_SECONDS = 7 * 60 * 60
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
WINDOW_SECONDS = {
    "5m": 5 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "48h": 48 * 60 * 60,
}
ROUTER_CORE_SLO_ID = "router_core"
SLO_TARGET_UPTIME_PERCENT = 99.99
SLO_ERROR_BUDGET_FRACTION = 1.0 - (SLO_TARGET_UPTIME_PERCENT / 100.0)
SLO_BURN_WINDOWS = ("5m", "1h", "6h", "24h")


def status_snapshot(
    samples: list[SyntheticProbeSample],
    *,
    rollups: list[SyntheticRollup] | None = None,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    # `now` defaults to wall-clock so production callers don't need to
    # pass it; tests inject a fixed timestamp so daily-rollup bucketing
    # is deterministic regardless of when the test happens to run
    # (a sample created `now - 2h` falls into a different daily bucket
    # depending on whether it's morning vs. late-night UTC).
    if now is None:
        now = utcnow()
    # `settings` scopes which components this deployment publishes at all.
    # Defaulting to the running deployment's own configuration is the point:
    # a caller that forgets to pass it must not silently fall back to
    # advertising another cloud's regions.
    if settings is None:
        settings = get_settings()
    precomputed_rollups = rollups or []
    ordered = sorted(samples, key=lambda sample: sample.created_at, reverse=True)
    freshness = _monitor_freshness(ordered, now=now)
    router_core_samples = [
        sample for sample in ordered if ROUTER_CORE_SLO_ID in sample_slo_class_ids(sample)
    ]
    gateway_region_components = published_gateway_region_components(settings)
    # `current.checks` is the MACHINE-readable surface, not a second copy of
    # the components table: quill-cloud-proxy's watchdog decides per-region
    # rollback from checks[].target_region, and its deploy gate waits on the
    # same array. Restricting it to router_core (canonical-only) meant a
    # region pinned probe could be flat down and no automation would ever see
    # an eu-west-1 row at all — the gate would sit at "waiting" forever
    # instead of "down", and nobody would be paged. The SLO windows below
    # stay canonical-only on purpose: they measure the address customers
    # resolve, and mixing in diagnostic per-region probes would triple the
    # denominator of a published SLO.
    current = _current_status(
        router_core_samples + _machine_region_samples(ordered, settings=settings),
        now=now,
    )
    router_core_rollups = [
        rollup
        for rollup in precomputed_rollups
        if ROUTER_CORE_SLO_ID in rollup_slo_class_ids(rollup)
    ]
    five_minute = _scoped_window(
        router_core_samples,
        router_core_rollups,
        now=now,
        seconds=WINDOW_SECONDS["5m"],
    )
    twenty_four_hour = _scoped_window(
        router_core_samples,
        router_core_rollups,
        now=now,
        seconds=WINDOW_SECONDS["24h"],
    )
    forty_eight_hour = _scoped_window(
        router_core_samples,
        router_core_rollups,
        now=now,
        seconds=WINDOW_SECONDS["48h"],
    )
    daily = _rollup_history(router_core_rollups, period="day") or _daily_rollups(
        router_core_samples
    )
    monthly = _monthly_history(router_core_rollups)
    components = _components(ordered, now=now, rollups=precomputed_rollups, settings=settings)
    slo_classes = _slo_classes(ordered, precomputed_rollups, now=now)
    slo_history = {
        str(definition["id"]): _slo_long_term_history(
            ordered,
            precomputed_rollups,
            slo_id=str(definition["id"]),
        )
        for definition in SLO_DEFINITIONS
    }
    router_core_status = str(slo_classes.get(ROUTER_CORE_SLO_ID, {}).get("status") or "unknown")
    # A dead region behind an anycast record must not read "All Systems
    # Operational". router_core measures the hostname customers resolve,
    # which Global Accelerator keeps answering from the SURVIVING region, so
    # the pinned per-region rows are the only evidence that half the fleet is
    # gone. Without this the banner contradicted the table directly beneath
    # it. `_worse_status` treats "unknown" as no-opinion and the tuple is
    # empty on every deployment that configures no pinned endpoints, so this
    # is a byte-identical no-op on GCP.
    overall_status = _worse_status(
        router_core_status,
        _aggregate_component_statuses(
            [
                str(row["status"])
                for row in components
                if str(row["id"]) in gateway_region_components
            ]
        ),
    )
    # Model Inference also pulls the banner, but only as far as "degraded":
    # on 2026-08-10 every pong probe on two deployments failed 100% while the
    # banner read "All Systems Operational", because pong samples fed no
    # component and no SLO. A dead model path must not render green — and it
    # must not render "Router Core Outage" either, because router_core is
    # passing; "Partial Outage: Model Inference" is what is actually true.
    # The SLO math is untouched (pong failures still never burn router_core).
    model_inference_down = any(
        str(row["id"]) == "model_inference" and str(row["status"]) == "down" for row in components
    )
    if model_inference_down:
        overall_status = _worse_status(overall_status, "degraded")
    down_component_names = [
        str(row["name"])
        for row in components
        if str(row["status"]) == "down"
        and (str(row["id"]) in gateway_region_components or str(row["id"]) == "model_inference")
    ]
    return {
        "generated_at": iso_now(),
        "overall_status": overall_status,
        "overall_status_label": _status_label(overall_status),
        "overall_status_class": _status_class(overall_status),
        "summary": _summary(
            overall_status,
            freshness=freshness,
            down_components=down_component_names,
        ),
        "monitor_freshness": freshness,
        "headline_metrics": _headline_metrics(ordered, now=now),
        "current": current,
        "slo_classes": slo_classes,
        "slo_history": slo_history,
        "burn_rate_alerts": _burn_rate_alerts(slo_classes),
        "components": components,
        "recent_events": _recent_events(
            ordered,
            rollups=precomputed_rollups,
            now=now,
        ),
        "history_scope": ROUTER_CORE_SLO_ID,
        "windows": {
            "5m": five_minute,
            "24h": twenty_four_hour,
            "48h": forty_eight_hour,
        },
        "daily": daily,
        "monthly": monthly,
        "samples": [sample.public_dict() for sample in ordered[:100]],
    }


def _monitor_freshness(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    # Future-dated samples are NOT evidence of freshness. This is not a
    # theoretical case: conformance fixtures dated in the year 7748 were
    # once written to a live store, and the previous implementation —
    # `max(age, 0)` over `max(samples, key=created_at)` — locked
    # latest_sample_age_seconds to 0 and is_stale to False permanently.
    # A staleness detector a single poison row can disable forever is
    # worse than none: it reports "fresh" through a total monitor outage.
    # Small negative ages are ordinary clock skew between the monitor and
    # this host, so only samples beyond the skew budget are excluded.
    dateable = [
        sample
        for sample in samples
        if (now - _parse_time(sample.created_at)).total_seconds() >= -FUTURE_SAMPLE_SKEW_SECONDS
    ]
    future_dated = len(samples) - len(dateable)
    if not dateable:
        return {
            "latest_sample_at": None,
            "latest_sample_age_seconds": None,
            "stale_after_seconds": CURRENT_SAMPLE_TTL_SECONDS,
            "is_stale": True,
            "future_dated_samples": future_dated,
        }
    latest = max(dateable, key=lambda sample: _parse_time(sample.created_at))
    age = (now - _parse_time(latest.created_at)).total_seconds()
    return {
        "latest_sample_at": latest.created_at,
        "latest_sample_age_seconds": int(max(age, 0)),
        "stale_after_seconds": CURRENT_SAMPLE_TTL_SECONDS,
        "is_stale": age > CURRENT_SAMPLE_TTL_SECONDS,
        "future_dated_samples": future_dated,
    }


def _headline_metrics(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> dict[str, Any]:
    in_region_latencies = _gateway_latency_values(samples, now=now, in_region=True)
    global_latencies = _gateway_latency_values(samples, now=now)
    canonical_latencies = _gateway_latency_values(samples, now=now, target="canonical")
    primary_latencies = in_region_latencies or global_latencies
    cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["5m"])
    phase_samples = [
        sample
        for sample in samples
        if sample.probe_type in {"gateway_cold_path", "gateway_reused_path"}
        and sample.status == "up"
        and _parse_time(sample.created_at) >= cutoff
    ]
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
        "latency_anatomy": _sample_group_breakdown(phase_samples),
        # Human-friendly label for the headline-metric subtitle; the
        # actual rollup window stays at WINDOW_SECONDS["5m"] above.
        "window": "last 5 min",
    }


def _machine_region_samples(
    samples: list[SyntheticProbeSample],
    *,
    settings: Settings,
) -> list[SyntheticProbeSample]:
    """Regional gateway samples consumed by deploy and watchdog automation."""
    published = {
        COMPONENT_PROBE_TARGETS[component_id]
        for component_id in published_machine_region_components(settings)
    }
    if not published:
        return []
    return [
        sample
        for sample in samples
        if sample.target in published and sample.probe_type in REGIONAL_GATEWAY_PROBES
    ]


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
        # The headline "gateway overhead" numbers describe the path customers
        # take. A pinned per-region probe deliberately BYPASSES Global
        # Accelerator by dialling one load balancer directly, so its latency
        # describes a path nobody is served on: pooling it dropped the
        # published in-region p50 from ~30 ms to ~12 ms on deploy with no
        # change whatsoever in what customers experience. Asking for that
        # target by name still returns it — this only excludes it from the
        # unscoped in-region/global aggregates.
        if target is None and sample.target in GATEWAY_REGION_TARGET_NAMES:
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
    precomputed_rollups = rollups or []
    snapshot = status_snapshot(samples, rollups=precomputed_rollups)
    if window == "daily":
        return {
            "window": "daily",
            "scope": ROUTER_CORE_SLO_ID,
            "data": snapshot["daily"],
        }
    if window == "monthly":
        return {
            "window": "monthly",
            "scope": ROUTER_CORE_SLO_ID,
            "data": snapshot["monthly"],
        }
    if window in snapshot["windows"]:
        return {
            "window": window,
            "scope": ROUTER_CORE_SLO_ID,
            "data": snapshot["windows"][window],
        }
    return {"window": window, "scope": ROUTER_CORE_SLO_ID, "data": {}}


def _monitor_is_reporting(samples: list[SyntheticProbeSample], *, now: dt.datetime) -> bool:
    """Is the monitor alive at all — i.e. did ANY probe report recently?

    Distinguishes "the whole monitor stopped" (reported once, by
    monitor_freshness) from "one probe stopped while the rest kept
    reporting" (a real, probe-specific outage that must turn something
    red). Without the distinction you must choose between missing the
    second case and painting the page red on every cold start.
    """
    return any(
        -FUTURE_SAMPLE_SKEW_SECONDS
        <= (now - _parse_time(sample.created_at)).total_seconds()
        <= CURRENT_SAMPLE_TTL_SECONDS
        for sample in samples
    )


def _sample_effective_status(
    sample: SyntheticProbeSample,
    *,
    now: dt.datetime,
    monitor_reporting: bool = False,
) -> tuple[str, float]:
    """(effective status, signed age).

    Too far in the FUTURE is always "unknown": a future-dated `up` sample
    must never pin a component green — that is poison or clock breakage,
    not evidence.

    Too OLD depends on whether the monitor is otherwise alive:

      * monitor_reporting=True and past the freshness contract -> "degraded".
        Regional jobs run every three minutes, leaving room for Cloud Run
        startup latency before this five-minute boundary.

      * monitor_reporting=True and two cadences late -> "down". This probe
        stopped emitting while its siblings kept going. That is the
        silent-disappearance outage: previously it dropped to "unknown",
        and _worse_status treats unknown as no-opinion, so a probe that
        vanished entirely left the SLO green.

      * monitor_reporting=False -> "unknown". Every probe is stale, so
        the monitor itself is down or cold-starting. monitor_freshness
        reports that as is_stale; marking each probe `down` too would
        paint a false outage on every deploy.
    """
    age = (now - _parse_time(sample.created_at)).total_seconds()
    if age < -FUTURE_SAMPLE_SKEW_SECONDS:
        return "unknown", age
    if age > SILENT_PROBE_TTL_SECONDS:
        return ("down" if monitor_reporting else "unknown"), age
    if age > CURRENT_SAMPLE_TTL_SECONDS:
        return ("degraded" if monitor_reporting else "unknown"), age
    return sample.status, age


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
    monitor_reporting = _monitor_is_reporting(samples, now=now)
    for sample in latest.values():
        status, signed_age = _sample_effective_status(
            sample, now=now, monitor_reporting=monitor_reporting
        )
        age = max(signed_age, 0)
        overall = _worse_status(overall, status)
        row = sample.public_dict()
        row["age_seconds"] = int(age)
        row["effective_status"] = status
        row["effective_status_label"] = _status_label(status)
        rows.append(row)
    rows.sort(
        key=lambda row: (str(row["target"]), str(row["probe_type"]), str(row["monitor_region"]))
    )
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
        {
            "date": day,
            **_rollup(rows),
            "groups": _sample_group_breakdown(rows, include_component=True),
        }
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
                "groups": _rollup_group_breakdown(period_rollups, include_component=True),
            }
        )
    return rows


def _monthly_history(rollups: list[SyntheticRollup]) -> list[dict[str, Any]]:
    month_rollups = [rollup for rollup in rollups if rollup.period == "month"]
    if month_rollups:
        return _rollup_history(month_rollups, period="month")
    day_rollups = [rollup for rollup in rollups if rollup.period == "day"]
    if not day_rollups:
        return []
    return _rollup_history(
        [
            replace(
                rollup,
                period="month",
                period_start=f"{rollup.period_start[:7]}-01T00:00:00Z",
            )
            for rollup in day_rollups
        ],
        period="month",
    )


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


def _scoped_window(
    samples: list[SyntheticProbeSample],
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
    seconds: int,
) -> dict[str, Any]:
    detail = _window_rollup_with_rollup_backfill(
        samples,
        rollups,
        now=now,
        seconds=seconds,
    )
    metrics = _slo_window(samples, rollups, now=now, seconds=seconds)
    return {**detail, **metrics}


def _slo_long_term_history(
    samples: list[SyntheticProbeSample],
    rollups: list[SyntheticRollup],
    *,
    slo_id: str,
) -> dict[str, list[dict[str, Any]]]:
    scoped_samples = [sample for sample in samples if slo_id in sample_slo_class_ids(sample)]
    scoped_rollups = [rollup for rollup in rollups if slo_id in rollup_slo_class_ids(rollup)]
    return {
        "daily": _rollup_history(scoped_rollups, period="day") or _daily_rollups(scoped_samples),
        "monthly": _monthly_history(scoped_rollups),
    }


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
        if rollup.period == "hour" and cutoff <= _parse_time(rollup.period_start) <= now
    ]


def _rollup_from_rollups(rollups: list[SyntheticRollup]) -> dict[str, Any]:
    groups = _rollup_group_breakdown(rollups)
    overall = "unknown"
    for group in groups:
        overall = _worse_status(overall, str(group["status"]))
    return {
        "overall_status": overall if groups else "unknown",
        "sample_count": sum(int(group["sample_count"]) for group in groups),
        "groups": groups,
    }


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(raw) for key, raw in value.items()}


def _rollup(samples: list[SyntheticProbeSample]) -> dict[str, Any]:
    groups = _sample_group_breakdown(samples)
    overall = "unknown"
    for group in groups:
        overall = _worse_status(overall, str(group["status"]))

    return {
        "overall_status": overall if groups else "unknown",
        "sample_count": len(samples),
        "groups": groups,
    }


def _slo_classes(
    samples: list[SyntheticProbeSample],
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for definition in SLO_DEFINITIONS:
        slo_id = str(definition["id"])
        slo_samples = [sample for sample in samples if slo_id in sample_slo_class_ids(sample)]
        slo_rollups = [rollup for rollup in rollups if slo_id in rollup_slo_class_ids(rollup)]
        current = _slo_current(slo_samples, now=now)
        windows = {
            name: _slo_window(slo_samples, slo_rollups, now=now, seconds=seconds)
            for name, seconds in WINDOW_SECONDS.items()
        }
        rows[slo_id] = {
            **definition,
            "target_uptime_percent": SLO_TARGET_UPTIME_PERCENT,
            "status": current["status"],
            "status_label": _status_label(str(current["status"])),
            "status_class": _status_class(str(current["status"])),
            "current_by_region": current["by_region"],
            "sample_count": current["sample_count"],
            "windows": windows,
        }
    return rows


def _slo_current(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    latest: dict[tuple[str, str, str], SyntheticProbeSample] = {}
    for sample in samples:
        key = (sample.monitor_region, sample.target, sample.probe_type)
        if key not in latest:
            latest[key] = sample

    overall = "unknown"
    by_region: dict[str, dict[str, Any]] = {}
    sample_count = 0
    monitor_reporting = _monitor_is_reporting(samples, now=now)
    for sample in latest.values():
        status, _signed_age = _sample_effective_status(
            sample, now=now, monitor_reporting=monitor_reporting
        )
        overall = _worse_status(overall, status)
        sample_count += 1
        for region in _sample_region_keys(sample):
            region_row = by_region.setdefault(
                region,
                {
                    "status": "unknown",
                    "status_label": "Unknown",
                    "status_class": "unknown",
                    "sample_count": 0,
                    "last_checked_at": None,
                },
            )
            region_row["status"] = _worse_status(str(region_row["status"]), status)
            region_row["status_label"] = _status_label(str(region_row["status"]))
            region_row["status_class"] = _status_class(str(region_row["status"]))
            region_row["sample_count"] = int(region_row["sample_count"]) + 1
            last_checked_at = region_row["last_checked_at"]
            if last_checked_at is None or sample.created_at > str(last_checked_at):
                region_row["last_checked_at"] = sample.created_at

    return {
        "status": overall if sample_count else "unknown",
        "by_region": by_region,
        "sample_count": sample_count,
    }


def _sample_region_keys(sample: SyntheticProbeSample) -> list[str]:
    if sample.target_region:
        return [sample.target_region]
    if sample.target in {"canonical", "control-plane"}:
        return ["global"]
    return [sample.target]


def _slo_window(
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
    counts: dict[str, int] = defaultdict(int)
    for sample in raw_rows:
        counts[sample.status] += 1
    for rollup in backfill_rollups:
        for status, count in _rollup_status_counts(rollup).items():
            counts[status] += count
    status_counts = dict(counts)
    sample_count = sum(status_counts.values())
    bad_count = max(sample_count - status_counts.get("up", 0), 0)
    uptime_percent = _uptime_percent_counts(status_counts) if sample_count else None
    bad_fraction = bad_count / sample_count if sample_count else None
    burn_rate = (
        round(bad_fraction / SLO_ERROR_BUDGET_FRACTION, 2)
        if bad_fraction is not None and SLO_ERROR_BUDGET_FRACTION > 0
        else None
    )
    return {
        "overall_status": _aggregate_status_counts(status_counts) if sample_count else "unknown",
        "uptime_percent": uptime_percent,
        "sample_count": sample_count,
        "up_count": status_counts.get("up", 0),
        "bad_count": bad_count,
        "burn_rate": burn_rate,
        "status_counts": status_counts,
    }


def _rollup_status_counts(rollup: SyntheticRollup) -> dict[str, int]:
    return {
        "up": rollup.up_count,
        "down": rollup.down_count,
        "degraded": rollup.degraded_count,
        "routing_degraded": rollup.routing_degraded_count,
        "trust_degraded": rollup.trust_degraded_count,
        "unknown": rollup.unknown_count,
    }


def _burn_rate_alerts(slo_classes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for slo_id, slo in slo_classes.items():
        windows = slo.get("windows")
        if not isinstance(windows, dict):
            continue
        for window, row in windows.items():
            if not isinstance(row, dict):
                continue
            burn_rate = row.get("burn_rate")
            if burn_rate is None:
                continue
            level = _burn_alert_level(slo_id, str(window), float(burn_rate))
            if level is None:
                continue
            alerts.append(
                {
                    "level": level,
                    "slo_class": slo_id,
                    "slo_name": slo.get("name", slo_id),
                    "window": str(window),
                    "burn_rate": burn_rate,
                    "uptime_percent": row.get("uptime_percent"),
                    "bad_count": row.get("bad_count", 0),
                    "sample_count": row.get("sample_count", 0),
                }
            )
    return alerts


def _burn_alert_level(slo_id: str, window: str, burn_rate: float) -> str | None:
    if slo_id == ROUTER_CORE_SLO_ID and window in {"5m", "1h"} and burn_rate >= 14.4:
        return "critical"
    if window == "6h" and burn_rate >= 6.0:
        return "warning"
    if window == "24h" and burn_rate >= 3.0:
        return "watch"
    return None


def _components(
    samples: list[SyntheticProbeSample],
    *,
    now: dt.datetime,
    settings: Settings,
    rollups: list[SyntheticRollup] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    precomputed_rollups = rollups or []
    # Only what THIS deployment can measure. Iterating the full catalogue
    # here is what made the AWS EU status page advertise GCP's regional
    # gateways as permanently "unknown".
    for definition in applicable_component_definitions(settings):
        component_id = str(definition["id"])
        component_samples = [
            sample for sample in samples if component_id in sample_component_ids(sample)
        ]
        component_rollups = [
            rollup
            for rollup in precomputed_rollups
            if rollup.component == component_id
            and rollup.probe_type in component_probe_types(component_id)
        ]
        component_hour_rollups = [rollup for rollup in component_rollups if rollup.period == "hour"]
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
            sample
            for sample in component_samples
            if _parse_time(sample.created_at) >= five_minute_cutoff
        ]
        status = _aggregate_status([sample.status for sample in current_samples])
        if not current_samples and component_id == "image_generation":
            status = _fresh_image_rollup_status(component_hour_rollups, now=now)
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
                else (
                    _uptime_percent([sample.status for sample in day_samples])
                    if day_samples
                    else None
                ),
                "sample_count_24h": int(day_rollup["sample_count"])
                if day_rollup
                else len(day_samples),
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


def _fresh_image_rollup_status(
    rollups: list[SyntheticRollup],
    *,
    now: dt.datetime,
) -> str:
    """Recover current low-cadence image status from its latest hourly rollup."""
    latest = max(
        (
            rollup
            for rollup in rollups
            if rollup.last_checked_at is not None
            and (now - _parse_time(rollup.last_checked_at)).total_seconds()
            <= IMAGE_GENERATION_SAMPLE_TTL_SECONDS
        ),
        key=lambda rollup: str(rollup.last_checked_at),
        default=None,
    )
    if latest is None:
        return "unknown"
    return _aggregate_status_counts(_rollup_status_counts(latest))


def _latency_breakdown(samples: list[SyntheticProbeSample]) -> list[dict[str, Any]]:
    return _sample_group_breakdown(samples)


def _sample_group_breakdown(
    samples: list[SyntheticProbeSample],
    *,
    include_component: bool = False,
) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str, str, str], list[SyntheticProbeSample]] = defaultdict(list)
    for sample in samples:
        component_ids = sample_component_ids(sample) if include_component else [""]
        if include_component and not component_ids:
            component_ids = ["uncategorized"]
        for component_id in component_ids:
            by_group[
                (
                    component_id,
                    sample.target,
                    sample.probe_type,
                    sample.monitor_region,
                    sample.target_region or "",
                )
            ].append(sample)
    rows: list[dict[str, Any]] = []
    for (
        component_id,
        target,
        probe_type,
        monitor_region,
        target_region,
    ), probe_samples in sorted(by_group.items()):
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
        dns_values = [
            sample.dns_milliseconds
            for sample in probe_samples
            if sample.dns_milliseconds is not None
        ]
        tcp_values = [
            sample.tcp_connect_milliseconds
            for sample in probe_samples
            if sample.tcp_connect_milliseconds is not None
        ]
        tls_values = [
            sample.tls_handshake_milliseconds
            for sample in probe_samples
            if sample.tls_handshake_milliseconds is not None
        ]
        gateway_values = [
            sample.gateway_processing_milliseconds
            for sample in probe_samples
            if sample.gateway_processing_milliseconds is not None
        ]
        statuses = [sample.status for sample in probe_samples]
        row = {
            "target": target,
            "target_label": _target_label(target),
            "probe_type": probe_type,
            "monitor_region": monitor_region,
            "target_region": target_region or None,
            "region_pair": _region_pair(monitor_region, target_region or None),
            "route_label": _route_label(
                target,
                monitor_region,
                target_region or None,
            ),
            "status": _aggregate_status(statuses),
            "uptime_percent": _uptime_percent(statuses),
            "sample_count": len(probe_samples),
            "p50_latency_milliseconds": _percentile(latencies, 50),
            "p95_latency_milliseconds": _percentile(latencies, 95),
            "p50_ttfb_milliseconds": _percentile(ttfbs, 50),
            "p95_ttfb_milliseconds": _percentile(ttfbs, 95),
            "p50_dns_milliseconds": _percentile(dns_values, 50),
            "p95_dns_milliseconds": _percentile(dns_values, 95),
            "p50_tcp_connect_milliseconds": _percentile(tcp_values, 50),
            "p95_tcp_connect_milliseconds": _percentile(tcp_values, 95),
            "p50_tls_handshake_milliseconds": _percentile(tls_values, 50),
            "p95_tls_handshake_milliseconds": _percentile(tls_values, 95),
            "p50_gateway_processing_milliseconds": _percentile(gateway_values, 50),
            "p95_gateway_processing_milliseconds": _percentile(gateway_values, 95),
            "last_checked_at": max(sample.created_at for sample in probe_samples),
        }
        if include_component:
            row["component"] = component_id
            row["component_name"] = component_name(component_id)
        rows.append(row)
    return rows


def _rollup_group_breakdown(
    rollups: list[SyntheticRollup],
    *,
    include_component: bool = False,
) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str, str, str, str], list[SyntheticRollup]] = defaultdict(list)
    for rollup in rollups:
        by_group[
            (
                rollup.component if include_component else "",
                rollup.target,
                rollup.probe_type,
                rollup.monitor_region,
                rollup.target_region or "",
            )
        ].append(rollup)
    rows: list[dict[str, Any]] = []
    for (
        component_id,
        target,
        probe_type,
        monitor_region,
        target_region,
    ), group_rollups in sorted(by_group.items()):
        merged = merge_rollups(group_rollups)
        status_counts = _int_dict(merged["status_counts"])
        row = {
            "target": target,
            "target_label": _target_label(target),
            "probe_type": probe_type,
            "monitor_region": monitor_region,
            "target_region": target_region or None,
            "region_pair": _region_pair(monitor_region, target_region or None),
            "route_label": _route_label(
                target,
                monitor_region,
                target_region or None,
            ),
            "status": _aggregate_status_counts(status_counts),
            "uptime_percent": _uptime_percent_counts(status_counts),
            "sample_count": int(merged["sample_count"]),
            "p50_latency_milliseconds": merged["p50_latency_milliseconds"],
            "p95_latency_milliseconds": merged["p95_latency_milliseconds"],
            "p50_ttfb_milliseconds": merged["p50_ttfb_milliseconds"],
            "p95_ttfb_milliseconds": merged["p95_ttfb_milliseconds"],
            "p50_dns_milliseconds": merged["p50_dns_milliseconds"],
            "p95_dns_milliseconds": merged["p95_dns_milliseconds"],
            "p50_tcp_connect_milliseconds": merged["p50_tcp_connect_milliseconds"],
            "p95_tcp_connect_milliseconds": merged["p95_tcp_connect_milliseconds"],
            "p50_tls_handshake_milliseconds": merged["p50_tls_handshake_milliseconds"],
            "p95_tls_handshake_milliseconds": merged["p95_tls_handshake_milliseconds"],
            "p50_gateway_processing_milliseconds": merged["p50_gateway_processing_milliseconds"],
            "p95_gateway_processing_milliseconds": merged["p95_gateway_processing_milliseconds"],
            "last_checked_at": merged["last_checked_at"],
            "top_error": merged["top_error"],
        }
        if include_component:
            row["component"] = component_id
            row["component_name"] = component_name(component_id)
        rows.append(row)
    return rows


def _region_pair(monitor_region: str, target_region: str | None) -> str:
    if target_region:
        return f"{monitor_region} -> {target_region}"
    return monitor_region


def _target_label(target: str) -> str:
    return {
        "canonical": "Global endpoint",
        "us-central1": "US Central direct",
        "us-east4": "US East direct",
        "europe-west4": "EU direct",
        "southamerica-east1": "São Paulo direct",
        # Per-region AWS targets: same hostname as "canonical", pinned to
        # one region's load balancer (which fronts that region's enclave
        # fleet — see COMPONENT_DEFINITIONS on why this does not say
        # "enclave").
        "eu-west-1": "Ireland gateway direct",
        "eu-west-3": "Paris gateway direct",
        "control-plane": "Control plane",
    }.get(target, target.replace("-", " ").title())


def _route_label(target: str, monitor_region: str, target_region: str | None) -> str:
    return f"{_target_label(target)} · {_region_pair(monitor_region, target_region)}"


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
    return [
        sample
        for sample in latest.values()
        if (now - _parse_time(sample.created_at)).total_seconds()
        <= (
            IMAGE_GENERATION_SAMPLE_TTL_SECONDS
            if sample.probe_type == "image_generation"
            else CURRENT_SAMPLE_TTL_SECONDS
        )
    ]


def _component_history(
    samples: list[SyntheticProbeSample], *, now: dt.datetime
) -> list[dict[str, Any]]:
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
                    "latency_breakdown": [],
                    "top_error": None,
                    "title": _history_title_hourly(bucket_start, "unknown", None, 0, None),
                }
            )
            continue

        statuses = [sample.status for sample in rows]
        uptime = _uptime_percent(statuses)
        status = _history_status(
            uptime, has_trust_degraded=any(s == "trust_degraded" for s in statuses)
        )

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
                "latency_breakdown": _latency_breakdown(rows),
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
                    "latency_breakdown": [],
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
    status = _history_status(
        uptime, has_trust_degraded=any(s == "trust_degraded" for s in statuses)
    )

    latencies = [
        sample.latency_milliseconds for sample in rows if sample.latency_milliseconds is not None
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
        "latency_breakdown": _latency_breakdown(rows),
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
        "latency_breakdown": _rollup_group_breakdown(rows),
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


def _recent_events(
    samples: list[SyntheticProbeSample],
    *,
    rollups: list[SyntheticRollup],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(seconds=WINDOW_SECONDS["24h"])
    events: list[dict[str, Any]] = []
    raw_event_buckets: set[tuple[str, str, str, str]] = set()
    for sample in samples:
        if _parse_time(sample.created_at) < cutoff or sample.status == "up":
            continue
        component_ids = sample_component_ids(sample)
        if not component_ids and not sample_slo_class_ids(sample):
            continue
        component_names = [
            str(definition["name"])
            for definition in COMPONENT_DEFINITIONS
            if str(definition["id"]) in component_ids
        ]
        raw_event_buckets.add(
            (
                sample.target,
                sample.probe_type,
                sample.monitor_region,
                sample.created_at[:13],
            )
        )
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
                "aggregate": False,
            }
        )

    component_order = {
        str(definition["id"]): index for index, definition in enumerate(COMPONENT_DEFINITIONS)
    }
    rollup_groups_seen: set[tuple[str, str, str, str]] = set()
    recent_rollups = sorted(
        (
            rollup
            for rollup in rollups
            if rollup.period == "hour"
            and rollup.last_checked_at is not None
            and _parse_time(rollup.last_checked_at) >= cutoff
        ),
        key=lambda rollup: (
            rollup.period_start,
            -component_order.get(rollup.component, len(component_order)),
        ),
        reverse=True,
    )
    for rollup in recent_rollups:
        # Apply the SAME publishability rule the raw-sample loop above uses.
        # The two loops render into one public list, so disagreeing about
        # what belongs there is the defect: component-less diagnostics
        # (gateway_cold_path / gateway_reused_path) were skipped as raw
        # samples but then reappeared an hour later as their rollup, drawn
        # as "Uncategorized — Major outage" with an internal error slug.
        # The underlying sample and rollup are still recorded and still red;
        # only the unlabelled public row is suppressed.
        if rollup.component == UNCATEGORIZED_COMPONENT and not rollup_slo_class_ids(rollup):
            continue
        counts = _rollup_status_counts(rollup)
        failure_count = sum(count for status, count in counts.items() if status != "up")
        if failure_count <= 0:
            continue
        bucket_key = (
            rollup.target,
            rollup.probe_type,
            rollup.monitor_region,
            rollup.period_start[:13],
        )
        if bucket_key in raw_event_buckets or bucket_key in rollup_groups_seen:
            continue
        rollup_groups_seen.add(bucket_key)
        status = _rollup_failure_status(counts)
        merged = merge_rollups([rollup])
        events.append(
            {
                "id": f"rollup:{rollup.id}",
                "component": component_name(rollup.component),
                "status": status,
                "status_label": _status_label(status),
                "status_class": _status_class(status),
                "probe_type": rollup.probe_type,
                "target": rollup.target,
                "monitor_region": rollup.monitor_region,
                "created_at": rollup.period_start,
                "latency_milliseconds": merged["p50_latency_milliseconds"],
                "error_type": merged["top_error"],
                "aggregate": True,
                "failure_count": failure_count,
                "sample_count": rollup.sample_count,
            }
        )
    events.sort(key=lambda event: str(event["created_at"]), reverse=True)
    return events[:8]


def _rollup_failure_status(counts: dict[str, int]) -> str:
    for status in ("trust_degraded", "down", "routing_degraded", "degraded", "unknown"):
        if counts.get(status, 0) > 0:
            return status
    return "unknown"


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


def _summary(
    status: str,
    *,
    freshness: dict[str, Any] | None = None,
    down_components: list[str] | None = None,
) -> dict[str, str]:
    if status == "unknown" and freshness and freshness.get("is_stale"):
        latest = freshness.get("latest_sample_at")
        if latest:
            return {
                "headline": "Monitor Data Stale",
                "detail": f"Synthetic checks stopped reporting after {latest}. Router-core history remains visible below.",
            }
        return {
            "headline": "Monitor Data Missing",
            "detail": "Synthetic checks have not reported data yet.",
        }
    if status == "up":
        return {
            "headline": "All Systems Operational",
            "detail": "Router-core synthetic checks are passing for attested reachability, authorization, fallback, and settlement.",
        }
    if status == "down":
        return {
            "headline": "Router Core Outage",
            "detail": "One or more router-core synthetic checks are failing.",
        }
    if status == "trust_degraded":
        return {
            "headline": "Trust Verification Degraded",
            "detail": "Inference may still work, but an attestation check is failing and should be treated as critical.",
        }
    if status in {"degraded", "routing_degraded"}:
        # Name the failing surface when the degradation is a component-level
        # outage rather than a router-core burn: "Partial Outage: Model
        # Inference" is honest and actionable; "Router Core Degraded" for a
        # dead pong path would be both wrong and alarming.
        if down_components:
            names = ", ".join(down_components)
            verb = "is" if len(down_components) == 1 else "are"
            return {
                "headline": f"Partial Outage: {names}",
                "detail": (
                    f"{names} {verb} failing synthetic checks. "
                    "Other router-core checks are passing."
                ),
            }
        return {
            "headline": "Router Core Degraded",
            "detail": (
                "One or more canonical reachability, authorization, fallback, "
                "or settlement checks are degraded."
            ),
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
    parts = [
        f"{label}",
        f"{_status_label(status)}",
        f"{uptime:.2f}% uptime",
        f"{sample_count} samples",
    ]
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
