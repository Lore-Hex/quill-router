"""The 503 for headroom in leases (docs/design/fast-admission-widening.md
section 11 item 7; fast-admission-and-batched-settlement.md section 4.4).

402 comes only when the workspace's balance cannot cover the hold. When the
balance covers it but the headroom outside the fast path's leases does not
(`total_credits - total_usage - reserved`, where the leases' remaining
allocation is part of `reserved`), the typed authorize answers 503 with
Retry-After, in the reserve and in the insufficient-credit precheck, and the
precheck's per-process memory does not remember the workspace as exhausted.
All of it behind `fastpath_lease_headroom_503`, off by default.

The differential runs today's authorize (the setting off) against the same
credit rows with and without lease rows, and against the setting on: off
must not read a lease at all, and on must differ only where live leases
hold the shortfall, 402 to 503, never the other way and never to a 200.
Ids are synthetic.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import make_fake_store
from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _request,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router import storage_gcp_authorize as billing
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.storage_errors import StoreUnavailable
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount

WS = "ws-lease-503"
OTHER_WS = "ws-lease-503-other"
LEASE_MARKER = "/* fastpath_lease_held_of */"

# A lease: its state and its donors, (credit shard, allocation, consumed).
Lease = tuple[str, tuple[tuple[int, int, int], ...]]


@dataclass(frozen=True)
class Case:
    credits: tuple[int, ...]
    usage: tuple[int, ...]
    sync_reserved: tuple[int, ...]
    leases: tuple[Lease, ...]
    estimate: int
    marked: bool = False

    @property
    def held(self) -> int:
        return sum(
            allocation - consumed
            for state, donors in self.leases
            if state in ("open", "draining")
            for _shard, allocation, consumed in donors
        )

    def reserved(self) -> list[int]:
        out = list(self.sync_reserved)
        for state, donors in self.leases:
            if state not in ("open", "draining"):
                continue
            for shard, allocation, consumed in donors:
                out[shard] += allocation - consumed
        return out

    def headroom(self) -> list[int]:
        return [
            c - u - r for c, u, r in zip(self.credits, self.usage, self.reserved(), strict=True)
        ]

    def lease_held(self) -> bool:
        """The design's rule: the rows cannot cover the estimate (no shard and
        not their sum, which a rebalance would gather) and live leases hold
        the rest."""
        room = self.headroom()
        refused = max(room) < self.estimate and sum(room) < self.estimate
        return (
            refused
            and not self.marked
            and self.held > 0
            and sum(room) + self.held >= self.estimate
        )


def _seed(case: Case, *, with_leases: bool = True) -> tuple[Any, Any, Any]:
    store, database = make_fake_store(request_record_write_mode="typed")
    store._write_entity(
        "credit", WS, CreditAccount(workspace_id=WS, shard_count=len(case.credits))
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, reserved in enumerate(case.reserved()):
        table[(WS, shard)] = {
            "workspace_id": WS,
            "shard": shard,
            "total_credits": case.credits[shard],
            "total_usage": case.usage[shard],
            "reserved": reserved,
            "in_debt": case.marked,
            "source_updated_at": None,
            "updated_at": None,
        }
    if with_leases:
        _seed_leases(database, WS, case.leases)
    # Another workspace's live lease is never this one's headroom.
    _seed_leases(database, OTHER_WS, (("open", ((0, 10_000, 0),)),), prefix="other")
    _raw, key = store.api_keys.create(
        workspace_id=WS, name="lease-503", creator_user_id=None, limit_microdollars=None
    )
    return store, database, key


def _seed_leases(database: Any, workspace: str, leases: tuple[Lease, ...], *, prefix: str = "l") -> None:
    for index, (state, donors) in enumerate(leases):
        lease_id = f"{prefix}-{workspace}-{index}"
        database.fastpath_leases[lease_id] = {
            "workspace_id": workspace,
            "state": state,
            "allocation": sum(allocation for _s, allocation, _c in donors),
            "consumed": sum(consumed for _s, _a, consumed in donors),
        }
        for shard, allocation, consumed in donors:
            database.fastpath_lease_donors.append({
                "workspace_id": workspace,
                "lease_id": lease_id,
                "credit_shard": shard,
                "allocation": allocation,
                "consumed": consumed,
            })


def _authorize(store: Any, key: Any, estimate: int, *, on: bool) -> tuple[str, Any]:
    return store.authorize_gateway_typed(
        workspace_id=WS,
        key_hash=key.hash,
        estimate=estimate,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="model",
        provider="provider",
        requested_model_id=None,
        candidate_model_ids=["model"],
        region="us",
        endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        lease_headroom_503=on,
    )


@dataclass(frozen=True)
class Answer:
    outcomes: tuple[str, ...]
    rows: tuple[tuple[int, int, int], ...]
    reservations: int
    remembered: bool
    # Read-write transactions each request ran. Not compared: on two shards
    # the bounded random shard prefix can take a retry on one run and not on
    # another.
    transactions: tuple[int, ...] = field(default=(), compare=False)


def _count_transactions(database: Any) -> list[int]:
    counter = [0]
    original = database.run_in_transaction

    def counting(*args: Any, **kwargs: Any) -> Any:
        counter[0] += 1
        return original(*args, **kwargs)

    database.run_in_transaction = counting
    return counter


def _run(case: Case, *, on: bool, with_leases: bool = True, remembered: bool = False) -> Answer:
    """Two authorizes of the same hold, the second through whatever the first
    left in the process's memory, and what they left behind."""
    store, database, key = _seed(case, with_leases=with_leases)
    if remembered:
        store._insufficient_credit_workspaces.add(WS)
    counter = _count_transactions(database)
    outcomes, transactions = [], []
    for _ in range(2):
        before = counter[0]
        try:
            outcome, _authorization = _authorize(store, key, case.estimate, on=on)
            outcomes.append(str(outcome))
        except StoreUnavailable:
            outcomes.append("store_unavailable")
        transactions.append(counter[0] - before)
    table = database.typed[CREDIT_BALANCE_TABLE]
    shards = [table[(WS, s)] for s in range(len(case.credits))]
    # Summed over the shards: which shard an admission holds on, and where a
    # rebalance moves idle credit, are chosen at random.
    rows = tuple(
        (sum(int(r["total_credits"]) for r in shards),
         sum(int(r["total_usage"]) for r in shards),
         sum(int(r["reserved"]) for r in shards)),
    )
    return Answer(
        outcomes=tuple(outcomes),
        rows=rows,
        reservations=len(database.reservations),
        remembered=WS in store._insufficient_credit_workspaces,
        transactions=tuple(transactions),
    )


