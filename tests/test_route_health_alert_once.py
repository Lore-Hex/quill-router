"""Each route-health alert is emitted once per sweep, not once per caller.

Two regional synthetic monitors (us-central1 and europe-west4) post every
full route-health sweep, each to its own regional control-plane instance.
Both evaluated and both reported, so every alert arrived twice in the same
second (Sentry QUILL-ROUTER-HV, the first route-hold, 2026-11-21 08:01Z; the
Maple route-configuration alerts before it). Both callers still evaluate;
only the first to claim the hour's sweep reports.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.synthetic import route_health
from trusted_router.synthetic.route_health import RouteHealthFlag

HEADERS = {"x-trustedrouter-internal-token": "test-observer-secret"}
HELD = ("nextbit-eu", "google/gemma-3-27b-it")


def _flags() -> list[RouteHealthFlag]:
    return [
        RouteHealthFlag("chutes", "qwen/qwen3.5-397b-a17b", 6, 6, 1.0, "provider_error", None,
                        kind="availability", newest_error_status=502),
        RouteHealthFlag(*HELD, 6, 6, 1.0, "rate_limit_error", None,
                        kind="availability", newest_error_status=429),
        RouteHealthFlag("maple", "x/y", 6, 6, 1.0, "provider_auth_config", "denied",
                        kind="configuration"),
        RouteHealthFlag("abliterate", "a/b", 14, 4, 0.29, "provider_error", None,
                        kind="degradation", newest_error_status=500),
        RouteHealthFlag("gmi", "c/d", 6, 6, 1.0, "provider_error", "model unavailable"),
    ]


@pytest.fixture(autouse=True)
def _reset_operation_limits() -> Iterator[None]:
    # These tests post several sweeps; leave the shared per-minute allowance
    # as they found it for the next test module.
    from trusted_router.routes.internal import synthetic as synthetic_routes

    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
    yield
    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    sent: list[list[str]] = []
    monkeypatch.setattr(
        "trusted_router.synthetic.alerts.ops_alert",
        lambda message, *, fingerprint, tags=None: sent.append(fingerprint) or True,
    )

    class _Scope:
        fingerprint: list[str] = []

        def __enter__(self) -> _Scope:
            return self

        def __exit__(self, *_exc: object) -> None:
            sent.append(list(self.fingerprint))

        def set_tag(self, *_args: object) -> None:
            return None

    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "push_scope", lambda: _Scope())
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda *_a, **_k: None)
    return sent


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from tests.test_synthetic_monitoring import _benchmark_ingest_settings
    from trusted_router.main import create_app
    from trusted_router.routes.internal import synthetic as synthetic_routes

    monkeypatch.setattr(synthetic_routes, "evaluate_route_health", lambda _store: _flags())
    monkeypatch.setattr(synthetic_routes, "_pending_route_holds", lambda: frozenset({HELD}))
    monkeypatch.setattr(synthetic_routes, "report_catalog_freshness", lambda: None)
    # The daily digest fires only in its hour; make every hour that hour.
    monkeypatch.setattr(route_health, "DEGRADATION_DIGEST_HOUR_UTC", dt.datetime.now(dt.UTC).hour)
    return TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))


def test_two_callers_in_one_sweep_emit_each_alert_once(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: "2026-11-21T08")
    client = _client(monkeypatch)
    first = client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    once = sorted(alerts)
    second = client.post("/v1/internal/synthetic/route-health", headers=HEADERS)

    assert first.status_code == second.status_code == 200
    # Both callers still evaluate and see the same verdict.
    assert first.json() == second.json()
    assert len(first.json()["data"]["flagged"]) == len(_flags())
    kinds = {fingerprint[0] for fingerprint in once}
    assert {"route-availability", "route-hold", "route-configuration",
            "route-degradation-digest", "route-health"} <= kinds
    assert sorted(alerts) == once
    assert len(alerts) == len({tuple(fp) for fp in alerts})


def test_the_next_sweep_reports_again(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    hours = iter(["2026-11-21T08", "2026-11-21T09"])
    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: next(hours))
    client = _client(monkeypatch)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    per_sweep = len(alerts)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert per_sweep > 0 and len(alerts) == 2 * per_sweep


def test_a_failed_claim_still_reports(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.storage import InMemoryStore

    def broken(self: Any, source: str, event_id: str) -> bool:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(InMemoryStore, "record_webhook_event_once", broken)
    client = _client(monkeypatch)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert any(fingerprint[0] == "route-availability" for fingerprint in alerts)


def test_fast_refusal_scope_alerts_once_across_instances(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from tests.test_synthetic_monitoring import _benchmark_ingest_settings
    from trusted_router.main import create_app
    from trusted_router.routes.internal import synthetic as synthetic_routes

    refusal = RouteHealthFlag("maple", "x/y", 3, 3, 1.0, "provider_auth_config", None,
                              kind="configuration", account_refusal=True)
    monkeypatch.setattr(synthetic_routes, "evaluate_account_refusals", lambda _store: [refusal])
    client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))
    for _instance in range(2):
        # A second regional instance has its own in-process cooldown.
        monkeypatch.setattr(route_health, "_FAST_REFUSAL_REPORTED", {})
        response = client.post(
            "/v1/internal/synthetic/route-health?scope=account_refusals", headers=HEADERS,
        )
        assert response.status_code == 200
    assert [fp for fp in alerts if fp[0] == "route-configuration"] == [
        ["route-configuration", "maple", "x/y"],
    ]
