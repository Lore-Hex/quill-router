"""Repeat unserved-route holds escalate instead of cycling every 24 hours.

Sentry QUILL-ROUTER-HT: nextbit-eu/google/gemma-3-27b-it answered HTTP 429 to
every probe from 2026-11-22. The provider-route-unserved hold held it for
24h, lifted it, and the route failed again at once; under a fixed 24h hold it
would hold, re-admit, fail and hold again indefinitely, with customer traffic
routed to it in each gap. Now a route that qualifies again within
REQUALIFY_WINDOW of its last hold lifting is held twice as long as last time
(24h, 48h, 96h, capped near a week), the count rides on the manifest row so it
survives refreshes, and the route-health alert asks for a permanent hold.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import unserved_route_holds as holds
from trusted_router import unserved_route_hold_plan as plan
from trusted_router.provider_manifest_policy import UNSERVED_ROUTE_HOLD_REASON
from trusted_router.synthetic import route_health
from trusted_router.synthetic.route_health import (
    ROUTE_HOLD_APPLY_DEADLINE,
    UNAVAILABLE_MIN_SPAN,
    RouteHealthFlag,
    UnservedRoute,
    report_route_holds,
)

NOW = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.UTC)
PROVIDER = "nextbit-eu"
MODEL = "google/gemma-3-27b-it"
ROUTE = (PROVIDER, MODEL)
SIBLINGS = ["google/gemma-3-12b-it", "google/gemma-4-26b-a4b-it", "qwen/qwen3-14b"]


@pytest.fixture(autouse=True)
def _no_route_here_retires(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests replay real incidents under their real route names, some at
    the wall clock. The hold's planner does not hold a route past its scheduled
    retirement, so a retirement announced later for one of these routes would
    turn them red at its cutover instant, on main, with nothing changed.
    Retirement is covered in tests/test_unserved_hold_live_routes.py; here the
    planner sees no retirement."""
    monkeypatch.setattr(plan, "provider_model_retired", lambda *args, **kwargs: False)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _row(model_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": model_id, "model_type": "chat", "endpoints": ["chat/completions"],
        "input_token_price_per_m": 100_000, "output_token_price_per_m": 200_000, **extra,
    }


def _manifest(tmp_path: Path, target: dict[str, Any] | None = None) -> Path:
    path = tmp_path / f"{PROVIDER}.json"
    rows = [target or _row(MODEL), *(_row(model) for model in SIBLINGS)]
    path.write_text(json.dumps({"provider": PROVIDER, "models": rows}), encoding="utf-8")
    return path


def _target(path: Path) -> dict[str, Any]:
    return next(row for row in json.loads(path.read_text())["models"] if row["id"] == MODEL)


def _hold(tmp_path: Path, at: dt.datetime) -> list[str]:
    return holds.apply_unserved_route_holds({ROUTE}, manifest_dir=tmp_path, now=at)


def _sweep(tmp_path: Path, at: dt.datetime) -> list[str]:
    """An hourly refresh with no evidence for the route (held routes are not
    probed, and a recovered route is not listed)."""
    return holds.apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=at)


def _live(tmp_path: Path, at: dt.datetime) -> bool:
    return ROUTE in plan.applied_unserved_route_holds(manifest_dir=tmp_path, now=at)


def test_hold_lengths_double_from_24h_and_cap_near_a_week() -> None:
    assert [plan.unserved_hold_hours(count) for count in range(1, 8)] == [24, 48, 96, 168, 168, 168, 168]
    assert plan.unserved_hold_hours(10_000) == plan.MAX_HOLD_HOURS == 7 * 24
    assert plan.unserved_hold_hours(0) == plan.HOLD_HOURS == 24


