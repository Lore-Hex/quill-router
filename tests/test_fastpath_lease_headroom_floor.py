"""The lease-headroom 503 is held to the fast path nodes' floor
(fastpath_lease_headroom_503_floor_micro; docs/design/fast-admission-widening.md
(b) and section 11 item 7).

The 503 says "retry shortly", which is true while the fast path can still
fund the workspace: a lease is topped up, or closes and gives back what it
holds. A node tops no lease up unless the headroom outside leases is at
least its floor plus the amount asked, and a lease gives back what it holds
only when it closes (ten minutes idle, an hour old, or its workspace off).
So a workspace whose headroom, counting what its leases hold, is under the
floor can spend the rest of its credit before a retry could succeed. On
2026-11-21, the day after the floor went from $20 to $2, a workspace within
cents of empty was answered the 503 on every request for three minutes,
until it bought credit. Under the floor the answer is the
insufficient_credits 402: what the same rows get with the 503 off.

The rule moves an answer one way only, 503 to 402, and only for a hold that
was refused already. The differential runs the same rows and leases with
the 503 off, with it on and no floor, and with it on at each floor: the
money, the rows and the memory are the same in all three, and the answers
differ from no-floor only where the lease-aware total is under the floor.
Ids are synthetic.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_fastpath_enabler_deploy import ROOT, _nodes_floor
from tests.test_fastpath_lease_headroom_503 import (
    CASES,
    LEASE_HELD_CASES,
    OTHER_WS,
    WS,
    Answer,
    Case,
    _authorize,
    _authorize_route,
    _count_transactions,
    _enclave_caches,
    _fail_lease_reads,
    _route_body,
    _route_request,
    _route_store,
    _run,
    _seed,
    _seed_leases,
    _track_lease_reads,
)
from tests.test_gateway_authorize_spanner_operations import (
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router import storage_gcp_authorize as billing
from trusted_router.config import Settings
from trusted_router.credit_refusal_read import CREDIT_REFUSAL_READ_HEADER, LeaseUnderFloor
from trusted_router.routes.internal import gateway
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

ACCEPTED = billing.AuthorizeOutcome.ACCEPTED
REFUSED = billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
LEASE_503 = billing.AuthorizeOutcome.LEASE_HEADROOM

# The nodes' floor since 2026-11-20, and rollout.sh's literal for it.
NODES_FLOOR = 2_000_000

# Floors across the matrix's amounts (its balances are 1,000): none, under
# every total, between them, exactly the largest, one over it, and far over.
MATRIX_FLOORS = (0, 1, 300, 500, 700, 900, 1_000, 1_001, 5_000)


def _total(case: Case, admitted: int = 0) -> int:
    """The lease-aware total: the rows' signed headroom, less the holds this
    run admitted, plus what the live leases hold."""
    return sum(case.headroom()) - admitted * case.estimate + case.held


def _expected(case: Case, off: Answer, remembered: bool, floor: int) -> tuple[tuple[str, ...], bool]:
    """The rule, written down independently of the code, from the answers
    with the 503 off (which tests/test_fastpath_lease_headroom_503.py pins
    to today's rules): a refusal for an unmarked workspace whose live leases
    hold a positive amount that, with the rows, covers the hold is the 503
    when that total is also at least the floor, and stays the 402 under it.
    Either refusal remembers the workspace; an admission forgets it."""
    outcomes, admitted = [], 0
    for outcome in off.outcomes:
        total = _total(case, admitted)
        if (
            outcome == REFUSED
            and not case.marked
            and case.held > 0
            and total >= case.estimate
            and total >= floor
        ):
            outcome = LEASE_503
        admitted += outcome == ACCEPTED
        outcomes.append(outcome)
    memory = remembered
    for outcome in outcomes:
        memory = outcome in (REFUSED, LEASE_503)
    return tuple(outcomes), memory


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c))
def test_a_floor_moves_only_a_503_under_it_to_the_402(case: Case) -> None:
    for remembered in (False, True):
        off = _run(case, on=False, remembered=remembered)
        no_floor = _run(case, on=True, remembered=remembered)
        for floor in MATRIX_FLOORS:
            on = _run(case, on=True, remembered=remembered, floor=floor)
            # The money is the same whichever refusal is returned: nothing is
            # held, written or admitted that was not with the 503 off.
            assert on.rows == off.rows == no_floor.rows, floor
            assert on.reservations == off.reservations == no_floor.reservations, floor
            outcomes, memory = _expected(case, off, remembered, floor)
            assert on.outcomes == outcomes, floor
            assert on.remembered == memory == no_floor.remembered, floor
            for before, after, without in zip(off.outcomes, on.outcomes, no_floor.outcomes, strict=True):
                # Against no floor: the same answer, or the 503 turned back
                # into the 402 the rows get with the 503 off. Never an
                # admission, never a refusal of a request admitted before.
                assert after == without or (without, after, before) == (LEASE_503, REFUSED, REFUSED), floor
                assert (after == ACCEPTED) == (before == ACCEPTED) == (without == ACCEPTED), floor
            if floor == 0:
                # The default: every answer, row and memory as before the
                # setting existed.
                assert on == no_floor
            if LEASE_503 not in on.outcomes:
                # No 503 left: all of it is the answer with the 503 off.
                assert on == off, floor


def test_the_matrix_reaches_both_sides_of_a_floor() -> None:
    # Positive control on the sweep above: lease-held cases sit on each side
    # of some floor, on one shard and on two, and a floor above every total
    # leaves no 503 at all.
    for shards in (1, 2):
        held = [case for case in LEASE_HELD_CASES if len(case.credits) == shards]
        assert any(_total(case) >= 700 for case in held)
        assert any(_total(case) < 700 for case in held)
    assert all(_total(case) <= 1_000 for case in LEASE_HELD_CASES)
    changed = kept = 0
    for case in LEASE_HELD_CASES:
        on = _run(case, on=True, floor=700)
        assert on.outcomes == ((LEASE_503,) * 2 if _total(case) >= 700 else (REFUSED,) * 2)
        changed += on.outcomes == (REFUSED,) * 2
        kept += on.outcomes == (LEASE_503,) * 2
        assert _run(case, on=True, floor=5_000).outcomes == (REFUSED,) * 2
        assert _run(case, on=True, floor=0).outcomes == (LEASE_503,) * 2
    assert changed and kept


@pytest.mark.parametrize("case", LEASE_HELD_CASES, ids=lambda c: repr(c))
def test_under_the_floor_the_answer_is_the_one_with_the_503_off(case: Case) -> None:
    """Under the floor everything is as with the 503 off: the answers, the
    rows, the reservations and the memory, on a first refusal (the
    transaction's) and on a remembered one, which stays the lock-free
    precheck's, with no transaction."""
    floor = _total(case) + 1
    for remembered in (False, True):
        off = _run(case, on=False, remembered=remembered)
        on = _run(case, on=True, remembered=remembered, floor=floor)
        assert on == off
        assert on.outcomes == (REFUSED,) * 2
        assert on.remembered
        # How many transactions a first refusal runs is the shard prefix's
        # chance on two shards; whether it runs any is not.
        assert on.transactions[1] == off.transactions[1] == 0
        assert (on.transactions[0] == 0) == (off.transactions[0] == 0) == remembered
    # At the floor exactly, the 503 as before.
    assert _run(case, on=True, floor=_total(case)).outcomes == (LEASE_503,) * 2


def test_off_reads_no_lease_and_no_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the 503 off the floor is not read: no lease statement runs and
    every answer is the same at any floor."""
    executed = _track_lease_reads(monkeypatch)
    for case in LEASE_HELD_CASES:
        for remembered in (False, True):
            assert _run(case, on=False, remembered=remembered, floor=5_000) == _run(
                case, on=False, remembered=remembered
            )
    assert executed == []
    # Positive control: on, the same cases read their leases.
    _run(LEASE_HELD_CASES[0], on=True, floor=5_000)
    assert executed


# The precheck: the one read both paths stand on.


def _precheck_store(
    credits: tuple[int, ...], usage: tuple[int, ...], reserved: tuple[int, ...],
    leases: tuple[Any, ...],
) -> tuple[Any, Any]:
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store(request_record_write_mode="typed")
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(credits):
        table[(WS, shard)] = {
            "workspace_id": WS, "shard": shard, "total_credits": total,
            "total_usage": usage[shard], "reserved": reserved[shard],
        }
    _seed_leases(database, WS, leases)
    _seed_leases(database, OTHER_WS, (("open", ((0, 10_000_000, 0),)),), prefix="other")
    return store, database


@pytest.mark.parametrize(
    ("credits", "usage", "reserved", "leases", "estimate", "floor", "verdict", "under"),
    [
        # 100 outside leases, 900 held, a hold of 450: the total is 1,000.
        ((1_000,), (0,), (900,), (("open", ((0, 900, 0),)),), 450, 0, billing.LEASE_HELD, None),
        ((1_000,), (0,), (900,), (("open", ((0, 900, 0),)),), 450, 999, billing.LEASE_HELD, None),
        # Exactly the floor counts.
        ((1_000,), (0,), (900,), (("open", ((0, 900, 0),)),), 450, 1_000, billing.LEASE_HELD, None),
        # One under it does not: the rows' own verdict, with the amounts.
        ((1_000,), (0,), (900,), (("open", ((0, 900, 0),)),), 450, 1_001, billing.EXHAUSTED, (100, 900)),
        ((1_000,), (1,), (900,), (("open", ((0, 900, 0),)),), 450, 1_000, billing.EXHAUSTED, (99, 900)),
        # A draining lease's remaining allocation counts as an open one's.
        ((1_000,), (0,), (900,), (("draining", ((0, 900, 450),)),), 450, 551, billing.EXHAUSTED, (100, 450)),
        ((1_000,), (0,), (900,), (("draining", ((0, 900, 450),)),), 450, 550, billing.LEASE_HELD, None),
        # Signed: rows overdrawn by 50, leases holding 500, total 450.
        ((1_000,), (550,), (500,), (("open", ((0, 500, 0),)),), 400, 451, billing.EXHAUSTED, (-50, 500)),
        ((1_000,), (550,), (500,), (("open", ((0, 500, 0),)),), 400, 450, billing.LEASE_HELD, None),
        # Across shards: the rows' sum and every live lease's donors.
        ((600, 400), (0, 0), (500, 300), (("open", ((0, 500, 0), (1, 300, 0))),), 500, 1_000,
         billing.LEASE_HELD, None),
        ((600, 400), (0, 0), (500, 300), (("open", ((0, 500, 0), (1, 300, 0))),), 500, 1_001,
         billing.EXHAUSTED, (200, 800)),
        # The floor never makes a refusal of what the rows cover, or of what
        # the leases do not: those verdicts are as with no floor.
        ((1_000,), (0,), (0,), (), 100, 5_000, billing.HEADROOM, None),
        ((1_000,), (1_000,), (0,), (), 100, 5_000, billing.EXHAUSTED, None),
        ((1_000,), (1,), (600,), (("open", ((0, 600, 0),)),), 1_000, 5_000, billing.EXHAUSTED, None),
        ((1_000,), (0,), (900,), (("closed", ((0, 900, 0),)),), 450, 5_000, billing.EXHAUSTED, None),
    ],
)
def test_lease_aware_precheck_at_a_floor(
    credits: tuple[int, ...],
    usage: tuple[int, ...],
    reserved: tuple[int, ...],
    leases: tuple[Any, ...],
    estimate: int,
    floor: int,
    verdict: str,
    under: tuple[int, int] | None,
) -> None:
    store, database = _precheck_store(credits, usage, reserved, leases)
    # Any: the verdict is annotated str and also carries what the read found.
    made: Any = billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=estimate, lease_aware=True, lease_floor=floor,
    )
    assert made == verdict
    assert getattr(made, "lease_under_floor", None) == (
        None if under is None else LeaseUnderFloor(available=under[0], lease_held=under[1], floor=floor)
    )
    without = billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=estimate, lease_aware=True,
    )
    if under is None:
        # Not a verdict the floor changes: the same as with no floor.
        assert made == without
    else:
        # Only a lease-held verdict is changed, and to the rows' own
        # verdict, read and all: what they get when no lease is read.
        assert without == billing.LEASE_HELD
        plain: Any = billing.credit_exhaustion_precheck(
            database, store._param_types, workspace_id=WS, estimate=estimate,
        )
        assert plain == billing.EXHAUSTED
        assert made.credit_read is not None and made.credit_read.read_timestamp_ns is not None
        # The same read but for its instant, which is each snapshot's own.
        assert dataclasses.replace(made.credit_read, read_timestamp_ns=None) == dataclasses.replace(
            plain.credit_read, read_timestamp_ns=None
        )
        assert getattr(plain, "lease_under_floor", None) is None
    # Not lease aware, the floor is not read.
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=estimate, lease_floor=floor,
    ) == (billing.EXHAUSTED if without == billing.LEASE_HELD else without)


def test_a_floor_leaves_the_failed_read_and_the_pending_reservation_as_they_were(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What comes before the lease is weighed is not the floor's to change:
    a reservation pending under the request's idempotency scope defers to
    the transaction, and a lease read that fails answers the rows' own
    verdict with no amounts, at a floor as with none."""
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, _key = _seed(case)

    def precheck(**kwargs: Any) -> Any:
        return billing.credit_exhaustion_precheck(
            database, store._param_types, workspace_id=WS, estimate=450, lease_aware=True, **kwargs,
        )

    # Positive control: under this floor the verdict is the floor's.
    assert precheck(lease_floor=5_000).lease_under_floor == LeaseUnderFloor(100, 900, 5_000)
    monkeypatch.setattr(
        billing, "read_reservation_by_idempotency", lambda *_a, **_k: {"reservation_id": "r"}
    )
    for floor in (0, 5_000):
        assert precheck(idempotency_scope="scope", lease_floor=floor) == billing.DEFER
    _fail_lease_reads(monkeypatch)
    for floor in (0, 5_000):
        failed = precheck(lease_floor=floor)
        assert failed == billing.EXHAUSTED
        assert failed.credit_read is not None and failed.lease_under_floor is None


def test_the_verdict_carries_the_amounts_on_both_paths() -> None:
    """The authorize verdict names what the read found, after a
    transaction's refusal and from the remembered precheck, and nothing on
    any other answer."""
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, key = _seed(case)
    transactions = _count_transactions(database)
    first, authorization = _authorize(store, key, case.estimate, on=True, floor=1_001)
    assert (first, authorization) == (REFUSED, None)
    assert transactions[0] >= 1
    assert first.lease_under_floor == LeaseUnderFloor(available=100, lease_held=900, floor=1_001)
    before = transactions[0]
    second, authorization = _authorize(store, key, case.estimate, on=True, floor=1_001)
    assert (second, authorization) == (REFUSED, None)
    assert transactions[0] == before
    assert second.lease_under_floor == LeaseUnderFloor(available=100, lease_held=900, floor=1_001)
    assert second.credit_read is not None and second.credit_read.read == "remembered"
    # At the floor: the 503, which names no amounts.
    held, _ = _authorize(store, key, case.estimate, on=True, floor=1_000)
    assert held == LEASE_503 and held.lease_under_floor is None
    # A 402 no lease explains names none either.
    plain, _ = _authorize(store, key, 2_000, on=True, floor=1_001)
    assert plain == REFUSED and plain.lease_under_floor is None
    # Off, nothing is read, so nothing is named.
    off, _ = _authorize(store, key, case.estimate, on=False, floor=1_001)
    assert off == REFUSED and off.lease_under_floor is None
    # And an admission names none.
    accepted, authorization = _authorize(store, key, 100, on=True, floor=1_001)
    assert accepted == ACCEPTED and authorization is not None and accepted.lease_under_floor is None


# The evidence, 2026-11-21: one credit row of about $30, spent down to
# within cents of empty, a live lease holding part of what was left, the
# nodes' floor at $2.


def _evidence(outside: int, lease_held: int, hold: int) -> Case:
    credits = 30_000_000
    return Case((credits,), (credits - outside - lease_held,), (0,), (("open", ((0, lease_held, 0),)),), hold)


EVIDENCE = [
    # A lease holding $0.059 and a hold of $0.083, so headroom outside the
    # lease from $0.024 (below it the lease would not cover the hold) to
    # just under the hold (at it the rows would).
    *(_evidence(outside, 59_000, 83_000) for outside in (24_000, 50_000, 82_999)),
    # The same line read as the route writes it, reserved_microdollars the
    # request's own hold: a hold of $0.059 against about $0.14 left in all.
    *(_evidence(outside, 140_000 - outside, 59_000) for outside in (0, 30_000, 58_999)),
]


@pytest.mark.parametrize("case", EVIDENCE, ids=lambda c: repr(c))
def test_the_evidence_case_is_answered_402_at_the_nodes_floor(case: Case) -> None:
    assert case.headroom() == [_total(case) - case.held]
    assert case.lease_held() and _total(case) < NODES_FLOOR
    # Positive control, what happened: with no floor, the 503 on the first
    # request and on every one after it.
    assert _run(case, on=True).outcomes == (LEASE_503,) * 2
    for remembered in (False, True):
        off = _run(case, on=False, remembered=remembered)
        on = _run(case, on=True, remembered=remembered, floor=NODES_FLOOR)
        assert on.outcomes == (REFUSED,) * 2
        assert on == off
        assert on.reservations == 0
        assert on.rows == (30_000_000, 30_000_000 - _total(case), case.held)


def test_a_workspace_the_fast_path_can_still_fund_keeps_the_503_at_the_nodes_floor() -> None:
    """The case the 503 was made for: a balance well above the floor, most
    of it in leases, and a synchronous hold larger than what is outside
    them. And the line itself: $2 in all is the 503, a microdollar less the
    402."""
    funded = _evidence(41_000, 9_000_000, 83_000)
    assert _run(funded, on=True, floor=NODES_FLOOR).outcomes == (LEASE_503,) * 2
    at = _evidence(41_000, NODES_FLOOR - 41_000, 83_000)
    assert _total(at) == NODES_FLOOR
    assert _run(at, on=True, floor=NODES_FLOOR).outcomes == (LEASE_503,) * 2
    under = _evidence(41_000, NODES_FLOOR - 41_001, 83_000)
    assert _total(under) == NODES_FLOOR - 1
    assert _run(under, on=True, floor=NODES_FLOOR).outcomes == (REFUSED,) * 2


# Retries and the idempotent replay: as for today's 402.


def _retries(case: Case, *, on: bool, floor: int) -> list[tuple[str, str | None, int, bool, int]]:
    """One idempotency key through a refusal, its retry, bought credit, and
    a retry after the admission: each answer, the authorization it names,
    the reservations then, the memory, and the transactions it ran."""
    store, database, key = _seed(case)
    counter = _count_transactions(database)
    trace: list[tuple[str, str | None, int, bool, int]] = []
    ids: list[str | None] = []

    def attempt() -> None:
        before = counter[0]
        outcome, authorization = _authorize(
            store, key, case.estimate, on=on, floor=floor, idempotency_key="idem-floor",
        )
        trace.append((
            str(outcome), None if authorization is None else "authorization",
            len(database.reservations), WS in store._insufficient_credit_workspaces,
            min(counter[0] - before, 1),
        ))
        ids.append(None if authorization is None else authorization.id)

    attempt()
    attempt()
    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] += 10_000_000
    attempt()
    attempt()
    # The retry after the admission names the same authorization.
    assert ids[2] is not None and ids[3] == ids[2]
    return trace


def test_retries_and_the_replay_are_those_of_todays_402() -> None:
    case = EVIDENCE[1]
    off = _retries(case, on=False, floor=0)
    # Today's 402: refused, with nothing kept for the key; refused again
    # from the lock-free precheck; admitted once credit is bought, the same
    # key and all; and then replayed from the stored authorization.
    assert off == [
        (REFUSED, None, 0, True, 1),
        (REFUSED, None, 0, True, 0),
        (ACCEPTED, "authorization", 1, False, 1),
        (billing.AuthorizeOutcome.REPLAY, "authorization", 1, False, 1),
    ]
    assert _retries(case, on=True, floor=NODES_FLOOR) == off
    # Positive control: with no floor the refusals are the 503, and the
    # rest is the same, so the 503 never kept anything for the key either.
    held = _retries(case, on=True, floor=0)
    assert [step[0] for step in held] == [LEASE_503, LEASE_503, ACCEPTED, billing.AuthorizeOutcome.REPLAY]
    assert [step[1:] for step in held] == [step[1:] for step in off]


# The route.


def _route_store_with(credits: int, lease_held: int) -> tuple[Any, Any, Any]:
    """The route fixture (a hold of 82,960) on a balance of `credits`, of
    which a live lease holds `lease_held`."""
    store, database, key = _route_store(lease_held=lease_held)
    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] = credits
    return store, database, key


