"""The route-hold alert announces a hold once per episode, not every hour.

Sentry QUILL-ROUTER-HV: nextbit-eu/google/gemma-3-27b-it failed every probe
for hours; the route-hold alert fired at 05:01, 06:01, 07:01 and 08:01Z on
2026-11-23 although the price refresh committed the hold at 06:31. The monitor
judges holds on the DEPLOYED manifests, and a committed price snapshot reaches
production only when a rollout carries it (up to SNAPSHOT_MAX_WAIT_SECONDS
later, plus the rollout). Until then the route stays routable, keeps being
probed and flagged, still reads as "the next refresh will hold it", and the
per-hour claim let the announcement through every hour.

Now the claim carries the hold episode, so:
* qualified, not yet held: one route-hold announcement;
* still not held in later hours (the hold waiting for its deploy): silent;
* not held ROUTE_HOLD_APPLY_DEADLINE after it qualified: route-hold-not-applied;
* held in the deployed manifests: neither a hold alert nor a page;
* the 24h hold expired and the route still fails: a new episode, announced again.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.deploy import price_snapshot_deploy
from trusted_router import unserved_route_hold_plan as plan
from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
)
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic import route_health
from trusted_router.synthetic.route_health import (
    ROUTE_HOLD_APPLY_DEADLINE,
    UNAVAILABLE_MIN_SPAN,
    RouteHealthFlag,
    UnservedRoute,
    evaluate_unserved_routes,
)

HEADERS = {"x-trustedrouter-internal-token": "test-observer-secret"}
ROUTE = ("nextbit-eu", "google/gemma-3-27b-it")
SIBLING = ("nextbit-eu", "google/gemma-3-12b-it")
OTHER_MODELS = ["google/gemma-3-12b-it", "google/gemma-4-26b-a4b-it", "qwen/qwen3-14b"]
FIRST_EPISODE = "2026-11-22T15:34:15Z"


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _evidence(*, qualified_ago: dt.timedelta, first_failure_at: str = FIRST_EPISODE) -> UnservedRoute:
    qualified = _iso(_now() - qualified_ago)
    return UnservedRoute(*ROUTE, samples=6, first_failure_at=first_failure_at,
                         last_failure_at=qualified, error_types=("rate_limit_error",),
                         kind="unavailable", qualified_at=qualified)


def _flag() -> RouteHealthFlag:
    return RouteHealthFlag(*ROUTE, 6, 6, 1.0, "rate_limit_error", None,
                           kind="availability", newest_error_status=429)


def _write_manifest(directory: Path, *, held_at: str | None) -> None:
    rows: list[dict[str, Any]] = []
    for model in [ROUTE[1], *OTHER_MODELS]:
        row: dict[str, Any] = {
            "id": model, "model_type": "chat", "endpoints": ["chat/completions"],
            "input_token_price_per_m": 100_000, "output_token_price_per_m": 200_000,
        }
        if model == ROUTE[1] and held_at is not None:
            row.update(routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                       **{UNSERVED_ROUTE_HELD_AT_FIELD: held_at})
        rows.append(row)
    (directory / "nextbit-eu.json").write_text(
        json.dumps({"provider": "nextbit-eu", "models": rows}), encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _no_route_here_retires(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests replay real incidents under their real route names, some at
    the wall clock. The hold's planner does not hold a route past its scheduled
    retirement, so a retirement announced later for one of these routes would
    turn them red at its cutover instant, on main, with nothing changed.
    Retirement is covered in tests/test_unserved_hold_live_routes.py; here the
    planner sees no retirement."""
    monkeypatch.setattr(plan, "provider_model_retired", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def _reset_operation_limits() -> Iterator[None]:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
    yield
    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    """(fingerprint, message) of each ops alert that reaches the Sentry SDK."""
    import sentry_sdk

    sent: list[tuple[list[str], str]] = []

    def capture(message: str, *_args: object, **_kwargs: object) -> None:
        fingerprint = list(sentry_sdk.get_current_scope()._fingerprint or [])  # noqa: SLF001
        if fingerprint[:1] == ["ops-alert"]:
            sent.append((fingerprint[1:], message))

    monkeypatch.setattr(sentry_sdk, "capture_message", capture)
    return sent


class _Sweeps:
    """Hourly route-health sweeps against a deployed manifest directory."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, manifest_dir: Path) -> None:
        from tests.test_synthetic_monitoring import _benchmark_ingest_settings
        from trusted_router.main import create_app
        from trusted_router.routes.internal import synthetic as synthetic_routes

        self.evidence: list[UnservedRoute] = []
        self.flags: list[RouteHealthFlag] = [_flag()]
        self.hour = 5
        monkeypatch.setattr(plan, "PACKAGED_MANIFEST_DIR", manifest_dir)
        monkeypatch.setattr(synthetic_routes, "evaluate_route_health", lambda _store: list(self.flags))
        monkeypatch.setattr(synthetic_routes, "evaluate_unserved_routes", lambda _store: list(self.evidence))
        monkeypatch.setattr(synthetic_routes, "report_catalog_freshness", lambda: None)
        monkeypatch.setattr(synthetic_routes, "_sweep_hour", lambda now=None: f"2026-11-23T{self.hour:02d}")
        # Keep the daily degradation digest out of these sweeps.
        monkeypatch.setattr(route_health, "DEGRADATION_DIGEST_HOUR_UTC", (_now().hour + 12) % 24)
        self.client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))

    def run(self, *, callers: int = 2) -> None:
        from trusted_router.routes.internal import synthetic as synthetic_routes

        # Each sweep is an hour apart: a fresh per-minute operation allowance.
        synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
        # Both regional monitors post every sweep.
        for _caller in range(callers):
            response = self.client.post("/v1/internal/synthetic/route-health", headers=HEADERS)
            assert response.status_code == 200
            assert response.json()["data"]["evaluated"] is True
        self.hour += 1


def _kinds(alerts: list[tuple[list[str], str]]) -> list[str]:
    return [fingerprint[0] for fingerprint, _ in alerts]


def test_qualified_but_not_yet_held_alerts_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: list[tuple[list[str], str]],
) -> None:
    _write_manifest(tmp_path, held_at=None)
    sweeps = _Sweeps(monkeypatch, tmp_path)
    sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(minutes=40))]
    sweeps.run()
    assert alerts == [(["route-hold", *ROUTE], alerts[0][1])]
    assert alerts[0][1].startswith("route-hold: nextbit-eu/google/gemma-3-27b-it has failed every probe")
    assert "(latest: rate_limit_error, HTTP 429)" in alerts[0][1]


def test_a_committed_hold_waiting_for_its_deploy_does_not_re_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: list[tuple[list[str], str]],
) -> None:
    # The QUILL-ROUTER-HV timeline: the deployed manifest still has the route
    # routable for hours after the refresh committed the hold, so every sweep
    # still finds it pending. Only the first sweep announces it.
    _write_manifest(tmp_path, held_at=None)
    sweeps = _Sweeps(monkeypatch, tmp_path)
    for minutes in (40, 100, 160, 220):  # 05:01, 06:01, 07:01, 08:01
        sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(minutes=minutes))]
        sweeps.run()
    assert _kinds(alerts) == ["route-hold"]


def test_held_in_the_deployed_manifests_neither_alerts_nor_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: list[tuple[list[str], str]],
) -> None:
    # Once the snapshot deploys, the hold is live. Evidence and a flag that
    # still arrive (a sweep racing the rollout) neither announce the hold
    # again nor fall through to a route-availability page.
    _write_manifest(tmp_path, held_at=_iso(_now() - dt.timedelta(hours=2)))
    sweeps = _Sweeps(monkeypatch, tmp_path)
    sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(hours=4))]
    sweeps.run()
    sweeps.run()
    assert alerts == []
    other = RouteHealthFlag(SIBLING[0], "qwen/qwen3-14b", 6, 6, 1.0, "provider_error", None,
                            kind="availability", newest_error_status=502)
    sweeps.flags = [_flag(), other]
    sweeps.run()
    # Only the hold is silenced: another route of the provider still pages.
    assert alerts == [(["route-availability", other.provider, other.model], alerts[0][1])]


def test_a_hold_that_never_applies_alerts_within_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: list[tuple[list[str], str]],
) -> None:
    _write_manifest(tmp_path, held_at=None)
    sweeps = _Sweeps(monkeypatch, tmp_path)
    deadline_hours = int(ROUTE_HOLD_APPLY_DEADLINE.total_seconds() // 3600)
    for hours in range(0, 2 * deadline_hours + 2):
        sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(hours=hours, minutes=30))]
        sweeps.run()
    assert _kinds(alerts) == ["route-hold", "route-hold-not-applied", "route-hold-not-applied"]
    overdue = alerts[1]
    assert overdue[0] == ["route-hold-not-applied", *ROUTE]
    assert "still routable in production" in overdue[1]
    assert f"over {deadline_hours}h ago" in overdue[1]
    assert f"over {2 * deadline_hours}h ago" in alerts[2][1]


def test_an_expired_hold_that_still_fails_alerts_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alerts: list[tuple[list[str], str]],
) -> None:
    # First episode: announced, then held.
    _write_manifest(tmp_path, held_at=None)
    sweeps = _Sweeps(monkeypatch, tmp_path)
    sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(minutes=40))]
    sweeps.run()
    _write_manifest(tmp_path, held_at=_iso(_now() - dt.timedelta(hours=1)))
    sweeps.run()
    assert _kinds(alerts) == ["route-hold"]

    # 24h later the hold has expired. The deployed manifest still carries the
    # stale stamp (the lift rides a later snapshot), but an expired hold is
    # not live, and the route-health pass judges the hold the refresh would
    # place after lifting it. The re-probed route failed again: a new streak,
    # after the gap the hold left, is a new episode. It qualified right after
    # the lift, so it is a repeat hold: route-hold-repeat (a longer hold and a
    # nudge toward a permanent operator hold) instead of another route-hold.
    expired = _iso(_now() - dt.timedelta(hours=plan.HOLD_HOURS + 1))
    _write_manifest(tmp_path, held_at=expired)
    assert plan.applied_unserved_route_holds(manifest_dir=tmp_path) == set()
    second_episode = _iso(_now() - dt.timedelta(hours=7))
    sweeps.evidence = [_evidence(qualified_ago=dt.timedelta(minutes=30), first_failure_at=second_episode)]
    sweeps.run()
    sweeps.run()
    assert _kinds(alerts) == ["route-hold", "route-hold-repeat"]
    assert "hold 2" in alerts[-1][1] and "48h" in alerts[-1][1]
    assert "permanent operator hold" in alerts[-1][1]


def test_applied_holds_are_the_unexpired_unserved_holds(tmp_path: Path) -> None:
    now = _now()
    _write_manifest(tmp_path, held_at=_iso(now - dt.timedelta(hours=plan.HOLD_HOURS - 1)))
    assert plan.applied_unserved_route_holds(manifest_dir=tmp_path, now=now) == {ROUTE}
    assert plan.pending_unserved_route_holds({ROUTE}, manifest_dir=tmp_path, now=now) == set()
    _write_manifest(tmp_path, held_at=_iso(now - dt.timedelta(hours=plan.HOLD_HOURS)))
    assert plan.applied_unserved_route_holds(manifest_dir=tmp_path, now=now) == set()
    assert plan.pending_unserved_route_holds({ROUTE}, manifest_dir=tmp_path, now=now) == {ROUTE}
    _write_manifest(tmp_path, held_at=None)
    assert plan.applied_unserved_route_holds(manifest_dir=tmp_path, now=now) == set()


def test_apply_deadline_covers_a_snapshot_deploy_and_stays_under_the_hold() -> None:
    # Two refresh cycles to commit the hold (scheduled runs start one to two
    # hours apart), the longest a snapshot waits for a rollout, and a rollout.
    commit = dt.timedelta(hours=2)
    rollout = dt.timedelta(hours=1)
    snapshot_wait = dt.timedelta(seconds=price_snapshot_deploy.SNAPSHOT_MAX_WAIT_SECONDS)
    assert ROUTE_HOLD_APPLY_DEADLINE >= commit + snapshot_wait + rollout
    assert ROUTE_HOLD_APPLY_DEADLINE * 2 <= dt.timedelta(hours=plan.HOLD_HOURS)


def _sample(route: tuple[str, str], created: dt.datetime, *, status: str = "error") -> ProviderBenchmarkSample:
    provider, model = route
    return ProviderBenchmarkSample(
        id=f"{provider}-{model}-{created.isoformat()}-{status}", model=model, provider=provider,
        provider_name=provider, status=status, usage_type="Credits", streamed=True,
        error_type=None if status == "success" else "rate_limit_error",
        error_status=None if status == "success" else 429, source="synthetic",
        created_at=_iso(created),
    )


class _Store:
    def __init__(self, samples: list[ProviderBenchmarkSample]) -> None:
        self.samples = samples

    def provider_route_benchmark_samples(
        self, *, cutoff: str, per_route_limit: int, limit: int,
    ) -> list[ProviderBenchmarkSample]:
        rows = [sample for sample in self.samples if sample.created_at >= cutoff]
        return sorted(rows, key=lambda sample: sample.created_at, reverse=True)[:limit]


def test_the_episode_and_its_qualifying_time_hold_still_while_failures_continue() -> None:
    start = dt.datetime(2026, 11, 22, 15, 34, 15, tzinfo=dt.UTC)
    failures = [_sample(ROUTE, start + dt.timedelta(hours=2 * i)) for i in range(4)]
    # The sixth failure is the first that meets the six-sample, six-hour rule.
    failures += [_sample(ROUTE, start + dt.timedelta(hours=7)), _sample(ROUTE, start + dt.timedelta(hours=9))]
    qualifying = start + dt.timedelta(hours=9)
    assert qualifying - start >= UNAVAILABLE_MIN_SPAN

    def evaluate(samples: list[ProviderBenchmarkSample], now: dt.datetime) -> UnservedRoute:
        sibling = _sample(SIBLING, now - dt.timedelta(minutes=10), status="success")
        (route,) = evaluate_unserved_routes(_Store([*samples, sibling]), routes=[ROUTE], now=now)
        return route

    first = evaluate(failures, qualifying + dt.timedelta(minutes=30))
    later = evaluate(
        [*failures, _sample(ROUTE, qualifying + dt.timedelta(hours=3))],
        qualifying + dt.timedelta(hours=4),
    )
    assert first.kind == later.kind == "unavailable"
    assert first.first_failure_at == later.first_failure_at == _iso(start)
    assert first.qualified_at == later.qualified_at == _iso(qualifying)
    assert later.last_failure_at != first.last_failure_at