def test_a_first_hold_lasts_24h(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    notes = _hold(tmp_path, NOW)
    row = _target(path)
    assert (row["routable"], row["routable_reason"]) == (False, UNSERVED_ROUTE_HOLD_REASON)
    assert row[holds.HELD_AT_FIELD] == _iso(NOW)
    assert row[holds.HOLD_COUNT_FIELD] == 1
    assert holds.LIFTED_AT_FIELD not in row
    assert any(f"held {MODEL} ({UNSERVED_ROUTE_HOLD_REASON}) for 24h" in note for note in notes)
    assert not any("repeat hold" in note for note in notes)
    assert _live(tmp_path, NOW + dt.timedelta(hours=23, minutes=59))
    assert not _live(tmp_path, NOW + dt.timedelta(hours=24))


def test_a_lift_keeps_the_count_and_records_when_it_lifted(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    _hold(tmp_path, NOW)
    lifted_at = NOW + dt.timedelta(hours=24, minutes=31)
    notes = _sweep(tmp_path, lifted_at)
    row = _target(path)
    assert row["routable"] is True
    assert {"routable_reason", holds.HELD_AT_FIELD}.isdisjoint(row)
    assert row[holds.HOLD_COUNT_FIELD] == 1
    assert row[holds.LIFTED_AT_FIELD] == _iso(lifted_at)
    assert any("lifted expired" in note for note in notes)


def test_a_quick_requalification_doubles_the_hold(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    _hold(tmp_path, NOW)
    lifted_at = NOW + dt.timedelta(hours=24, minutes=31)
    _sweep(tmp_path, lifted_at)
    # The lift deploys, the route fails for the six-hour outage rule again.
    again = lifted_at + dt.timedelta(hours=10)
    notes = _hold(tmp_path, again)
    row = _target(path)
    assert row[holds.HOLD_COUNT_FIELD] == 2
    assert row[holds.HELD_AT_FIELD] == _iso(again)
    assert holds.LIFTED_AT_FIELD not in row
    assert any("for 48h (repeat hold 2; consider a permanent operator hold)" in note for note in notes)
    assert _live(tmp_path, again + dt.timedelta(hours=47, minutes=59))
    assert not _live(tmp_path, again + dt.timedelta(hours=48))


def test_lift_and_rehold_in_one_refresh_also_escalates(tmp_path: Path) -> None:
    # The refresh lifts first, then holds: an expired hold whose route is
    # still listed is re-held at once, which is the quickest re-qualification.
    path = _manifest(tmp_path)
    _hold(tmp_path, NOW)
    _hold(tmp_path, NOW + dt.timedelta(hours=25))
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 2


def test_repeat_holds_keep_doubling_up_to_the_cap(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    at = NOW
    lengths = []
    for _ in range(6):
        _hold(tmp_path, at)
        hours = plan.unserved_hold_hours(_target(path)[holds.HOLD_COUNT_FIELD])
        lengths.append(hours)
        lifted = at + dt.timedelta(hours=hours, minutes=30)
        assert not _live(tmp_path, lifted)
        _sweep(tmp_path, lifted)
        at = lifted + dt.timedelta(hours=8)
    assert lengths == [24, 48, 96, 168, 168, 168]
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 6


def test_a_route_healthy_for_longer_than_the_window_starts_over(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    _hold(tmp_path, NOW)
    _hold(tmp_path, NOW + dt.timedelta(hours=25))
    lifted_at = NOW + dt.timedelta(hours=25 + 48, minutes=30)
    _sweep(tmp_path, lifted_at)
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 2

    # Still inside the window: the record stays.
    _sweep(tmp_path, lifted_at + plan.REQUALIFY_WINDOW)
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 2
    # Served through the whole window: the next refresh forgets the record.
    healthy = lifted_at + plan.REQUALIFY_WINDOW + dt.timedelta(hours=1)
    _sweep(tmp_path, healthy)
    assert {holds.HOLD_COUNT_FIELD, holds.LIFTED_AT_FIELD}.isdisjoint(_target(path))

    notes = _hold(tmp_path, healthy + dt.timedelta(days=3))
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 1
    assert any("for 24h" in note for note in notes)


def test_a_late_requalification_starts_over_even_before_cleanup(tmp_path: Path) -> None:
    lifted_at = NOW - plan.REQUALIFY_WINDOW - dt.timedelta(minutes=1)
    path = _manifest(tmp_path, _row(MODEL, **{
        holds.HOLD_COUNT_FIELD: 3, holds.LIFTED_AT_FIELD: _iso(lifted_at),
    }))
    _hold(tmp_path, NOW)
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 1


@pytest.mark.parametrize("record", [
    {holds.HOLD_COUNT_FIELD: 3},                                             # no lift stamp
    {holds.HOLD_COUNT_FIELD: 3, holds.LIFTED_AT_FIELD: "yesterday"},         # malformed
    {holds.HOLD_COUNT_FIELD: 3, holds.LIFTED_AT_FIELD: _iso(NOW + dt.timedelta(hours=2))},  # future
    {holds.HOLD_COUNT_FIELD: "3", holds.LIFTED_AT_FIELD: _iso(NOW)},         # malformed count
    {holds.HOLD_COUNT_FIELD: True, holds.LIFTED_AT_FIELD: _iso(NOW)},
])
def test_an_unreadable_record_never_lengthens_a_hold(tmp_path: Path, record: dict[str, Any]) -> None:
    path = _manifest(tmp_path, _row(MODEL, **record))
    _hold(tmp_path, NOW)
    expected = 2 if record[holds.HOLD_COUNT_FIELD] in ("3", True) else 1
    # A malformed count with a good stamp reads as a single earlier hold.
    assert _target(path)[holds.HOLD_COUNT_FIELD] == expected


def test_a_malformed_held_count_reads_as_a_first_hold(tmp_path: Path) -> None:
    held = _row(MODEL, routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON, **{
        holds.HELD_AT_FIELD: _iso(NOW - dt.timedelta(hours=30)), holds.HOLD_COUNT_FIELD: "9",
    })
    _manifest(tmp_path, held)
    assert not _live(tmp_path, NOW)


def test_the_record_survives_a_provider_manifest_refresh(tmp_path: Path) -> None:
    # The shared discovery writer rebuilds rows from the old row plus the
    # fresh discovery; the repeat-hold record must ride along, held or not.
    from scripts.pricing.base import ModelPrice, ProviderPricingResult
    from scripts.pricing.manifest import write_discovered_chat_manifest

    path = _manifest(tmp_path)
    _hold(tmp_path, NOW)
    _sweep(tmp_path, NOW + dt.timedelta(hours=24, minutes=30))
    before = _target(path)
    models = [MODEL, *SIBLINGS]
    result = ProviderPricingResult(
        slug=PROVIDER, prices={model: ModelPrice(100_000, 200_000) for model in models},
        source="api", fetched_url="https://example.invalid/models",
    )
    write_discovered_chat_manifest(
        result, manifest_path=path, discovered_rows={model: {"id": model} for model in models},
        source_url="https://example.invalid/models",
    )
    after = _target(path)
    assert after[holds.HOLD_COUNT_FIELD] == before[holds.HOLD_COUNT_FIELD] == 1
    assert after[holds.LIFTED_AT_FIELD] == before[holds.LIFTED_AT_FIELD]
    assert _hold(tmp_path, NOW + dt.timedelta(hours=34))
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 2


def test_safety_caps_still_refuse_a_repeat_hold(tmp_path: Path) -> None:
    # A repeat hold is still a hold: the provider-fraction cap applies.
    path = tmp_path / "small.json"
    rows = [_row("m/a", **{holds.HOLD_COUNT_FIELD: 2, holds.LIFTED_AT_FIELD: _iso(NOW)}), _row("m/b")]
    path.write_text(json.dumps({"provider": "small", "models": rows}), encoding="utf-8")
    notes = holds.apply_unserved_route_holds({("small", "m/a"), ("small", "m/b")}, manifest_dir=tmp_path, now=NOW)
    assert all(row.get("routable") is not False for row in json.loads(path.read_text())["models"])
    assert any("provider-wide problem" in note for note in notes)


def test_pending_counts_match_what_the_refresh_will_write(tmp_path: Path) -> None:
    # The route-health pass judges the deployed manifest: here the hold has
    # expired but its lift has not deployed yet.
    held = _row(MODEL, routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON, **{
        holds.HELD_AT_FIELD: _iso(NOW - dt.timedelta(hours=25)), holds.HOLD_COUNT_FIELD: 1,
    })
    path = _manifest(tmp_path, held)
    assert plan.pending_unserved_route_hold_counts({ROUTE}, manifest_dir=tmp_path, now=NOW) == {ROUTE: 2}
    assert plan.pending_unserved_route_holds({ROUTE}, manifest_dir=tmp_path, now=NOW) == {ROUTE}
    _hold(tmp_path, NOW)
    assert _target(path)[holds.HOLD_COUNT_FIELD] == 2

    # A route never held, or whose record has gone stale, is a first hold.
    _manifest(tmp_path)
    assert plan.pending_unserved_route_hold_counts({ROUTE}, manifest_dir=tmp_path, now=NOW) == {ROUTE: 1}


def test_requalify_window_covers_a_lift_deploy_and_the_outage_rule() -> None:
    # A lift reaches production about as slowly as a hold does, then the route
    # must fail for the outage rule's span before it qualifies again, and the
    # next refresh places the hold.
    refresh = dt.timedelta(hours=2)
    assert plan.REQUALIFY_WINDOW >= ROUTE_HOLD_APPLY_DEADLINE + UNAVAILABLE_MIN_SPAN + refresh
    # Escalated holds are longer, never shorter, so a held route's probe gap
    # still separates episodes.
    assert route_health.UNSERVED_MAX_PROBE_GAP < dt.timedelta(hours=plan.HOLD_HOURS)


# --- route-health alert -------------------------------------------------------


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    import sentry_sdk

    sent: list[tuple[list[str], str]] = []

    def capture(message: str, *_args: object, **_kwargs: object) -> None:
        fingerprint = list(sentry_sdk.get_current_scope()._fingerprint or [])  # noqa: SLF001
        if fingerprint[:1] == ["ops-alert"]:
            sent.append((fingerprint[1:], message))

    monkeypatch.setattr(sentry_sdk, "capture_message", capture)
    return sent


def _flag() -> RouteHealthFlag:
    return RouteHealthFlag(*ROUTE, 6, 6, 1.0, "rate_limit_error", None,
                           kind="availability", newest_error_status=429)


def _evidence(*, qualified_ago: dt.timedelta = dt.timedelta(minutes=30)) -> UnservedRoute:
    qualified = _iso(NOW - qualified_ago)
    return UnservedRoute(*ROUTE, samples=6, first_failure_at=_iso(NOW - dt.timedelta(hours=7)),
                         last_failure_at=qualified, error_types=("rate_limit_error",),
                         kind="unavailable", qualified_at=qualified)


def test_a_first_hold_announces_route_hold_for_24h(alerts: list[tuple[list[str], str]]) -> None:
    report_route_holds([_flag()], {ROUTE: _evidence()}, {ROUTE: 1}, now=NOW)
    assert [fingerprint for fingerprint, _ in alerts] == [["route-hold", PROVIDER, MODEL]]
    assert "for 24h" in alerts[0][1]


def test_escalation_raises_a_distinct_alert_asking_for_a_permanent_hold(
    alerts: list[tuple[list[str], str]],
) -> None:
    report_route_holds([_flag()], {ROUTE: _evidence()}, {ROUTE: 3}, now=NOW)
    assert [fingerprint for fingerprint, _ in alerts] == [["route-hold-repeat", PROVIDER, MODEL]]
    message = alerts[0][1]
    assert "hold 3" in message and "96h instead of 48h" in message
    assert "permanent operator hold" in message and "_OPERATOR_HOLD_REASONS" in message
    assert "HTTP 429" in message


def test_an_overdue_repeat_hold_still_says_it_has_not_applied(alerts: list[tuple[list[str], str]]) -> None:
    late = _evidence(qualified_ago=ROUTE_HOLD_APPLY_DEADLINE + dt.timedelta(minutes=5))
    report_route_holds([_flag()], {ROUTE: late}, {ROUTE: 2}, now=NOW)
    assert [fingerprint[0] for fingerprint, _ in alerts] == ["route-hold-repeat", "route-hold-not-applied"]


def test_the_repeat_alert_is_claimed_once_per_episode(alerts: list[tuple[list[str], str]]) -> None:
    from trusted_router.synthetic.alerts import alerts_once

    claimed: set[str] = set()

    def claim(key: list[str]) -> bool:
        joined = "|".join(key)
        if joined in claimed:
            return False
        claimed.add(joined)
        return True

    with alerts_once(claim):
        for hours_later in range(4):
            report_route_holds([_flag()], {ROUTE: _evidence()}, {ROUTE: 2},
                               now=NOW + dt.timedelta(hours=hours_later, minutes=1))
    assert [fingerprint[0] for fingerprint, _ in alerts] == ["route-hold-repeat"]