_READ_AT = re.compile(r" read_at=\S+")


def _answer(error: Any, records: list[logging.LogRecord], key: Any) -> dict[str, Any]:
    """An answer and the route's billing lines, whole, but for the two things
    that are each store's own: the read's instant (the fake's clock) and the
    key's id (made at random), written `KEY`."""
    headers = dict(error.headers or {})
    if CREDIT_REFUSAL_READ_HEADER in headers:
        headers[CREDIT_REFUSAL_READ_HEADER] = _READ_AT.sub("", headers[CREDIT_REFUSAL_READ_HEADER])
    lines = [
        record.getMessage().replace(f"key_id={key.hash} ", "key_id=KEY ")
        for record in records if record.name == gateway.logger.name
    ]
    return {
        "status": error.status_code,
        # The body: its error whole, and what else it carries by name (the
        # request's timings, which are each run's own).
        "detail": {"error": error.detail["error"]},
        "body": sorted(error.detail),
        "headers": headers,
        "lines": [line for line in lines if line.startswith("billing.") and "lease_under_floor" not in line],
        "marks": [line for line in lines if line.startswith("billing.lease_under_floor_402 ")],
    }


def _route_refusals(
    credits: int, lease_held: int, *, on: bool, floor: int, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[str], Any, Any]:
    """Two requests of the route fixture's hold, the second through the
    process's memory: each answer with its lines, the free-credit
    exhaustions recorded, and the database and the key after them."""
    exhausted: list[str] = []
    monkeypatch.setattr(gateway, "record_free_credit_exhausted_safely", exhausted.append)
    _store, database, key = _route_store_with(credits, lease_held)
    answers = []
    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=gateway.logger.name):
            error = _authorize_route(key, on=on, floor=floor)
        answers.append(_answer(error, list(caplog.records), key))
    return answers, exhausted, database, key


