from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

import trusted_router.synthetic.client_watch as client_watch
from trusted_router.config import Settings
from trusted_router.synthetic.client_watch import (
    ClientWatchAlert,
    evaluate_client_watch,
    report_client_watch,
)

NOW = dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC)


def _snapshot(
    *,
    attempts: int = 200,
    failures: int = 4,
    tenants: int = 3,
    baseline_attempts: int = 1_000,
    baseline_failures: int = 0,
    canary_age: int | None = 30,
    freshness_age: int | None = 30,
) -> dict[str, Any]:
    return {
        "canary": {"last_seen_age_seconds": canary_age},
        "freshness": {"age_seconds": freshness_age},
        "watch": {
            "by_host_15m": {
                "apex": {
                    "attempts": attempts,
                    "attempt_tr_fault": failures,
                    "distinct_tenants": tenants,
                }
            },
            "by_host_7d": {
                "apex": {
                    "attempts": baseline_attempts,
                    "attempt_tr_fault": baseline_failures,
                }
            },
        },
    }


@pytest.mark.parametrize(
    ("updates", "router_core_up", "fires"),
    [
        ({}, True, True),
        ({"attempts": 199}, True, False),
        ({"tenants": 2}, True, False),
        ({"failures": 3}, True, False),
        ({"baseline_attempts": 500, "baseline_failures": 1}, True, False),
        (
            {
                "failures": 8,
                "baseline_attempts": 1_000,
                "baseline_failures": 2,
            },
            True,
            True,
        ),
        ({}, False, False),
    ],
    ids=[
        "exact-minimums",
        "attempts-low",
        "tenants-low",
        "absolute-rate-low",
        "baseline-multiple-low",
        "baseline-multiple-equal",
        "router-core-down",
    ],
)
def test_invisible_outage_threshold_table(
    updates: dict[str, int],
    router_core_up: bool,
    fires: bool,
) -> None:
    alerts = evaluate_client_watch(
        _snapshot(**updates),
        router_core_up=router_core_up,
        now=NOW,
    )
    outages = [alert for alert in alerts if alert.kind == "invisible_outage"]

    assert bool(outages) is fires
    if fires:
        assert outages[0].message == (
            "client_observed.invisible_outage host=apex rate="
            f"{updates.get('failures', 4) / updates.get('attempts', 200):.4f} "
            f"attempts={updates.get('attempts', 200)} tenants={updates.get('tenants', 3)} "
            f"baseline={updates.get('baseline_failures', 0) / updates.get('baseline_attempts', 1_000):.4f} "
            "router_core=up"
        )
        assert outages[0].fingerprint == ["client-observed-outage", "apex"]
        assert outages[0].tags == {
            "host": "apex",
            "tr_component": "client_observed",
        }


def test_two_tenants_only_suffice_when_the_canary_is_stale() -> None:
    healthy = evaluate_client_watch(
        _snapshot(tenants=2, canary_age=900),
        router_core_up=True,
        now=NOW,
    )
    stale = evaluate_client_watch(
        _snapshot(tenants=2, canary_age=901),
        router_core_up=True,
        now=NOW,
    )

    assert all(alert.kind != "invisible_outage" for alert in healthy)
    assert any(alert.kind == "invisible_outage" for alert in stale)


@pytest.mark.parametrize(
    ("freshness_age", "canary_age", "fires"),
    [
        (901, None, True),
        (None, 901, True),
        (900, 900, False),
        (None, None, False),
    ],
)
def test_pipeline_stale_thresholds(
    freshness_age: int | None,
    canary_age: int | None,
    fires: bool,
) -> None:
    alerts = evaluate_client_watch(
        _snapshot(
            attempts=0,
            freshness_age=freshness_age,
            canary_age=canary_age,
        ),
        router_core_up=True,
        now=NOW,
    )
    stale = [alert for alert in alerts if alert.kind == "pipeline_stale"]

    assert bool(stale) is fires
    if fires:
        assert stale[0].message == (
            "client_observed.pipeline_stale "
            f"age_seconds={freshness_age} canary_age_seconds={canary_age}"
        )
        assert stale[0].fingerprint == ["client-observed-stale"]


def test_missing_snapshot_shares_the_stale_fingerprint() -> None:
    assert evaluate_client_watch(None, router_core_up=True, now=NOW) == [
        ClientWatchAlert(
            kind="snapshot_missing",
            message="client_observed.snapshot_missing",
            fingerprint=["client-observed-stale"],
            tags={"tr_component": "client_observed"},
        )
    ]


def test_report_client_watch_obeys_flag_and_forwards_exact_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    alert = ClientWatchAlert(
        kind="invisible_outage",
        message="client_observed.invisible_outage test",
        fingerprint=["client-observed-outage", "apex"],
        tags={"host": "apex", "tr_component": "client_observed"},
    )

    def fake_ops_alert(
        message: str,
        *,
        fingerprint: list[str],
        tags: dict[str, str],
    ) -> bool:
        calls.append((message, fingerprint, tags))
        return True

    monkeypatch.setattr(client_watch, "ops_alert", fake_ops_alert)
    report_client_watch(
        [alert],
        settings=Settings(environment="test", client_events_enabled=False),
    )
    assert calls == []

    report_client_watch(
        [alert],
        settings=Settings(environment="test", client_events_enabled=True),
    )
    assert calls == [(alert.message, alert.fingerprint, alert.tags)]
