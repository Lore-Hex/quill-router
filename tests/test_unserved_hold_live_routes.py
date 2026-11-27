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


def _both_sides(
    tmp_path: Path, evidence: set[tuple[str, str]], now: datetime,
    served: frozenset[tuple[str, str]] | None = None,
) -> tuple[set[str], set[str], list[str]]:
    """(what route health expects the next refresh to hold, what the refresh's
    step holds, the step's notes) on the same manifests at the same time,
    with the same served routes."""
    pending = {model for _provider, model in planner.pending_unserved_route_holds(
        evidence, manifest_dir=tmp_path, now=now, served=served,
    )}
    notes = step.apply_unserved_route_holds(evidence, manifest_dir=tmp_path, now=now, served=served)
    return pending, _held_ids(tmp_path / f"{PROVIDER}.json"), notes


def _routes(*models: str) -> frozenset[tuple[str, str]]:
    return frozenset((PROVIDER, model) for model in models)


def test_a_providers_last_live_route_is_not_held_once_its_other_rows_retired(tmp_path: Path, retire) -> None:
    # Three rows marked routable; two retire. The third is then the
    # provider's only route, and holding it would take the provider dark.
    path = _manifest(tmp_path, PROVIDER, [_row("vendor/old-1"), _row("vendor/old-2"), _row("vendor/current")])
    retire("vendor/old-1", "vendor/old-2")
    before = path.read_bytes()

    pending, held, notes = _both_sides(tmp_path, {(PROVIDER, "vendor/current")}, NOW)

    assert pending == held == set()
    assert path.read_bytes() == before
    assert any(f"{PROVIDER}: 1 of 1 routes unserved; provider-wide problem, holding none" in note for note in notes)


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
    assert any("3 of 4 routes unserved" in note for note in notes)

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
    assert any(f"{PROVIDER}: vendor/old is not a route this provider serves; not held" in note for note in notes)


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


# --- the routes a provider serves, not the rows its manifest lists -----------
#
# A manifest lists rows the catalog never serves: routes an operator holds,
# models the host may not resell, rows outside the host's allowlist. One
# provider listed about ten rows for every route it served.

_LISTED = [f"vendor/listed-{index}" for index in range(64)]
_SERVED = [f"vendor/served-{index}" for index in range(7)]


def _mostly_unserved(root: Path) -> Path:
    root.mkdir()
    return _manifest(root, PROVIDER, [_row(model) for model in (*_SERVED, *_LISTED)])


def test_every_served_route_of_a_provider_is_never_held_together(tmp_path: Path) -> None:
    served = _routes(*_SERVED)

    # All seven served routes are 7 of 71 rows, and all the provider serves.
    path = _mostly_unserved(tmp_path / "all")
    before = path.read_bytes()
    pending, held, notes = _both_sides(tmp_path / "all", _routes(*_SERVED), NOW, served)
    assert pending == held == set()
    assert path.read_bytes() == before
    assert any(f"{PROVIDER}: 7 of 7 routes unserved; provider-wide problem, holding none" in note for note in notes)

    # Four of seven is more than half of what it serves.
    _mostly_unserved(tmp_path / "four")
    pending, held, notes = _both_sides(tmp_path / "four", _routes(*_SERVED[:4]), NOW, served)
    assert pending == held == set()
    assert any("4 of 7 routes unserved" in note for note in notes)

    # Three of seven is not.
    _mostly_unserved(tmp_path / "three")
    pending, held, _notes = _both_sides(tmp_path / "three", _routes(*_SERVED[:3]), NOW, served)
    assert pending == held == set(_SERVED[:3])


def test_counting_every_listed_row_would_hold_them_all(tmp_path: Path) -> None:
    # What the same evidence does for a caller that has no catalog for these
    # manifests and so counts every live row: the reason callers that have
    # one must pass it.
    _mostly_unserved(tmp_path / "all")

    pending, held, _notes = _both_sides(tmp_path / "all", _routes(*_SERVED), NOW, None)

    assert pending == held == set(_SERVED)


def test_a_listed_row_the_provider_does_not_serve_is_not_held_and_is_named(tmp_path: Path) -> None:
    path = _mostly_unserved(tmp_path / "listed")
    before = path.read_bytes()

    pending, held, notes = _both_sides(tmp_path / "listed", _routes(_LISTED[0]), NOW, _routes(*_SERVED))

    assert pending == held == set()
    assert path.read_bytes() == before
    assert any(f"{PROVIDER}: {_LISTED[0]} is not a route this provider serves; not held" in note for note in notes)