def _nothing_held(database: Any, lease_held: int) -> None:
    balance = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert (balance["total_usage"], balance["reserved"]) == (0, lease_held)
    assert not database.reservations
    assert not database.typed.get("tr_gateway_authorization", {})


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_under_the_floor_answers_the_402_of_the_503_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The evidence at the route: $0.10 in all, $0.059 of it in a lease, a
    hold of $0.083, the floor at $2. The 402, its body, its header, its
    billing.insufficient_credits line and its free-credit record are those
    of the same balance with the 503 off; the one difference is the line
    that counts it."""
    credits, lease_held = 100_000, 59_000
    off, off_exhausted, off_database, _off_key = _route_refusals(
        credits, lease_held, on=False, floor=0, caplog=caplog, monkeypatch=monkeypatch,
    )
    on, on_exhausted, on_database, _on_key = _route_refusals(
        credits, lease_held, on=True, floor=NODES_FLOOR, caplog=caplog, monkeypatch=monkeypatch,
    )
    for answer in on:
        assert answer["status"] == 402
        assert answer["detail"]["error"]["type"] == "insufficient_credits"
        assert _enclave_caches(answer["status"], answer["detail"]["error"]["type"])
        assert "Retry-After" not in answer["headers"]
        assert set(answer["headers"]) == {CREDIT_REFUSAL_READ_HEADER}
        assert answer["lines"] == [
            f"billing.insufficient_credits workspace_id={WS} key_id=KEY request_id=request-lease-503 "
            "model=anthropic/claude-haiku-4.5 reserved_microdollars=82960 estimated_microdollars=82960 "
            "estimated_input_tokens=42000 output_token_budget=8192"
        ]
        assert answer["marks"] == [
            f"billing.lease_under_floor_402 workspace_id={WS} key_id=KEY "
            "request_id=request-lease-503 reserved_microdollars=82960 estimated_microdollars=82960 "
            "available_microdollars=41000 lease_held_microdollars=59000 floor_microdollars=2000000"
        ]
    # The first is the transaction's refusal, the second the remembered
    # precheck's, as with the 503 off.
    assert [a["headers"][CREDIT_REFUSAL_READ_HEADER].split()[0] for a in on] == ["read=heal", "read=remembered"]
    for mine, todays in zip(on, off, strict=True):
        assert todays["marks"] == []
        assert {k: v for k, v in mine.items() if k != "marks"} == {
            k: v for k, v in todays.items() if k != "marks"
        }
    assert on_exhausted == off_exhausted == [WS, WS]
    _nothing_held(on_database, lease_held)
    _nothing_held(off_database, lease_held)


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_without_a_floor_answers_the_503_for_the_same_balance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Positive control, and the setting at 0: the default answers the 503
    the incident saw, with its header and its line, and no mark."""
    assert Settings(environment="test").fastpath_lease_headroom_503_floor_micro == 0
    answers, exhausted, database, _key = _route_refusals(
        100_000, 59_000, on=True, floor=0, caplog=caplog, monkeypatch=monkeypatch,
    )
    for answer in answers:
        assert answer["status"] == 503
        assert answer["headers"] == {"Retry-After": gateway.LEASE_HEADROOM_RETRY_AFTER}
        assert answer["detail"]["error"]["type"] == "service_unavailable"
        assert answer["detail"]["error"]["message"] == gateway.LEASE_HEADROOM_MESSAGE
        (line,) = answer["lines"]
        assert line.startswith("billing.lease_headroom_unavailable ")
        assert answer["marks"] == []
    assert exhausted == []
    _nothing_held(database, 59_000)