def _single_shard_cases() -> list[Case]:
    leases: tuple[tuple[Lease, ...], ...] = (
        (),
        (("open", ((0, 500, 0),)),),
        (("open", ((0, 500, 200),)),),
        (("draining", ((0, 300, 0),)),),
        # A closed lease holds nothing, whatever its donors say.
        (("closed", ((0, 500, 0),)),),
        (("open", ((0, 200, 0),)), ("draining", ((0, 250, 50),))),
    )
    return [
        Case((1_000,), (usage,), (sync,), lease_set, estimate)
        for usage, sync, lease_set, estimate in itertools.product(
            (0, 400, 1_000, 1_200), (0, 300), leases, (100, 450, 900, 2_000)
        )
    ]


def _two_shard_cases() -> list[Case]:
    leases: tuple[tuple[Lease, ...], ...] = (
        (),
        (("open", ((0, 300, 0), (1, 200, 0))),),
        (("open", ((1, 350, 100),)),),
        (("draining", ((0, 400, 0),)), ("closed", ((1, 300, 0),))),
    )
    return [
        Case((600, 400), usage, sync, lease_set, estimate)
        for usage, sync, lease_set, estimate in itertools.product(
            ((0, 0), (300, 100)), ((0, 0), (100, 0)), leases, (100, 500, 800, 1_500)
        )
    ]


def _marked_cases() -> list[Case]:
    # In debt: the signed sum is negative and every row marked. A 402 stays a
    # 402 whatever the leases hold.
    return [
        Case((1_000,), (600,), (0,), (("open", ((0, 500, 0),)),), 100, marked=True),
        Case((100, 500), (300, 0), (0, 0), (("open", ((1, 400, 0),)),), 150, marked=True),
    ]


