"""Pure alert evaluation for client-observed reliability signals."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router.config import Settings, get_settings
from trusted_router.synthetic.alerts import ops_alert


@dataclass(frozen=True)
class ClientWatchAlert:
    kind: str
    message: str
    fingerprint: list[str]
    tags: dict[str, str]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_client_watch(
    snapshot: dict[str, Any] | None,
    *,
    router_core_up: bool,
    now: dt.datetime,
) -> list[ClientWatchAlert]:
    """Return page-worthy conditions from one client reliability snapshot."""

    _ = now
    if not isinstance(snapshot, dict):
        return [
            ClientWatchAlert(
                kind="snapshot_missing",
                message="client_observed.snapshot_missing",
                fingerprint=["client-observed-stale"],
                tags={"tr_component": "client_observed"},
            )
        ]

    alerts: list[ClientWatchAlert] = []
    canary = _mapping(snapshot.get("canary"))
    freshness = _mapping(snapshot.get("freshness"))
    canary_age = _optional_int(canary.get("last_seen_age_seconds"))
    freshness_age = _optional_int(freshness.get("age_seconds"))
    watch = _mapping(snapshot.get("watch"))
    recent_hosts = _mapping(watch.get("by_host_15m"))
    baseline_hosts = _mapping(watch.get("by_host_7d"))
    required_tenants = 2 if canary_age is not None and canary_age > 900 else 3

    if router_core_up:
        for host in sorted(str(value) for value in recent_hosts):
            recent = _mapping(recent_hosts.get(host))
            attempts = _optional_int(recent.get("attempts")) or 0
            failures = _optional_int(recent.get("attempt_tr_fault")) or 0
            tenants = _optional_int(recent.get("distinct_tenants")) or 0
            if attempts < 200 or tenants < required_tenants:
                continue
            baseline = _mapping(baseline_hosts.get(host))
            baseline_attempts = _optional_int(baseline.get("attempts")) or 0
            baseline_failures = _optional_int(baseline.get("attempt_tr_fault")) or 0
            rate = failures / attempts
            baseline_rate = baseline_failures / baseline_attempts if baseline_attempts else 0.0
            if rate < max(0.02, 20 * baseline_rate):
                continue
            alerts.append(
                ClientWatchAlert(
                    kind="invisible_outage",
                    message=(
                        "client_observed.invisible_outage "
                        f"host={host} rate={rate:.4f} attempts={attempts} tenants={tenants} "
                        f"baseline={baseline_rate:.4f} router_core=up"
                    ),
                    fingerprint=["client-observed-outage", host],
                    tags={"host": host, "tr_component": "client_observed"},
                )
            )

    if (freshness_age is not None and freshness_age > 900) or (
        canary_age is not None and canary_age > 900
    ):
        alerts.append(
            ClientWatchAlert(
                kind="pipeline_stale",
                message=(
                    "client_observed.pipeline_stale "
                    f"age_seconds={freshness_age} canary_age_seconds={canary_age}"
                ),
                fingerprint=["client-observed-stale"],
                tags={"tr_component": "client_observed"},
            )
        )
    return alerts


def report_client_watch(
    alerts: list[ClientWatchAlert],
    *,
    settings: Settings | None = None,
) -> None:
    """Emit enabled client-watch alerts through the shared ops pager."""

    active_settings = settings or get_settings()
    if not active_settings.client_events_enabled:
        return
    for alert in alerts:
        ops_alert(alert.message, fingerprint=alert.fingerprint, tags=alert.tags)
