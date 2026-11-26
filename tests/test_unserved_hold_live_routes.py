"""The unserved-route hold counts only routes a provider still serves.

A scheduled retirement stops a route at its time without touching the
manifest row, which stays marked routable. The hold's limits (never a
provider's last route, never more than half of a provider) are about routes
customers can still use, so a retired row is neither a route to hold nor one
that counts as still served. The refresh's step and the route-health pass
share the planner and must give the same answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import unserved_route_holds as step
from trusted_router import provider_lifecycle
from trusted_router import unserved_route_hold_plan as planner
from trusted_router.provider_manifest_policy import UNSERVED_ROUTE_HOLD_REASON

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
RETIRED_AT = NOW - timedelta(days=11)
PROVIDER = "acme"


def _row(model_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": model_id, "model_type": "chat", "endpoints": ["chat/completions"],
        "input_token_price_per_m": 1_000_000, "output_token_price_per_m": 2_000_000, **extra,
    }


def _manifest(root: Path, provider: str, rows: list[dict[str, Any]]) -> Path:
    path = root / f"{provider}.json"
    path.write_text(json.dumps({"provider": provider, "models": rows}, indent=2) + "\n")
    return path


def _held_ids(path: Path) -> set[str]:
    return {
        row["id"] for row in json.loads(path.read_text())["models"]
        if row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
    }


@pytest.fixture
def retire(monkeypatch: pytest.MonkeyPatch):
    """Schedule retirements of ``PROVIDER`` routes at RETIRED_AT, by model id or
    by upstream id, through the lifecycle's own record and lookup."""
    def schedule(*model_ids: str, upstream_ids: tuple[str, ...] = ()) -> None:
        record = provider_lifecycle._Retirement(
            provider=PROVIDER, model_ids=frozenset(model_ids),
            upstream_ids=frozenset(upstream_ids), effective_at=RETIRED_AT,
        )
        monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (*provider_lifecycle._RETIREMENTS, record))

    return schedule


def _both_sides(tmp_path: Path, evidence: set[tuple[str, str]], now: datetime) -> tuple[set[str], set[str], list[str]]:
    """(what route health expects the next refresh to hold, what the refresh's
    step holds, the step's notes) on the same manifests at the same time."""
    pending = {model for _provider, model in planner.pending_unserved_route_holds(
        evidence, manifest_dir=tmp_path, now=now,
    )}
    notes = step.apply_unserved_route_holds(evidence, manifest_dir=tmp_path, now=now)
    return pending, _held_ids(tmp_path / f"{PROVIDER}.json"), notes


def test_a_providers_last_live_route_is_not_held_once_its_other_rows_retired(tmp_path: Path, retire) -> None:
    # Three rows marked routable; two retire. The third is then the
    # provider's only route, and holding it would take the provider dark.
    path = _manifest(tmp_path, PROVIDER, [_row("vendor/old-1"), _row("vendor/old-2"), _row("vendor/current")])
    retire("vendor/old-1", "vendor/old-2")
    before = path.read_bytes()

    pending, held, notes = _both_sides(tmp_path, {(PROVIDER, "vendor/current")}, NOW)

    assert pending == held == set()
    assert path.read_bytes() == before
    assert any(f"{PROVIDER}: 1 of 1 rows unserved; provider-wide problem, holding none" in note for note in notes)


def test_the_same_route_is_held_until_the_retirement_takes_effect(tmp_path: Path, retire) -> None:
    _manifest(tmp_path, PROVIDER, [_row("vendor/old-1"), _row("vendor/old-2"), _row("vendor/current")])
    retire("vendor/old-1", "vendor/old-2")

    pending, held, _notes = _both_sides(
        tmp_path, {(PROVIDER, "vendor/current")}, RETIRED_AT - timedelta(seconds=1),
    )

    assert pending == held == {"vendor/current"}


def test_half_of_a_provider_is_measured_over_the_routes_it_still_serves(tmp_path: Path, retire) -> None:
    live = [f"vendor/live-{index}" for index in range(4)]
    gone = [f"vendor/gone-{index}" for index in range(4)]
    evidence = {(PROVIDER, model) for model in live[:3]}

    # Three of eight marked rows, but three of the four that serve anything.
    retired = tmp_path / "retired"
    retired.mkdir()
    _manifest(retired, PROVIDER, [_row(model) for model in (*live, *gone)])
    retire(*gone)
    pending, held, notes = _both_sides(retired, evidence, NOW)
    assert pending == held == set()
    assert any("3 of 4 rows unserved" in note for note in notes)

    # Two of those four is half, which the step allows.
    pending, held, _notes = _both_sides(retired, {(PROVIDER, model) for model in live[:2]}, NOW)
    assert pending == held == set(live[:2])