def test_the_marked_cases_would_be_lease_held_unmarked() -> None:
    # Positive control: only the mark keeps them out of the 503.
    for case in _marked_cases():
        assert not case.lease_held()
        assert dataclasses.replace(case, marked=False).lease_held()


CASES = _single_shard_cases() + _two_shard_cases() + _marked_cases()
LEASE_HELD_CASES = [case for case in CASES if case.lease_held()]


def test_the_matrix_reaches_every_kind_of_case() -> None:
    # Positive control on the matrix itself: it holds lease-held cases on one
    # shard and on two, accepted ones, refusals with no lease, and refusals
    # with leases too small to cover the shortfall.
    assert any(len(case.credits) == 1 for case in LEASE_HELD_CASES)
    assert any(len(case.credits) == 2 for case in LEASE_HELD_CASES)
    off = {case: _run(case, on=False) for case in CASES}
    assert any(answer.outcomes[0] == billing.AuthorizeOutcome.ACCEPTED for answer in off.values())
    refused = [
        case for case, answer in off.items()
        if answer.outcomes[0] == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    ]
    assert any(case.held == 0 for case in refused)
    assert any(case.held > 0 and not case.lease_held() for case in refused)


ACCEPTED = billing.AuthorizeOutcome.ACCEPTED
REFUSED = billing.AuthorizeOutcome.INSUFFICIENT_CREDITS


def _today(case: Case, remembered: bool) -> set[tuple[tuple[str, ...], bool]]:
    """Today's authorize, written down independently of the code: the rules
    the #1461 tests pin (tests/test_insufficient_credit_precheck.py,
    tests/test_credit_row_sharding_increment3.py). A hold is admitted when
    the rows are unmarked and one shard covers it, or, on more than one
    shard, their sum does (the rebalance gathers idle credit onto a target);
    otherwise it is the 402, and the workspace is remembered. An admission
    forgets it. Leases count only through `reserved`. Which covering shard
    an admission takes, and where a rebalance gathers, are chosen at random,
    so the answer is the set of every choice's outcomes and memory."""
    results = set()

    def walk(rooms: tuple[int, ...], outcomes: tuple[str, ...]) -> None:
        if len(outcomes) == 2:
            memory = remembered
            for outcome in outcomes:
                memory = outcome == REFUSED
            results.add((outcomes, memory))
            return
        e = case.estimate
        covering = [i for i, room in enumerate(rooms) if room >= e]
        if case.marked or not (covering or (len(rooms) > 1 and sum(rooms) >= e)):
            walk(rooms, (*outcomes, REFUSED))
            return
        if covering:
            for i in covering:
                walk(tuple(room - e if j == i else room for j, room in enumerate(rooms)), (*outcomes, ACCEPTED))
            return
        for target in range(len(rooms)):
            pooled = sum(room for room in rooms if room > 0) - e
            walk(tuple(
                pooled if j == target else (room if room < 0 else 0) for j, room in enumerate(rooms)
            ), (*outcomes, ACCEPTED))

    walk(tuple(case.headroom()), ())
    return results


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c))
def test_off_answers_are_pinned_to_todays_rules(case: Case) -> None:
    """The off path against fixed expectations, not against itself: a change
    to today's authorize fails here whatever the setting does."""
    for remembered in (False, True):
        off = _run(case, on=False, remembered=remembered)
        assert (off.outcomes, off.remembered) in _today(case, remembered)
        # A refusal after a refusal is the lock-free precheck's: no
        # transaction (an admission in between forgets the workspace).
        if off.outcomes == (REFUSED, REFUSED):
            assert off.transactions[1] == 0
        if remembered and off.outcomes[0] == REFUSED:
            assert off.transactions[0] == 0


