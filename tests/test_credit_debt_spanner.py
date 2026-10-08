"""The debt rules on the Spanner store's credit rows (fast-admission design section 4.7).

`tests/test_credit_debt.py` checks the pure rules against `CreditDebt`; these
check that the writers apply them: an overrun covers a negative shard or marks
every shard, a marked row refuses, money coming in repays the lowest negative
shard first and only then is offered to payment claims, a return repays before
it absorbs, and a stale mark heals on refusal.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest
from google.cloud.spanner_v1 import param_types

from tests.fakes.spanner import make_fake_store
from trusted_router import storage_gcp_authorize
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, settle_atomic
from trusted_router.storage_gcp_counter_dml import (
    release_credit_no_debt_statement,
    reserve_credit,
    reserve_credit_statement,
    reserve_credit_with_pause,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_federated_settlement import _book_usage
from trusted_router.storage_models import CreditAccount, CreditProvenance, TrustEvent

WS = "ws-debt"
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _seed(
    totals: list[int],
    *,
    usage: list[int] | None = None,
    reserved: list[int] | None = None,
    in_debt: bool = False,
) -> tuple[Any, Any, Any]:
    store, database = make_fake_store()
    usage = usage or [0] * len(totals)
    reserved = reserved or [0] * len(totals)
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS, shard_count=len(totals)))
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(totals):
        table[(WS, shard)] = {
            "workspace_id": WS,
            "shard": shard,
            "total_credits": total,
            "total_usage": usage[shard],
            "reserved": reserved[shard],
            "in_debt": in_debt,
            "billing_pause_causes": [],
            "pause_epoch": 0,
            "source_updated_at": None,
            "updated_at": None,
        }
    _raw, key = store.api_keys.create(
        workspace_id=WS, name="debt", creator_user_id=None, limit_microdollars=None,
    )
    return store, database, key


def _rows(database: Any) -> list[dict[str, Any]]:
    table = database.typed[CREDIT_BALANCE_TABLE]
    return [table[pk] for pk in sorted(pk for pk in table if pk[0] == WS)]


def _headrooms(database: Any) -> list[int]:
    return [
        int(row["total_credits"]) - int(row["total_usage"]) - int(row["reserved"])
        for row in _rows(database)
    ]


def _marks(database: Any) -> list[bool]:
    return [bool(row.get("in_debt")) for row in _rows(database)]


def _authorize(
    store: Any, key: Any, monkeypatch: pytest.MonkeyPatch, *, estimate: int,
    candidates: tuple[int, ...],
) -> tuple[str, Any]:
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
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
    )


def _settle(store: Any, held: Any, actual: int) -> None:
    assert settle_atomic(
        store._database, store._param_types, reservation_id=held.credit_reservation_id,
        actual_micro=actual, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"


def _pay(store: Any, amount: int, event_id: str) -> None:
    assert store.credit_workspace_typed_direct(
        WS, amount, event_id, provenance=CreditProvenance.system_grant(),
    ) is True


def _claim(database: Any, unrecovered: int) -> None:
    row = dict.fromkeys(field.name for field in fields(TrustEvent))
    row.update(workspace_id=WS, event_id="claim", kind="payment", provider="stripe",
               occurred_at=NOW, recorded_at=NOW, unrecovered_micro=unrecovered,
               recovered_micro=0, recovery_target=unrecovered, debit_status="unrecovered")
    database.typed.setdefault("tr_trust_event", {})[(WS, "claim")] = row


def _unrecovered(database: Any) -> int:
    return int(database.typed["tr_trust_event"][(WS, "claim")]["unrecovered_micro"])


def test_every_reservation_statement_refuses_a_marked_row() -> None:
    class Recorder:
        sql: list[str] = []

        def execute_update(self, sql: str, **_: Any) -> int:
            self.sql.append(sql)
            return 1

        def execute_sql(self, sql: str, **_: Any) -> list[Any]:
            self.sql.append(sql)
            return []

    recorder = Recorder()
    reserve_credit(recorder, param_types, WS, 1)
    reserve_credit_with_pause(recorder, param_types, WS, 1)
    statements = [
        *recorder.sql,
        reserve_credit_statement(param_types, WS, 1)[0],
        reserve_credit_statement(param_types, WS, 1, check_pause=True)[0],
        release_credit_no_debt_statement(param_types, WS, 2, 1, shard=0)[0],
    ]
    assert all("AND NOT COALESCE(in_debt, FALSE)" in sql for sql in statements)


def test_an_overrun_is_covered_from_the_lowest_positive_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([100, 100, 100])
    outcome, held = _authorize(store, key, monkeypatch, estimate=50, candidates=(1, 0, 2))
    assert outcome == AuthorizeOutcome.ACCEPTED
    _settle(store, held, 180)
    # Shard 1 ended at -80 with a signed sum of 120: shard 0, the lowest
    # positive shard, covered it in the same transaction.
    assert _headrooms(database) == [20, 0, 100]
    assert _marks(database) == [False, False, False]
    assert sum(row["total_credits"] for row in _rows(database)) == 300


def test_an_overrun_past_the_balance_marks_every_shard_and_a_marked_shard_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([100, 100])
    outcome, held = _authorize(store, key, monkeypatch, estimate=50, candidates=(0, 1))
    assert outcome == AuthorizeOutcome.ACCEPTED
    _settle(store, held, 260)
    assert _headrooms(database) == [-160, 100]
    assert _marks(database) == [True, True]
    # Shard 1 has headroom, but the workspace is in debt: the answer is 402.
    before = _rows(database)
    assert _authorize(store, key, monkeypatch, estimate=10, candidates=(1, 0)) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _rows(database) == before


def test_money_in_repays_the_lowest_negative_shard_first_and_clears_the_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([0, 0, 100], usage=[160, 30, 0], in_debt=True)
    assert _headrooms(database) == [-160, -30, 100]
    _pay(store, 40, "evt-a")
    # Lowest negative shard first, at most to zero; the sum is still negative.
    assert _headrooms(database) == [-120, -30, 100]
    assert _marks(database) == [True, True, True]
    _pay(store, 100, "evt-b")
    # 100 repays shard 0 to -20, leaving the sum at 50: shard 0 and shard 1
    # are covered from shard 2 and the mark clears.
    assert _headrooms(database) == [0, 0, 50]
    assert _marks(database) == [False, False, False]
    assert _authorize(store, key, monkeypatch, estimate=10, candidates=(2, 0, 1))[0] == (
        AuthorizeOutcome.ACCEPTED
    )


def test_shard_debt_comes_before_payment_claims() -> None:
    store, database, _key = _seed([100], usage=[150], in_debt=True)
    _claim(database, 100)
    _pay(store, 50, "evt-repay")
    assert _headrooms(database) == [0]
    assert _marks(database) == [False]
    assert _unrecovered(database) == 100


def test_what_repays_the_debt_and_clears_the_mark_is_then_offered_to_claims() -> None:
    store, database, _key = _seed([100], usage=[150], in_debt=True)
    _claim(database, 50)
    _pay(store, 100, "evt-repay-and-claim")
    assert _headrooms(database) == [0]
    assert _marks(database) == [False]
    assert _unrecovered(database) == 0


def test_a_return_on_a_marked_workspace_repays_a_lower_shard_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([100, 100])
    outcome, first = _authorize(store, key, monkeypatch, estimate=100, candidates=(1, 0))
    assert outcome == AuthorizeOutcome.ACCEPTED
    outcome, second = _authorize(store, key, monkeypatch, estimate=50, candidates=(0, 1))
    assert outcome == AuthorizeOutcome.ACCEPTED
    _settle(store, second, 260)
    assert _headrooms(database) == [-160, 0]
    assert _marks(database) == [True, True]
    # The first hold settles at nothing: the 100 it frees on shard 1 repays
    # shard 0, the lower negative shard, and the workspace stays marked.
    _settle(store, first, 0)
    assert _headrooms(database) == [-60, 0]
    assert _marks(database) == [True, True]


def test_a_return_on_a_negative_unmarked_shard_absorbs_only_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([100])
    outcome, held = _authorize(store, key, monkeypatch, estimate=10, candidates=(0,))
    assert outcome == AuthorizeOutcome.ACCEPTED
    # A shard left negative before the debt rules existed: 100 - 95 - 10.
    _rows(database)[0]["total_usage"] = 95
    assert _headrooms(database) == [-5]
    _claim(database, 10)
    # The 10 freed first repays the shard's own 5; the claim gets the other 5.
    # Before the debt rules this release offered the claim all 10, debited
    # them back from a shard holding 5 and raised.
    _settle(store, held, 0)
    assert _headrooms(database) == [0]
    assert _marks(database) == [False]
    assert _unrecovered(database) == 5


@pytest.mark.parametrize(
    ("cost", "headrooms", "marks"),
    [(150, [0, 50], [False, False]), (250, [-150, 100], [True, True])],
)
def test_federated_booking_covers_or_marks(
    cost: int, headrooms: list[int], marks: list[bool],
) -> None:
    store, database, _key = _seed([100, 100])
    store._run_in_transaction(
        lambda transaction: _book_usage(transaction, store._param_types, WS, cost, NOW)
    )
    assert _headrooms(database) == headrooms
    assert _marks(database) == marks


@pytest.mark.parametrize("totals", [[100], [50, 50]])
def test_a_stale_mark_heals_when_it_refuses_a_funded_workspace(
    monkeypatch: pytest.MonkeyPatch, totals: list[int],
) -> None:
    store, database, key = _seed(totals, in_debt=True)
    outcome, _held = _authorize(
        store, key, monkeypatch, estimate=10, candidates=tuple(range(len(totals))),
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert _marks(database) == [False] * len(totals)


def test_a_mark_the_balance_bears_out_does_not_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, key = _seed([100, 100], usage=[260, 0], in_debt=True)
    assert _authorize(store, key, monkeypatch, estimate=10, candidates=(1, 0)) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _marks(database) == [True, True]


def test_the_reaper_keeps_going_past_a_reservation_that_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [("res-bad", "auth-bad", 1, 1), ("res-good", "auth-good", 1, 1)]
    seen: list[str] = []

    def finalize(_database: Any, _pt: Any, *, reservation_id: str, **_: Any) -> Any:
        seen.append(reservation_id)
        if reservation_id == "res-bad":
            raise RuntimeError("one workspace's transaction failed")
        return storage_gcp_authorize._ReapOneResult(
            outcome=storage_gcp_authorize.SettleOutcome.SETTLED, released_hold_micro=1,
        )

    class Snapshot:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def execute_sql(self, *_: Any, **__: Any) -> list[Any]:
            return rows

    class Database:
        def snapshot(self, **_: Any) -> Any:
            return Snapshot()

    monkeypatch.setattr(storage_gcp_authorize, "_finalize_reaped_reservation_atomic", finalize)
    result = storage_gcp_authorize.reap_expired_reservations_result(
        Database(), param_types, now=NOW, limit=10,
    )
    assert seen == ["res-bad", "res-good"]
    assert (result.count, result.errors) == (1, 1)