@pytest.mark.usefixtures("fixed_operation_catalog")
@pytest.mark.parametrize(
    ("credits", "status"),
    [
        # $2 in all, all but $0.05 of it in a lease: at the floor, the 503.
        (NODES_FLOOR, 503),
        (NODES_FLOOR + 7_000_000, 503),
        # A microdollar under it: the 402.
        (NODES_FLOOR - 1, 402),
    ],
)
def test_route_at_or_above_the_floor_still_answers_the_503(
    credits: int, status: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    lease_held = credits - 50_000
    trace = "97947552453ac21dc57168fd64afffe7"
    exhausted: list[str] = []
    monkeypatch.setattr(gateway, "record_free_credit_exhausted_safely", exhausted.append)
    _store, database, key = _route_store_with(credits, lease_held)

    with caplog.at_level(logging.INFO, logger=gateway.logger.name):
        error = _authorize_route(key, on=True, floor=NODES_FLOOR, trace_context=f"{trace}/123;o=1")

    answer = _answer(error, list(caplog.records), key)
    assert answer["status"] == status
    if status == 503:
        assert answer["headers"] == {"Retry-After": "1"}
        assert answer["detail"]["error"]["type"] == "service_unavailable"
        assert answer["detail"]["error"]["message"] == gateway.LEASE_HEADROOM_MESSAGE
        assert not _enclave_caches(answer["status"], answer["detail"]["error"]["type"])
        # The line the deploy gate and the 503's counter read, unchanged.
        assert answer["lines"] == [
            f"billing.lease_headroom_unavailable workspace_id={WS} key_id=KEY "
            f"request_id=request-lease-503 reserved_microdollars=82960 estimated_microdollars=82960 trace={trace}"
        ]
        assert answer["marks"] == []
        assert exhausted == []
    else:
        assert answer["detail"]["error"]["type"] == "insufficient_credits"
        assert [line.split()[0] for line in answer["lines"]] == ["billing.insufficient_credits"]
        assert len(answer["marks"]) == 1
        assert exhausted == [WS]
    _nothing_held(database, lease_held)


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_retries_and_replay_under_the_floor_are_todays() -> None:
    """One idempotency key at the route: refused twice under the floor,
    admitted once credit is bought, replayed after. Nothing is kept for the
    key by either refusal, with the floor or with the 503 off."""

    def run(*, on: bool, floor: int) -> list[tuple[Any, ...]]:
        idem = "idem-floor-route"
        _store, database, key = _route_store_with(100_000, 59_000)
        settings = Settings(
            environment="test", fastpath_lease_headroom_503=on, fastpath_lease_headroom_503_floor_micro=floor,
        )
        steps: list[tuple[Any, ...]] = []
        for _ in range(2):
            error = _authorize_route(key, on=on, floor=floor, idempotency_key=idem)
            steps.append((error.status_code, error.detail["error"]["type"], len(database.reservations)))
        database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] += 10_000_000
        responses = [
            gateway._authorize_gateway_sync(
                _route_request(), _route_body(key, idempotency_key=idem), settings,
            )["data"]
            for _ in range(2)
        ]
        assert responses[1]["authorization_id"] == responses[0]["authorization_id"]
        steps += [(200, response["idempotent_replay"], len(database.reservations)) for response in responses]
        balance = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
        steps.append((balance["total_usage"], balance["reserved"]))
        return steps

    off = run(on=False, floor=0)
    assert off == [
        (402, "insufficient_credits", 0),
        (402, "insufficient_credits", 0),
        (200, False, 1),
        (200, True, 1),
        # One hold, the admitted request's, beside the lease's.
        (0, 59_000 + 82_960),
    ]
    assert run(on=True, floor=NODES_FLOOR) == off