def test_a_served_route_that_has_retired_is_not_served(tmp_path: Path, retire) -> None:
    # The catalog a caller read may be a moment older than the time it plans
    # for: a route in it that has since passed its retirement is not counted.
    (tmp_path / "retired").mkdir()
    _manifest(tmp_path / "retired", PROVIDER, [_row(model) for model in ("vendor/a", "vendor/b", "vendor/old")])
    retire("vendor/old")

    pending, held, notes = _both_sides(
        tmp_path / "retired", _routes("vendor/a"), NOW, _routes("vendor/a", "vendor/b", "vendor/old"),
    )

    # One of two, not one of three: half is allowed, and it is not the last.
    assert pending == held == {"vendor/a"}
    pending, held, notes = _both_sides(
        tmp_path / "retired", _routes("vendor/b"), NOW, _routes("vendor/a", "vendor/b", "vendor/old"),
    )
    assert pending == set() and held == {"vendor/a"}
    assert any("2 of 2 routes unserved" in note for note in notes)


def _held_row(model_id: str, held_at: datetime, **extra: Any) -> dict[str, Any]:
    return _row(
        model_id, routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
        unserved_held_at=held_at.strftime("%Y-%m-%dT%H:%M:%SZ"), unserved_hold_count=1, **extra,
    )


def test_a_route_whose_hold_just_lifted_counts_as_served(tmp_path: Path) -> None:
    # A catalog built while a route was held does not serve it. Once its hold
    # lifts it is a served route again, so two holds that ran out leave five
    # routes, and two of five may be held. Counted as three, two could not.
    ran_out = NOW - timedelta(hours=planner.HOLD_HOURS + 1)
    (tmp_path / "lifted").mkdir()
    _manifest(tmp_path / "lifted", PROVIDER, [
        _held_row("vendor/back-1", ran_out), _held_row("vendor/back-2", ran_out),
        _row("vendor/a"), _row("vendor/b"), _row("vendor/c"),
    ])

    pending, held, _notes = _both_sides(
        tmp_path / "lifted", _routes("vendor/a", "vendor/b"), NOW, _routes("vendor/a", "vendor/b", "vendor/c"),
    )

    assert pending == held == {"vendor/a", "vendor/b"}


def test_a_live_hold_still_counts_on_both_sides(tmp_path: Path) -> None:
    # Unchanged: a route this step already holds is unserved and is one of
    # the provider's routes, so the limit cannot ratchet a provider down.
    (tmp_path / "held").mkdir()
    _manifest(tmp_path / "held", PROVIDER, [
        _held_row("vendor/held", NOW - timedelta(hours=1)), _row("vendor/a"), _row("vendor/b"),
    ])

    pending, held, notes = _both_sides(
        tmp_path / "held", _routes("vendor/a"), NOW, _routes("vendor/a", "vendor/b"),
    )

    assert pending == set() and held == {"vendor/held"}
    assert any("2 of 3 routes unserved" in note for note in notes)


def test_a_held_row_past_its_retirement_is_no_longer_one_of_the_providers_routes(
    tmp_path: Path, retire,
) -> None:
    (tmp_path / "held").mkdir()
    _manifest(tmp_path / "held", PROVIDER, [
        _held_row("vendor/old", NOW - timedelta(hours=1)), _row("vendor/a"), _row("vendor/b"),
    ])
    retire("vendor/old")

    pending, held, _notes = _both_sides(
        tmp_path / "held", _routes("vendor/a"), NOW, _routes("vendor/a", "vendor/b"),
    )

    # One of two served routes, where the retired hold made it two of three.
    assert pending == {"vendor/a"} and held == {"vendor/a", "vendor/old"}


# --- served routes belong to one provider ------------------------------------

OTHER = "zeta"


