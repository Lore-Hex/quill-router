"""A route down for hours is held from routing, and pages once, not hourly.

Sentry QUILL-ROUTER-HR: Chutes answered HTTP 502 for qwen/qwen3.5-27b on
every probe from 2026-11-20 12:01Z for over thirteen hours while serving its
other models. The route stayed advertised and routable, customer requests
tried it first, and route-availability paged every hour. The unserved-route
hold (scripts/pricing/unserved_route_holds.py) already holds a route the
provider refuses to serve, with automatic 24h expiry and re-probe; it ignored
5xx streaks. A sustained outage is now evidence for the same hold, and the
route-health pass replaces the hourly page with one route-hold alert.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.pricing import unserved_route_holds as holds
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic import route_health
from trusted_router.synthetic.route_health import (
    RouteHealthFlag,
    evaluate_unserved_routes,
    report_route_holds,
    split_held_flags,
)

NOW = dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.UTC)
ROUTE = ("chutes", "qwen/qwen3.5-27b")
SIBLING = ("chutes", "deepseek/deepseek-v4-flash-0731")


def _sample(
    route: tuple[str, str],
    *,
    minutes_ago: int,
    status: str = "error",
    error_type: str | None = "provider_error",
    error_status: int | None = 502,
) -> ProviderBenchmarkSample:
    provider, model = route
    return ProviderBenchmarkSample(
        id=f"{provider}-{model}-{minutes_ago}-{status}",
        model=model,
        provider=provider,
        provider_name=provider,
        status=status,
        usage_type="Credits",
        streamed=True,
        error_type=None if status == "success" else error_type,
        error_status=None if status == "success" else error_status,
        source="synthetic",
        created_at=(NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
    )


def _outage(*, hours: float = 13, every: int = 60, newest: int = 1, **kwargs: Any) -> list[ProviderBenchmarkSample]:
    count = int(hours * 60 // every) + 1
    return [_sample(ROUTE, minutes_ago=newest + i * every, **kwargs) for i in range(count)]


def _sibling_success(minutes_ago: int = 20) -> ProviderBenchmarkSample:
    return _sample(SIBLING, minutes_ago=minutes_ago, status="success")


class _Store:
    def __init__(self, samples: list[ProviderBenchmarkSample]) -> None:
        self.samples = samples

    def provider_route_benchmark_samples(
        self, *, cutoff: str, per_route_limit: int, limit: int,
    ) -> list[ProviderBenchmarkSample]:
        rows = sorted(
            (sample for sample in self.samples if sample.created_at >= cutoff),
            key=lambda sample: sample.created_at, reverse=True,
        )
        counts: dict[tuple[str, str], int] = {}
        selected = []
        for sample in rows:
            route = (sample.provider, sample.model)
            if counts.get(route, 0) < per_route_limit:
                counts[route] = counts.get(route, 0) + 1
                selected.append(sample)
        return selected[:limit]


def _evaluate(samples: list[ProviderBenchmarkSample]) -> list[route_health.UnservedRoute]:
    return evaluate_unserved_routes(
        _Store(samples), routes=[ROUTE, SIBLING], now=NOW,  # type: ignore[arg-type]
    )


# --- evidence ---------------------------------------------------------------


def test_chutes_502_outage_with_a_serving_sibling_is_hold_evidence() -> None:
    found = _evaluate(_outage() + [_sibling_success()])
    assert [(route.provider, route.model) for route in found] == [ROUTE]
    assert found[0].kind == "unavailable"
    assert found[0].error_types == ("provider_error",)


@pytest.mark.parametrize(("error_type", "error_status"), [
    ("upstream_error", 503), ("rate_limited", 429), ("ReadTimeout", None),
    ("ConnectError", None), ("provider_error", 404),
])
def test_any_provider_side_failure_class_counts_once_sustained(
    error_type: str, error_status: int | None,
) -> None:
    samples = _outage(error_type=error_type, error_status=error_status) + [_sibling_success()]
    assert [route.kind for route in _evaluate(samples)] == ["unavailable"]


def test_an_outage_shorter_than_the_bound_is_not_evidence() -> None:
    hours = route_health.UNAVAILABLE_MIN_SPAN.total_seconds() / 3600 - 0.5
    assert _evaluate(_outage(hours=hours, every=20) + [_sibling_success()]) == []


def test_without_a_fresh_sibling_success_an_outage_is_not_evidence() -> None:
    # Provider-wide or TrustedRouter-side: never hold routes one by one.
    assert _evaluate(_outage()) == []
    stale = _sibling_success(minutes_ago=7 * 60)
    assert _evaluate(_outage() + [stale]) == []


def test_recovery_breaks_the_outage() -> None:
    samples = _outage(newest=30) + [_sample(ROUTE, minutes_ago=5, status="success"), _sibling_success()]
    assert _evaluate(samples) == []


@pytest.mark.parametrize("error_type", [
    "probe_config_error", "router_error", "router_database_contention", "monitor_account_unavailable",
])
def test_our_own_failures_break_the_outage(error_type: str) -> None:
    head = _sample(ROUTE, minutes_ago=0, error_type=error_type, error_status=400)
    assert _evaluate([head, *_outage(newest=10), _sibling_success()]) == []


def test_an_unsupported_probe_breaks_the_outage() -> None:
    head = _sample(ROUTE, minutes_ago=0, status="unsupported", error_type="provider_error")
    assert _evaluate([head, *_outage(newest=10), _sibling_success()]) == []


def test_failures_from_before_a_hold_do_not_rearm_a_lifted_route() -> None:
    gap = int(route_health.UNSERVED_MAX_PROBE_GAP.total_seconds() // 60) + 60
    samples = [_sample(ROUTE, minutes_ago=5), *_outage(newest=5 + gap), _sibling_success()]
    assert _evaluate(samples) == []


def test_model_not_served_streaks_keep_the_shorter_bound() -> None:
    # Three and a half hours of unsupported_route is still enough, and reported as unserved.
    samples = [
        _sample(ROUTE, minutes_ago=10 + 35 * i, status="unsupported",
                error_type="unsupported_route", error_status=404)
        for i in range(7)
    ] + [_sibling_success()]
    assert [route.kind for route in _evaluate(samples)] == ["unserved"]


# --- the hold itself --------------------------------------------------------


def test_outage_evidence_holds_the_route_and_expiry_releases_it(tmp_path: Path) -> None:
    manifest = tmp_path / "chutes.json"
    rows = [
        {"id": model, "upstream_id": model, "input_token_price_per_m": 1, "output_token_price_per_m": 1}
        for model in (ROUTE[1], SIBLING[1], "other/a", "other/b")
    ]
    manifest.write_text(json.dumps({"provider": "chutes", "models": rows}), encoding="utf-8")
    found = _evaluate(_outage() + [_sibling_success()])
    evidence = {(route.provider, route.model) for route in found}

    notes = holds.apply_unserved_route_holds(evidence, manifest_dir=tmp_path, now=NOW)
    held = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert held[ROUTE[1]]["routable"] is False
    assert held[ROUTE[1]]["routable_reason"] == holds.UNSERVED_ROUTE_HOLD_REASON
    assert held[SIBLING[1]].get("routable") is not False
    assert any(ROUTE[1] in note for note in notes)

    # 24h later the probes have stopped (no evidence): the hold lifts.
    later = NOW + dt.timedelta(hours=holds.HOLD_HOURS, minutes=1)
    holds.apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=later)
    lifted = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert lifted[ROUTE[1]].get("routable") is not False


# --- alerting ---------------------------------------------------------------


def _availability_flag(route: tuple[str, str] = ROUTE) -> RouteHealthFlag:
    return RouteHealthFlag(
        provider=route[0], model=route[1], samples=9, failures=9, failure_rate=1.0,
        newest_error_type="provider_error", newest_error_message=None,
        kind="availability", newest_error_status=502,
    )


def test_held_route_alerts_once_as_a_hold_not_as_route_availability(monkeypatch) -> None:
    events: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "trusted_router.synthetic.alerts.ops_alert",
        lambda message, *, fingerprint, tags=None: events.append((message, fingerprint)) or True,
    )
    other = ("chutes", "other/a")
    flags = [_availability_flag(), _availability_flag(other)]
    paging, holding = split_held_flags(flags, {ROUTE})
    assert [(flag.provider, flag.model) for flag in holding] == [ROUTE]
    assert [(flag.provider, flag.model) for flag in paging] == [other]

    route_health.report_route_health(paging, now=NOW)
    report_route_holds(holding)
    fingerprints = [fingerprint for _, fingerprint in events]
    assert fingerprints == [["route-availability", *other], ["route-hold", *ROUTE]]
    hold_message = events[1][0]
    assert hold_message.startswith("route-hold: chutes/qwen/qwen3.5-27b ")
    assert "(latest: provider_error, HTTP 502)" in hold_message


def test_configuration_and_degradation_flags_are_never_superseded() -> None:
    flags = [
        RouteHealthFlag(ROUTE[0], ROUTE[1], 6, 6, 1.0, "provider_auth_config", None, kind="configuration"),
        RouteHealthFlag(ROUTE[0], ROUTE[1], 12, 4, 0.33, "provider_error", None, kind="degradation"),
    ]
    paging, holding = split_held_flags(flags, {ROUTE})
    assert paging == flags and holding == []


def _route_health_client(monkeypatch, samples: list[ProviderBenchmarkSample]) -> TestClient:
    from tests.test_synthetic_monitoring import _benchmark_ingest_settings
    from trusted_router.main import create_app
    from trusted_router.routes.internal import synthetic as synthetic_routes
    from trusted_router.storage import InMemoryStore

    monkeypatch.setattr(synthetic_routes, "evaluate_route_health", lambda _store: [_availability_flag()])
    monkeypatch.setattr(route_health, "rotation_candidates", lambda: {"chutes": [ROUTE[1], SIBLING[1]]})
    monkeypatch.setattr(
        InMemoryStore, "provider_route_benchmark_samples",
        lambda self, **kwargs: _Store(samples).provider_route_benchmark_samples(**kwargs),
    )
    return TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))


def _shift_to_now(samples: list[ProviderBenchmarkSample]) -> list[ProviderBenchmarkSample]:
    import dataclasses

    offset = dt.datetime.now(dt.UTC) - NOW
    return [
        dataclasses.replace(
            sample,
            created_at=(dt.datetime.fromisoformat(sample.created_at.replace("Z", "+00:00")) + offset)
            .isoformat().replace("+00:00", "Z"),
        )
        for sample in samples
    ]


def test_route_health_pass_pages_a_hold_instead_of_route_availability(monkeypatch) -> None:
    events: list[list[str]] = []
    monkeypatch.setattr(
        "trusted_router.synthetic.alerts.ops_alert",
        lambda message, *, fingerprint, tags=None: events.append(fingerprint) or True,
    )
    client = _route_health_client(monkeypatch, _shift_to_now(_outage() + [_sibling_success()]))
    response = client.post(
        "/v1/internal/synthetic/route-health",
        headers={"x-trustedrouter-internal-token": "test-observer-secret"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["evaluated"] is True
    assert [fp for fp in events if fp[0].startswith("route-")] == [["route-hold", *ROUTE]]


def test_route_health_pass_still_pages_when_hold_evidence_is_short(monkeypatch) -> None:
    events: list[list[str]] = []
    monkeypatch.setattr(
        "trusted_router.synthetic.alerts.ops_alert",
        lambda message, *, fingerprint, tags=None: events.append(fingerprint) or True,
    )
    short = _outage(hours=2, every=20) + [_sibling_success()]
    client = _route_health_client(monkeypatch, _shift_to_now(short))
    response = client.post(
        "/v1/internal/synthetic/route-health",
        headers={"x-trustedrouter-internal-token": "test-observer-secret"},
    )
    assert response.status_code == 200, response.text
    assert [fp for fp in events if fp[0].startswith("route-")] == [["route-availability", *ROUTE]]
