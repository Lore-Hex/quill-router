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
from trusted_router.synthetic.route_health import RouteHealthFlag, UnservedRoute

HEADERS = {"x-trustedrouter-internal-token": "test-observer-secret"}
HELD = ("nextbit-eu", "google/gemma-3-27b-it")


def _held_evidence() -> dict[tuple[str, str], UnservedRoute]:
    qualified = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)
    stamp = qualified.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {HELD: UnservedRoute(*HELD, samples=6, first_failure_at="2026-11-22T15:34:15Z",
                                last_failure_at=stamp, error_types=("rate_limit_error",),
                                kind="unavailable", qualified_at=stamp)}


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
    """Fingerprints of the events that actually reach the Sentry SDK."""
    import sentry_sdk

    sent: list[list[str]] = []

    def capture(*_args: object, **_kwargs: object) -> None:
        # ops_alert captures inside sentry_sdk.new_scope().
        fingerprint = list(sentry_sdk.get_current_scope()._fingerprint or [])  # noqa: SLF001
        if fingerprint[:1] == ["ops-alert"]:
            sent.append(fingerprint[1:])

    class _LegacyScope:
        # The structural route-health alert still uses push_scope.
        fingerprint: list[str] = []

        def __enter__(self) -> _LegacyScope:
            return self

        def __exit__(self, *_exc: object) -> None:
            sent.append(list(self.fingerprint))

        def set_tag(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(sentry_sdk, "capture_message", capture)
    monkeypatch.setattr(sentry_sdk, "push_scope", lambda: _LegacyScope())
    return sent


def _client(monkeypatch: pytest.MonkeyPatch, evaluations: list[Any] | None = None) -> TestClient:
    from tests.test_synthetic_monitoring import _benchmark_ingest_settings
    from trusted_router.main import create_app
    from trusted_router.routes.internal import synthetic as synthetic_routes

    queue = list(evaluations or [])

    def evaluate(_store: Any) -> list[RouteHealthFlag]:
        result = queue.pop(0) if queue else _flags()
        if isinstance(result, Exception):
            raise result
        return list(result)

    monkeypatch.setattr(synthetic_routes, "evaluate_route_health", evaluate)
    monkeypatch.setattr(synthetic_routes, "_route_holds", lambda: (_held_evidence(), frozenset(), {}))
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
    from trusted_router.storage import InMemoryStore

    claims: list[str] = []
    real_claim = InMemoryStore.record_webhook_event_once

    def recording_claim(self: Any, source: str, event_id: str) -> bool:
        claims.append(event_id)
        return real_claim(self, source, event_id)

    monkeypatch.setattr(InMemoryStore, "record_webhook_event_once", recording_claim)
    hour = {"value": "2026-11-21T08"}
    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: hour["value"])
    client = _client(monkeypatch)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    per_sweep = len(alerts)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert len(alerts) == per_sweep > 0
    hour["value"] = "2026-11-21T09"
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    # Every alert reports again except the route-hold announcement, which is
    # claimed once per hold episode (Sentry QUILL-ROUTER-HV re-fired hourly
    # while its committed hold waited for a deploy).
    assert len(alerts) == 2 * per_sweep - 1
    assert sum(fingerprint[0] == "route-hold" for fingerprint in alerts) == 1
    # Every emission went through a real claim, none through the fail-open path.
    hourly = {claim.rsplit("@", 1)[1] for claim in claims if not claim.startswith("route-hold|")}
    assert hourly == {"2026-11-21T08", "2026-11-21T09"}
    assert {claim for claim in claims if claim.startswith("route-hold|")} == {
        f"route-hold|{HELD[0]}|{HELD[1]}|episode=2026-11-22T15:34:15Z;stage=0",
    }


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


def test_a_failed_first_evaluation_does_not_silence_the_second(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: "2026-11-21T08")
    monkeypatch.setattr(synthetic_routes, "is_analytics_unavailable", lambda exc: True)
    client = _client(monkeypatch, [RuntimeError("analytics timed out"), _flags()])
    blind = client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert blind.json()["data"]["evaluated"] is False
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert ["route-availability", "chutes", "qwen/qwen3.5-397b-a17b"] in alerts
    assert ["route-hold", *HELD] in alerts


def test_a_flag_only_the_second_caller_sees_still_alerts(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: "2026-11-21T08")
    configuration = [flag for flag in _flags() if flag.kind == "configuration"]
    client = _client(monkeypatch, [[], configuration])
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    assert alerts == [["route-configuration", "maple", "x/y"]]


def test_a_digest_listing_a_newly_flaky_route_is_still_sent(
    monkeypatch: pytest.MonkeyPatch, alerts: list[list[str]],
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: "2026-11-21T14")
    degraded = [flag for flag in _flags() if flag.kind == "degradation"]
    another = RouteHealthFlag("novita", "e/f", 12, 4, 0.33, "provider_error", None,
                              kind="degradation", newest_error_status=503)
    client = _client(monkeypatch, [degraded, [*degraded, another], [*degraded, another]])
    for _caller in range(3):
        client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
    digests = [fp for fp in alerts if fp[0] == "route-degradation-digest"]
    # One digest per distinct contents: A, then A+B; the repeat of A+B is folded.
    assert len(digests) == 2