def _two_sides(
    root: Path, evidence: set[tuple[str, str]], served: frozenset[tuple[str, str]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(what route health expects held, what the step holds), as routes."""
    pending = planner.pending_unserved_route_holds(evidence, manifest_dir=root, now=NOW, served=served)
    step.apply_unserved_route_holds(evidence, manifest_dir=root, now=NOW, served=served)
    held = {
        (provider, model) for provider in (PROVIDER, OTHER)
        for model in _held_ids(root / f"{provider}.json")
    }
    return pending, held


def test_a_model_one_provider_serves_is_not_served_by_another_that_lists_it(tmp_path: Path) -> None:
    # Both list the same seven models. One serves all seven, the other two.
    models = [f"vendor/m-{index}" for index in range(7)]
    for provider in (PROVIDER, OTHER):
        _manifest(tmp_path, provider, [_row(model) for model in models])
    served = _routes(*models) | {(OTHER, models[0]), (OTHER, models[1])}

    # Both routes the second provider serves: all of it, though two of seven
    # by the models the first one serves.
    pending, held = _two_sides(tmp_path, {(OTHER, models[0]), (OTHER, models[1])}, served)
    assert pending == held == set()

    # One of its two may be held; a model it only lists may not.
    pending, held = _two_sides(tmp_path, {(OTHER, models[0]), (OTHER, models[5])}, served)
    assert pending == held == {(OTHER, models[0])}


@pytest.mark.parametrize(("holder", "lister"), [(OTHER, PROVIDER), (PROVIDER, OTHER)],
                         ids=["holds-in-the-last-manifest", "holds-in-the-first-manifest"])
def test_a_lifted_hold_counts_for_its_own_provider_only(tmp_path: Path, holder: str, lister: str) -> None:
    # One manifest has two holds that ran out; the other lists one of those
    # models without serving it. The lifted routes are the first provider's
    # again and nobody else's, whichever manifest is read first.
    ran_out = NOW - timedelta(hours=planner.HOLD_HOURS + 1)
    _manifest(tmp_path, lister, [_row("vendor/back-1"), _row("vendor/x"), _row("vendor/y")])
    _manifest(tmp_path, holder, [
        _held_row("vendor/back-1", ran_out), _held_row("vendor/back-2", ran_out),
        _row("vendor/a"), _row("vendor/b"), _row("vendor/c"),
    ])
    served = frozenset(
        {(lister, model) for model in ("vendor/x", "vendor/y")}
        | {(holder, model) for model in ("vendor/a", "vendor/b", "vendor/c")}
    )
    evidence = {(holder, "vendor/a"), (holder, "vendor/b"), (lister, "vendor/back-1")}

    pending, held = _two_sides(tmp_path, evidence, served)

    # Two of the holding provider's five routes; nothing of the other's.
    assert pending == held == {(holder, "vendor/a"), (holder, "vendor/b")}


# --- the refresh's own run ---------------------------------------------------


def _evidence_file(path: Path, routes: list[str], now: datetime) -> Path:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({
        "version": 1,
        "generated_at": stamp,
        "routes": [
            {"provider": PROVIDER, "model": model, "samples": 6, "first_failure_at": stamp,
             "last_failure_at": stamp, "error_types": ["unsupported_route"]}
            for model in routes
        ],
    }))
    return path


def test_the_refresh_counts_the_routes_its_own_catalog_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    manifests = tmp_path / "manifests"
    path = _mostly_unserved(manifests)
    before = path.read_bytes()
    monkeypatch.setattr(step, "MANIFEST_DIR", manifests)
    monkeypatch.setattr(step, "served_routes", lambda: _routes(*_SERVED))
    monkeypatch.setattr(step, "quarantined_manifests", lambda: frozenset())
    evidence = _evidence_file(tmp_path / "evidence.json", _SERVED, datetime.now(UTC))

    assert step.main(["--json", str(evidence), "--manifest-dir", str(manifests)]) == 0

    assert path.read_bytes() == before
    assert "7 of 7 routes unserved; provider-wide problem, holding none" in capsys.readouterr().out


def test_the_refresh_holds_nothing_when_it_cannot_tell_what_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    ran_out = datetime.now(UTC) - timedelta(hours=planner.HOLD_HOURS + 1)
    path = _manifest(manifests, PROVIDER, [
        _held_row("vendor/back", ran_out), *[_row(f"vendor/live-{index}") for index in range(6)],
    ])
    monkeypatch.setattr(step, "MANIFEST_DIR", manifests)
    monkeypatch.setattr(step, "quarantined_manifests", lambda: frozenset())

    def unavailable() -> frozenset[tuple[str, str]]:
        raise RuntimeError("no catalog")

    monkeypatch.setattr(step, "served_routes", unavailable)
    evidence = _evidence_file(tmp_path / "evidence.json", ["vendor/live-0"], datetime.now(UTC))

    assert step.main(["--json", str(evidence), "--manifest-dir", str(manifests)]) == 0

    # Nothing new is held, and the hold that ran out still lifts.
    assert _held_ids(path) == set()
    # The run's summary is its stdout: it names this cause, and does not
    # blame the evidence, which in this run was read.
    summary = capsys.readouterr().out
    assert "served routes unavailable (RuntimeError); holding nothing" in summary
    assert "evidence unavailable" not in summary
    assert "lifted expired unserved-route hold on vendor/back" in summary


def test_a_run_on_other_manifests_does_not_read_this_checkouts_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "manifests"
    path = _mostly_unserved(manifests)
    monkeypatch.setattr(step, "quarantined_manifests", lambda: frozenset())

    def unexpected() -> frozenset[tuple[str, str]]:
        raise AssertionError("the checkout's catalog does not describe these manifests")

    monkeypatch.setattr(step, "served_routes", unexpected)
    evidence = _evidence_file(tmp_path / "evidence.json", _SERVED[:3], datetime.now(UTC))

    assert step.main(["--json", str(evidence), "--manifest-dir", str(manifests)]) == 0

    assert _held_ids(path) == set(_SERVED[:3])


def test_the_served_routes_are_the_ones_route_health_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.synthetic import route_health

    monkeypatch.setattr(route_health, "rotation_candidates", lambda: {
        "acme": ["vendor/a", "vendor/b"], "other": ["vendor/a"], "empty": [],
    })

    assert route_health.probed_routes() == {("acme", "vendor/a"), ("acme", "vendor/b"), ("other", "vendor/a")}
    assert step.served_routes() == route_health.probed_routes()