def test_the_pinned_rules_are_not_vacuous() -> None:
    # Positive control: the model is decisive for most of the matrix, and it
    # refuses a case the code admits once the rule is bent.
    decisive = [case for case in CASES if len(_today(case, False)) == 1]
    assert len(decisive) > len(CASES) * 3 // 4
    bent = Case((1_000,), (0,), (0,), (), 1_000)
    assert _today(bent, False) == {((ACCEPTED, REFUSED), True)}
    assert _today(dataclasses.replace(bent, estimate=1_001), False) == {((REFUSED, REFUSED), True)}


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c))
def test_off_is_todays_authorize_and_reads_no_lease(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off, the lease rows change nothing: the answers, the rows and the
    memory are those of the same credit rows with no lease rows at all, and
    the lease statement is never executed."""
    executed = _track_lease_reads(monkeypatch)
    with_leases = _run(case, on=False)
    without_leases = _run(case, on=False, with_leases=False)
    assert with_leases == without_leases
    assert executed == []
    for remembered in (False, True):
        assert _run(case, on=False, remembered=remembered) == _run(
            case, on=False, with_leases=False, remembered=remembered
        )
    assert executed == []


def _expected_on(case: Case, off: Answer, remembered: bool) -> tuple[tuple[str, ...], bool]:
    """Today's answers with each refusal the leases explain turned into the
    503: a 402 for an unmarked workspace whose live leases hold, with what is
    left outside them after the admissions before it, the hold. And the
    memory those answers leave: a 402 or a 503 remembers, an admission
    forgets."""
    room = sum(case.headroom())
    outcomes, admitted = [], 0
    for outcome in off.outcomes:
        if (
            outcome == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
            and not case.marked
            and case.held > 0
            and room - admitted * case.estimate + case.held >= case.estimate
        ):
            outcome = billing.AuthorizeOutcome.LEASE_HEADROOM
        admitted += outcome == billing.AuthorizeOutcome.ACCEPTED
        outcomes.append(outcome)
    memory = remembered
    for outcome in outcomes:
        memory = outcome in (
            billing.AuthorizeOutcome.INSUFFICIENT_CREDITS, billing.AuthorizeOutcome.LEASE_HEADROOM,
        )
    return tuple(outcomes), memory


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c))
def test_on_differs_only_where_leases_hold_the_shortfall(case: Case) -> None:
    for remembered in (False, True):
        off = _run(case, on=False, remembered=remembered)
        on = _run(case, on=True, remembered=remembered)
        # The money is the same either way: a 503 holds and writes nothing.
        assert on.rows == off.rows
        assert on.reservations == off.reservations
        outcomes, memory = _expected_on(case, off, remembered)
        assert on.outcomes == outcomes
        assert on.remembered == memory
        if not case.lease_held() and billing.AuthorizeOutcome.LEASE_HEADROOM not in outcomes:
            assert on == off
    if case.lease_held():
        # Today's answer is the 402, remembered; on, the 503 both times,
        # remembered too, so the second is the lock-free precheck's: no
        # transaction, as today's second 402.
        off = _run(case, on=False)
        assert off.outcomes == (billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,) * 2
        assert off.remembered
        assert off.transactions[1] == 0
        on = _run(case, on=True)
        assert on.outcomes == (billing.AuthorizeOutcome.LEASE_HEADROOM,) * 2
        assert on.remembered
        assert on.transactions[0] >= 1
        assert on.transactions[1] == 0


def test_on_never_turns_a_refusal_into_an_admission_or_back() -> None:
    for case in CASES:
        off = _run(case, on=False)
        on = _run(case, on=True)
        for before, after in zip(off.outcomes, on.outcomes, strict=True):
            if after == billing.AuthorizeOutcome.ACCEPTED:
                assert before == billing.AuthorizeOutcome.ACCEPTED, case
            if before != after:
                assert (before, after) == (
                    billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,
                    billing.AuthorizeOutcome.LEASE_HEADROOM,
                ), case


def test_leases_too_small_for_the_shortfall_keep_the_402() -> None:
    case = Case((1_000,), (400,), (0,), (("open", ((0, 500, 0),)),), 900)
    assert not case.lease_held()  # 100 outside leases + 500 held < 900
    on = _run(case, on=True)
    assert on.outcomes == (billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,) * 2
    assert on.remembered


def test_a_remembered_workspace_whose_leases_hold_the_rest_stays_remembered() -> None:
    """The precheck path: a workspace the memory holds, funded but held in
    leases, is answered from the lock-free snapshot with the 503, without a
    transaction, and stays remembered: each later hit re-reads the rows and
    the leases."""
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    assert case.lease_held()
    store, database, key = _seed(case)
    store._insufficient_credit_workspaces.add(WS)
    transactions = _count_transactions(database)
    for _ in range(3):
        outcome, authorization = _authorize(store, key, case.estimate, on=True)
        assert outcome == billing.AuthorizeOutcome.LEASE_HEADROOM
        assert authorization is None
    assert transactions[0] == 0
    assert WS in store._insufficient_credit_workspaces


def _close_lease(database: Any, *, consumed: int) -> None:
    """The workspace's one lease closes having booked `consumed` of its 900:
    its donor's booking moves reserved to usage, and the rest goes back to
    headroom (store/close.go)."""
    lease_id = f"l-{WS}-0"
    donor = next(d for d in database.fastpath_lease_donors if d["lease_id"] == lease_id)
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    row["total_usage"] += consumed
    row["reserved"] -= donor["allocation"] - donor["consumed"]
    donor["consumed"] = consumed
    database.fastpath_leases[lease_id].update(state="closed", consumed=consumed)


def test_when_the_lease_returns_its_allocation_the_request_is_admitted_and_forgotten() -> None:
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, key = _seed(case)
    outcome, _ = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.LEASE_HEADROOM
    assert WS in store._insufficient_credit_workspaces
    _close_lease(database, consumed=0)
    verdict = billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=case.estimate, lease_aware=True,
    )
    assert verdict == billing.HEADROOM
    transactions = _count_transactions(database)
    outcome, authorization = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert transactions[0] >= 1
    assert WS not in store._insufficient_credit_workspaces
    assert database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["reserved"] == case.estimate


def test_when_the_lease_spends_the_balance_the_answer_is_todays_402() -> None:
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, key = _seed(case)
    outcome, _ = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.LEASE_HEADROOM
    _close_lease(database, consumed=900)
    transactions = _count_transactions(database)
    outcome, authorization = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert transactions[0] == 0
    assert WS in store._insufficient_credit_workspaces


def test_a_failed_lease_verdict_keeps_todays_402(monkeypatch: pytest.MonkeyPatch) -> None:
    case = LEASE_HELD_CASES[0]
    store, database, key = _seed(case)
    original = billing.credit_exhaustion_precheck

    def failing(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("lease_aware"):
            return billing.DEFER
        return original(*args, **kwargs)

    monkeypatch.setattr(billing, "credit_exhaustion_precheck", failing)
    outcome, _ = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert WS in store._insufficient_credit_workspaces


def _fail_lease_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import tests.fakes.spanner as fake

    failed: list[str] = []
    original = fake._execute_sql

    def failing(db: Any, txn: Any, sql: str, params: dict[str, Any]) -> Any:
        if LEASE_MARKER in sql:
            failed.append(sql)
            raise RuntimeError("lease read unavailable")
        return original(db, txn, sql, params)

    monkeypatch.setattr(fake, "_execute_sql", failing)
    return failed


def test_a_failed_lease_read_is_todays_402_lock_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease read that fails answers today's verdict for the rows,
    EXHAUSTED, not DEFER: on the memory path, today's 402 from the snapshot,
    with no transaction; after a refusal, today's 402."""
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, key = _seed(case)
    failed = _fail_lease_reads(monkeypatch)
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=450, lease_aware=True,
    ) == billing.EXHAUSTED
    outcome, _ = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert WS in store._insufficient_credit_workspaces
    transactions = _count_transactions(database)
    outcome, _ = _authorize(store, key, case.estimate, on=True)
    assert outcome == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert transactions[0] == 0
    assert len(failed) == 3