def test_without_the_retirement_the_same_three_rows_are_held(tmp_path: Path) -> None:
    live = [f"vendor/live-{index}" for index in range(4)]
    gone = [f"vendor/gone-{index}" for index in range(4)]
    _manifest(tmp_path, PROVIDER, [_row(model) for model in (*live, *gone)])

    pending, held, _notes = _both_sides(tmp_path, {(PROVIDER, model) for model in live[:3]}, NOW)

    assert pending == held == set(live[:3])


def test_a_retired_row_is_not_held_and_is_named(tmp_path: Path, retire) -> None:
    path = _manifest(tmp_path, PROVIDER, [_row("vendor/old"), *[_row(f"vendor/live-{index}") for index in range(3)]])
    retire("vendor/old")
    before = path.read_bytes()

    pending, held, notes = _both_sides(tmp_path, {(PROVIDER, "vendor/old")}, NOW)

    assert pending == held == set()
    assert path.read_bytes() == before
    assert any(f"{PROVIDER}: vendor/old is not a routable manifest row; not held" in note for note in notes)


def test_a_retirement_by_upstream_id_counts_too(tmp_path: Path, retire) -> None:
    _manifest(tmp_path, PROVIDER, [
        _row("vendor/old", upstream_id="Old-Native-Name"), _row("vendor/current", upstream_id="Current"),
    ])
    retire(upstream_ids=("Old-Native-Name",))

    pending, held, _notes = _both_sides(tmp_path, {(PROVIDER, "vendor/current")}, NOW)

    assert pending == held == set()


def test_another_providers_retirement_of_the_same_model_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    _manifest(tmp_path, PROVIDER, [_row("vendor/shared"), _row("vendor/current")])
    record = provider_lifecycle._Retirement(
        provider="elsewhere", model_ids=frozenset({"vendor/shared"}),
        upstream_ids=frozenset(), effective_at=RETIRED_AT,
    )
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (*provider_lifecycle._RETIREMENTS, record))

    pending, held, _notes = _both_sides(tmp_path, {(PROVIDER, "vendor/current")}, NOW)

    assert pending == held == {"vendor/current"}


def test_every_scheduled_retirement_is_seen_by_the_planner() -> None:
    # The registry's own records, whatever they are today: just before each
    # takes effect its rows count as served; from that time they do not.
    # A row an earlier record already retired is skipped for the "before" half.
    checked = 0
    for record in provider_lifecycle.provider_retirements():
        before, at = record.effective_at - timedelta(seconds=1), record.effective_at
        for model_id in sorted(record.model_ids):
            row = _row(model_id)
            assert not planner._live(record.provider, row, at), (record.provider, model_id)
            if not provider_lifecycle.provider_model_retired(record.provider, model_id, at=before):
                assert planner._live(record.provider, row, before), (record.provider, model_id)
                checked += 1
        for upstream_id in sorted(record.upstream_ids):
            row = _row("vendor/any-name", upstream_id=upstream_id)
            assert not planner._live(record.provider, row, at), (record.provider, upstream_id)
    assert checked


def test_the_planners_route_list_is_what_it_holds_from(tmp_path: Path, retire) -> None:
    rows = [
        _row("vendor/old"), _row("vendor/a"), _row("vendor/dark", routable=False),
        _row("vendor/b"), _row("vendor/a"), "not a row", {"no": "id"}, _row("vendor/c"), _row("vendor/d"),
    ]
    retire("vendor/old")

    ids = planner.live_route_ids(PROVIDER, rows, now=NOW)

    assert ids == ["vendor/a", "vendor/b", "vendor/c", "vendor/d"]
    assert planner.live_route_ids(PROVIDER, rows, now=RETIRED_AT - timedelta(seconds=1))[0] == "vendor/old"
    # Every listed route can be held, in halves; nothing outside the list can.
    for half in (ids[:2], ids[2:]):
        planned, _notes = planner.plan_unserved_route_holds(
            {(PROVIDER, model) for model in half}, [(PROVIDER, rows)], manifest_stems=[PROVIDER], now=NOW,
        )
        assert sorted(row["id"] for _index, row in planned) == half
    planned, _notes = planner.plan_unserved_route_holds(
        {(PROVIDER, "vendor/old"), (PROVIDER, "vendor/dark")}, [(PROVIDER, rows)],
        manifest_stems=[PROVIDER], now=NOW,
    )
    assert planned == []


def test_a_row_that_is_not_routable_is_not_live_whatever_the_registry_says() -> None:
    assert not planner._live(PROVIDER, _row("vendor/dark", routable=False), NOW)
    assert planner._live(PROVIDER, _row("vendor/lit"), NOW)
    assert planner._live(PROVIDER, _row("vendor/lit", upstream_id=None), NOW)