# The setting and its deployed value.

_ROLLOUT_FLOOR = r'^\s*"TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO=(\d+)"$'


def _rollout() -> str:
    return (ROOT / "scripts" / "deploy" / "rollout.sh").read_text()


def _rollout_floor() -> int:
    found = re.findall(_ROLLOUT_FLOOR, _rollout(), re.M)
    assert len(found) == 1, found
    return int(found[0])


def test_the_rollout_floor_is_the_nodes_floor() -> None:
    """The control plane holds the 503 to the floor the nodes lease at
    (infra/fastpath.tf fastpath_floor, the node's -floor). A change of the
    nodes' floor that leaves rollout.sh behind fails here: with the control
    plane's figure above the nodes', a workspace the fast path still tops
    up would be told it is out of credit; below it, a workspace no top-up
    can reach would be told to retry, as on 2026-11-21."""
    assert _rollout_floor() == _nodes_floor() == NODES_FLOOR


def test_the_floor_check_reads_each_literal() -> None:
    """The control: the pattern finds rollout.sh's literal and nothing
    else, so the rule above compares the deployed values."""
    line = '  "TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO=2000000"\n'
    assert re.findall(_ROLLOUT_FLOOR, line, re.M) == ["2000000"]
    assert re.findall(_ROLLOUT_FLOOR, '  "TR_FASTPATH_LEASE_HEADROOM_503=true"\n', re.M) == []
    assert re.findall(_ROLLOUT_FLOOR, '  # "TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO=1"\n', re.M) == []
    assert re.findall(_ROLLOUT_FLOOR, '  "TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO=${FLOOR}"\n', re.M) == []
    assert _rollout_floor() > 0 and _nodes_floor() > 0


def test_the_setting_is_zero_by_default_and_a_literal_beside_the_503_in_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Zero in code, so no revision inherits a floor and every answer is as
    # before; production sets it in rollout.sh's literal, right after the
    # 503's own, which must be on for it to be read at all.
    assert Settings(environment="test").fastpath_lease_headroom_503_floor_micro == 0
    with pytest.raises(ValidationError):
        Settings(environment="test", fastpath_lease_headroom_503_floor_micro=-1)
    rollout = _rollout()
    switch = '"TR_FASTPATH_LEASE_HEADROOM_503=true"'
    literal = f'"TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO={NODES_FLOOR}"'
    assert rollout.count(switch) == 1 and rollout.count(literal) == 1
    assert rollout.count("TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO=") == 1
    between = rollout[rollout.index(switch) + len(switch):rollout.index(literal)]
    assert all(line.strip().startswith("#") for line in between.strip().splitlines())
    # The name rollout.sh sets is the one the setting reads.
    monkeypatch.setenv("TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO", str(NODES_FLOOR))
    assert Settings(environment="test").fastpath_lease_headroom_503_floor_micro == NODES_FLOOR