def test_the_lease_read_after_a_refusal_keeps_the_strict_budget() -> None:
    """A strict key's post-refusal read runs under the transaction's budget
    deadline: past it, the read does not run and the answer is today's 402."""
    import time

    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, _database, _key = _seed(case)
    # Positive control: with no budget, and within one, it is lease-held.
    assert store._lease_held_after_refusal(WS, 450, None)
    assert store._lease_held_after_refusal(WS, 450, time.monotonic() + 30)
    assert not store._lease_held_after_refusal(WS, 450, time.monotonic() - 1)


def _track_lease_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import tests.fakes.spanner as fake

    executed: list[str] = []
    original = fake._execute_sql

    def tracking(db: Any, txn: Any, sql: str, params: dict[str, Any]) -> Any:
        if LEASE_MARKER in sql:
            executed.append(sql)
        return original(db, txn, sql, params)

    monkeypatch.setattr(fake, "_execute_sql", tracking)
    return executed


@pytest.mark.parametrize(
    ("credits", "usage", "reserved", "leases", "estimate", "verdict"),
    [
        # No lease: today's verdicts.
        ((1_000,), (1_000,), (0,), (), 100, billing.EXHAUSTED),
        ((1_000,), (0,), (0,), (), 100, billing.HEADROOM),
        # Live leases hold the shortfall.
        ((1_000,), (0,), (900,), (("open", ((0, 900, 0),)),), 450, billing.LEASE_HELD),
        ((1_000,), (0,), (900,), (("draining", ((0, 900, 450),)),), 450, billing.LEASE_HELD),
        # Exactly covered counts.
        ((1_000,), (0,), (600,), (("open", ((0, 600, 0),)),), 1_000, billing.LEASE_HELD),
        # One short of covering does not.
        ((1_000,), (1,), (600,), (("open", ((0, 600, 0),)),), 1_000, billing.EXHAUSTED),
        # Closed leases hold nothing.
        ((1_000,), (0,), (900,), (("closed", ((0, 900, 0),)),), 450, billing.EXHAUSTED),
        # Across shards: neither shard nor the sum covers it; the leases do.
        ((600, 400), (0, 0), (500, 300), (("open", ((0, 500, 0), (1, 300, 0))),), 500, billing.LEASE_HELD),
    ],
)
def test_lease_aware_precheck(
    credits: tuple[int, ...],
    usage: tuple[int, ...],
    reserved: tuple[int, ...],
    leases: tuple[Lease, ...],
    estimate: int,
    verdict: str,
) -> None:
    store, database = make_fake_store(request_record_write_mode="typed")
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(credits):
        table[(WS, shard)] = {
            "workspace_id": WS, "shard": shard, "total_credits": total,
            "total_usage": usage[shard], "reserved": reserved[shard],
        }
    _seed_leases(database, WS, leases)
    _seed_leases(database, OTHER_WS, (("open", ((0, 10_000, 0),)),), prefix="other")
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=estimate, lease_aware=True,
    ) == verdict
    # Not lease aware, a lease-held verdict is today's EXHAUSTED.
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=estimate,
    ) == (billing.EXHAUSTED if verdict == billing.LEASE_HELD else verdict)


def test_lease_aware_precheck_defers_to_a_pending_reservation_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing reservation under the request's idempotency scope defers
    to the transaction (its replay) before any lease is weighed."""
    case = Case((1_000,), (0,), (0,), (("open", ((0, 900, 0),)),), 450)
    store, database, _key = _seed(case)
    monkeypatch.setattr(
        billing, "read_reservation_by_idempotency", lambda *_a, **_k: {"reservation_id": "r"}
    )
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=450,
        idempotency_scope="scope", lease_aware=True,
    ) == billing.DEFER
    monkeypatch.setattr(billing, "read_reservation_by_idempotency", lambda *_a, **_k: None)
    assert billing.credit_exhaustion_precheck(
        database, store._param_types, workspace_id=WS, estimate=450,
        idempotency_scope="scope", lease_aware=True,
    ) == billing.LEASE_HELD


# The route: the 503's status, Retry-After and type, against today's 402.


def _route_store(*, lease_held: int) -> tuple[Any, Any, Any]:
    # The fixture catalog prices this body's hold at 82,960 (42,000 prompt
    # tokens at 1 per million and 8,192 output at 5 per million).
    store, database, key = _seed_typed_gateway_store(workspace_id=WS)
    balance = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    balance["total_credits"] = 100_000
    balance["reserved"] = lease_held
    if lease_held:
        _seed_leases(database, WS, (("open", ((0, lease_held, 0),)),))
    return store, database, key


def _route_body(key: Any) -> Any:
    body = _lookup_body(key, idempotency_key=None)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 8192
    return body


def _authorize_route(key: Any, *, on: bool, trace_context: str | None = None) -> HTTPException:
    request = _request()
    if trace_context is not None:
        request = Request({
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"x-cloud-trace-context", trace_context.encode())],
        })
    request.state.request_id = "request-lease-503"
    settings = Settings(environment="test", fastpath_lease_headroom_503=on)
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(request, _route_body(key), settings)
    return raised.value


def _enclave_caches(status: int, error_type: str) -> bool:
    """The enclave's billing backoff remembers an authorize refusal only when
    it is a 402 of type insufficient_credits (quill-cloud-proxy,
    enclave-go/internal/trustedrouter/billing_backoff.go,
    IsInsufficientCredits)."""
    return status == 402 and error_type == "insufficient_credits"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_answers_503_with_retry_after_for_a_lease_held_balance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    exhausted: list[str] = []
    monkeypatch.setattr(gateway, "record_free_credit_exhausted_safely", exhausted.append)
    _store, database, key = _route_store(lease_held=50_000)

    with caplog.at_level(logging.INFO):
        error = _authorize_route(key, on=True)

    assert error.status_code == 503
    assert error.headers == {"Retry-After": gateway.LEASE_HEADROOM_RETRY_AFTER}
    assert gateway.LEASE_HEADROOM_RETRY_AFTER == "1"
    body = error.detail["error"]
    assert body["type"] == "service_unavailable"
    assert body["message"] == gateway.LEASE_HEADROOM_MESSAGE
    assert not _enclave_caches(error.status_code, body["type"])
    assert exhausted == []
    assert "billing.lease_headroom_unavailable" in caplog.text
    assert "billing.insufficient_credits" not in caplog.text
    # Nothing held, nothing written.
    balance = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert (balance["total_usage"], balance["reserved"]) == (0, 50_000)
    assert not database.reservations
    assert not database.typed.get("tr_gateway_authorization", {})


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_line_carries_the_requests_trace(caplog: pytest.LogCaptureFixture) -> None:
    """The billing 5xx deploy gate pairs each authorize 503 in Cloud Run's
    request log with this line by trace (scripts/deploy/assert_no_billing_5xx.sh);
    the request log has no other mark of a lease-headroom 503."""
    trace = "97947552453ac21dc57168fd64afffe7"
    _store, _database, key = _route_store(lease_held=50_000)

    with caplog.at_level(logging.INFO, logger=gateway.logger.name):
        error = _authorize_route(key, on=True, trace_context=f"{trace}/123;o=1")

    assert error.status_code == 503
    (line,) = [r.getMessage() for r in caplog.records if "billing.lease_headroom_unavailable" in r.getMessage()]
    assert line.endswith(f" trace={trace}")


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_off_answers_todays_402_for_the_same_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted: list[str] = []
    monkeypatch.setattr(gateway, "record_free_credit_exhausted_safely", exhausted.append)
    _store, _database, key = _route_store(lease_held=50_000)

    error = _authorize_route(key, on=False)

    assert error.status_code == 402
    assert error.detail["error"]["type"] == "insufficient_credits"
    assert not error.headers
    assert _enclave_caches(error.status_code, error.detail["error"]["type"])
    assert exhausted == [WS]


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_route_on_keeps_the_402_for_a_balance_with_no_lease() -> None:
    _store, database, key = _route_store(lease_held=0)
    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] = 50_000

    error = _authorize_route(key, on=True)

    assert error.status_code == 402
    assert error.detail["error"]["type"] == "insufficient_credits"


def test_the_setting_is_off_by_default_and_on_as_a_literal_in_rollout() -> None:
    from pathlib import Path

    # Off in code, so no revision inherits it; production turns it on in
    # rollout.sh's literal, before the enabler admits near-floor balances.
    assert Settings(environment="test").fastpath_lease_headroom_503 is False
    rollout = (Path(__file__).resolve().parents[1] / "scripts/deploy/rollout.sh").read_text()
    assert rollout.count('"TR_FASTPATH_LEASE_HEADROOM_503=true"') == 1
    assert "TR_FASTPATH_LEASE_HEADROOM_503=false" not in rollout
