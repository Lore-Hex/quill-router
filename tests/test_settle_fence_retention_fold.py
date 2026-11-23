"""The fenced frozen apply carries its retention stamps ahead of the counter tail.

A commit on the async fence that marks no durable intent (the frozen apply,
from the drain or a snapshot settle) stamps retention itself with
done_retention_statements (F1-001). When its batch carries the folded counter
tail, the stamps ride in that batch ahead of the credit lock statement, as a
done-mark's or a one-commit intent's do, instead of in a batch of their own
after the tail: the counter rows are held exclusively for one round trip less.
These tests pin that shape and show the commit does the same thing either way,
on success and on every failure, against the shapes that still run the stamps
as their own batch (the sequential path, a refund, an overrun).
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from google.api_core.exceptions import FailedPrecondition, from_grpc_status
from google.cloud.spanner_v1 import param_types
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status

from tests.fakes.spanner import _FakeTransaction
from tests.fakes.spanner_order import credit_before_key, record_statements, transaction_statements
from tests.settle_release_lock_trace import LOCK
from tests.settle_retention_fold_trace import AUTHORIZATION_STAMP, RESERVATION_STAMP
from tests.test_spanner_batch_dml import NOW, _authorization, _authorize, _database, _state
from trusted_router import storage_gcp_authorize as current
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_generation_records import generation_record_body
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_gcp_settle_outbox import done_retention_statements
from trusted_router.storage_models import Generation

Statement = tuple[str, dict[str, Any], dict[str, Any]]
COUNTER_TABLES = ('tr_credit_balance', 'tr_key_limit', 'tr_trust_event')


def fixture(*, success: bool = True, actual: int = 70, own: str = 'absent', sibling: str = 'absent',
            armed: bool = False) -> tuple[Any, dict[str, Any]]:
    """A frozen apply on the fence: no durable intent to mark, no one-commit intent."""
    db = _database()
    db.now = NOW
    db.typed['tr_key_limit'][('key', 0)].update(
        day_start=datetime.now(UTC), week_start=datetime.now(UTC), month_start=datetime.now(UTC),
    )
    accepted = _authorize(db)
    aid, rid = accepted['authorization_id'], accepted['reservation_id']
    auth = _authorization(aid, rid)
    gen = None
    if success:
        gen = Generation.from_settle_body(
            authorization=auth, provider_name='provider', model_id='model', usage_type='Credits',
            provider='provider', body={}, input_tokens=5, output_tokens=7, actual_cost_microdollars=actual,
        )
        gen.id, gen.created_at = 'stable-generation', NOW.isoformat()
    auth.record_finalization(success=success, actual_microdollars=actual if success else 0,
                             selected_usage_type='Credits', generation=gen)
    if armed:
        db.gateway_authorizations[aid]['terminal_at'] = db.reservations[rid]['terminal_at'] = NOW
    kind = 'settle' if success else 'refund'
    for intent_kind, status in ((kind, own), ('refund' if success else 'settle', sibling)):
        if status != 'absent':
            # Legacy-origin rows: the fence's claim guard looks only at async ones,
            # the stamps' guards at every pending or dead row of the authorization.
            db.settle_outbox[(aid, intent_kind)] = dict(
                authorization_id=aid, intent_kind=intent_kind, reservation_id=rid, status=status,
                settle_origin='typed', actual_cost_micro=actual, attempts=1, async_version=None,
                lease_owner=None, leased_until=None, terminal_at=None,
            )
    return db, dict(
        reservation_id=rid, authorization_id=aid, success=success, actual_micro=actual if success else 0,
        settled_usage_type='Credits', now=NOW, outbox_available=True, authorization=auth,
        auth_body_settled=json_body(auth), generation=gen, persist_generation_record=success,
        generation_writes=[('generation', gen.id, generation_record_body(gen))] if gen is not None else [],
        async_fence=True,
    )


def clone(db: Any) -> Any:
    other = _database()
    other.now = NOW
    for name in ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
                 'generation_records', 'operational_analytics_outbox', 'analytics_outbox',
                 'stage_d_policy_watermarks', 'reservation_idemp'):
        setattr(other, name, copy.deepcopy(getattr(db, name)))
    return other


def state(db: Any) -> Any:
    return (_state(db), copy.deepcopy(db.generation_records), copy.deepcopy(db.operational_analytics_outbox))


def invoke(db: Any, options: dict[str, Any]) -> dict[str, Any]:
    return current.typed_finalize_atomic(db, param_types, operational_analytics_outbox=SpannerOperationalAnalyticsOutbox(
        db, param_types,
    ), **options)


def stamps(options: dict[str, Any]) -> list[Statement]:
    return done_retention_statements(
        param_types, authorization_id=options['authorization_id'],
        intent_kind='settle' if options['success'] else 'refund',
        reservation_id=options['reservation_id'], now=options['now'],
    )


def is_stamp(statement: Statement) -> bool:
    return statement[0].startswith((AUTHORIZATION_STAMP, RESERVATION_STAMP))


Fault = Callable[[Any, list[Statement], int, Callable[..., Any]], Any]


def record_batches(monkeypatch: pytest.MonkeyPatch, fault: Fault | None = None) -> list[tuple[Any, list[Statement]]]:
    """Every batch_update as (transaction, statements); ``fault`` may answer one instead."""
    original = _FakeTransaction.batch_update
    batches: list[tuple[Any, list[Statement]]] = []

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        statements = list(statements)
        batches.append((tx, copy.deepcopy(statements)))
        if fault is not None:
            answer = fault(tx, statements, len(batches), lambda part: original(tx, part, **kwargs))
            if answer is not None:
                return answer
        return original(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, 'batch_update', batch)
    return batches


def first_stamp(statements: list[Statement]) -> int:
    return next(index for index, statement in enumerate(statements) if is_stamp(statement))


def lock_index(statements: list[Statement]) -> int | None:
    found = [index for index, statement in enumerate(statements) if statement[0].startswith(LOCK)]
    assert len(found) <= 1
    return found[0] if found else None


def test_the_fenced_apply_carries_its_stamps_ahead_of_the_counter_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = fixture()
    batches = record_batches(monkeypatch)
    calls = record_statements(monkeypatch)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 1
    # One batch: the stamps immediately ahead of the credit lock, nothing after the tail.
    assert len(batches) == 1
    statements = batches[0][1]
    lock = lock_index(statements)
    assert lock is not None
    assert statements[lock - 2:lock] == stamps(options)
    assert sum(map(is_stamp, statements)) == 2
    assert all(any(table in sql for table in COUNTER_TABLES) for sql, _, _ in statements[lock:])
    # The transaction's last statement is the key release: no round trip
    # follows the counter tail before the commit.
    credit_before_key(transaction_statements(calls), key_last=True)
    aid, rid = options['authorization_id'], options['reservation_id']
    assert db.gateway_authorizations[aid]['terminal_at'] == NOW == db.reservations[rid]['terminal_at']
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    assert db.typed['tr_credit_balance'][('workspace', 0)]['reserved'] == 0
    assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70
    assert db.typed['tr_key_limit'][('key', 0)]['reserved'] == 0


def force_sequential(tx: Any, statements: list[Statement], number: int, run: Callable[..., Any]) -> Any:
    """Zero the folded batch's credit lock once: the commit reruns on the sequential path."""
    lock = lock_index(statements)
    if number != 1 or lock is None:
        return None
    status, counts = run(statements)
    counts[lock] = 0
    return status, counts


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('sibling', ['absent', 'pending', 'dead', 'done', 'release_approved'])
@pytest.mark.parametrize('own', ['absent', 'pending', 'dead', 'done'])
def test_the_folded_stamps_do_what_the_stamps_alone_do(
    monkeypatch: pytest.MonkeyPatch, own: str, sibling: str, armed: bool,
) -> None:
    """The fold against the unchanged sequential commit, whose stamps run alone."""
    initial, options = fixture(own=own, sibling=sibling, armed=armed)
    observations = []
    for fault in (None, force_sequential):
        db = clone(initial)
        with monkeypatch.context() as patch:
            batches = record_batches(patch, fault)
            result = invoke(db, copy.deepcopy(options))
        attempts = result.pop('attempts')
        stamp_batches = [statements for _, statements in batches if any(map(is_stamp, statements))]
        if fault is None:
            # Folded: the stamps ride in the one batch, ahead of the lock.
            assert attempts == 1 and len(batches) == 1
            assert lock_index(batches[0][1]) == first_stamp(batches[0][1]) + 2
        else:
            # Sequential: the rolled-back fold, then the stamps as a batch of
            # their own in the commit that lands.
            assert attempts == 2 and stamp_batches[-1] == stamps(options)
            assert batches[-1][0] is not batches[0][0]
        observations.append((result, state(db)))
        if fault is None:
            folded = db
    assert observations[0] == observations[1]
    # And what the stamps do: an outstanding sibling clears both records; an
    # outstanding intent of its own keeps either from being armed now (the
    # deferred claim has cleared the reservation's); an armed deadline is
    # never moved; otherwise both are armed at the commit's own time.
    outstanding = ('pending', 'dead')
    authorization = folded.gateway_authorizations[options['authorization_id']]['terminal_at']
    reservation = folded.reservations[options['reservation_id']]['terminal_at']
    assert authorization == (None if sibling in outstanding else NOW if armed or own not in outstanding else None)
    assert reservation == (None if sibling in outstanding or own in outstanding else NOW)


@pytest.mark.parametrize('shape', ['folded', 'refund', 'overrun'])
@pytest.mark.parametrize('code', [code_pb2.FAILED_PRECONDITION, code_pb2.UNAVAILABLE, code_pb2.INTERNAL,
                                  code_pb2.ALREADY_EXISTS])
def test_a_failing_stamp_rolls_the_whole_commit_back(monkeypatch: pytest.MonkeyPatch, shape: str, code: int) -> None:
    """Folded or alone, a stamp that fails raises its own error and commits nothing.

    The refund and the overrun still run their stamps as their own batch, the
    shape the fold left; the folded commit fails the same way, and now before
    any counter statement has run.
    """
    if shape == 'refund':
        db, options = fixture(success=False)
    elif shape == 'overrun':
        db, options = fixture(actual=150)
    else:
        db, options = fixture()
    before, commits = state(db), db.commits

    def fail_at_stamp(tx: Any, statements: list[Statement], number: int, run: Callable[..., Any]) -> Any:
        if not any(map(is_stamp, statements)):
            return None
        status, counts = run(statements[:first_stamp(statements)])
        assert status.code == 0
        return Status(code=code, message='injected'), counts

    batches = record_batches(monkeypatch, fail_at_stamp)
    calls = record_statements(monkeypatch)
    expected = type(from_grpc_status(code, 'injected'))
    with pytest.raises(expected):
        invoke(db, options)
    assert state(db) == before and db.commits == commits and db.rollback_calls == 1
    folded = [statements for _, statements in batches if lock_index(statements) is not None]
    assert bool(folded) == (shape == 'folded')
    if shape == 'folded':
        # The batch stopped at the stamp: no counter row was touched.
        assert not any(any(table in sql for table in COUNTER_TABLES) for _, sql in calls)


def test_an_aborted_stamp_retries_the_whole_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db, options = fixture()

    def abort_once(tx: Any, statements: list[Statement], number: int, run: Callable[..., Any]) -> Any:
        if number != 1:
            return None
        _, counts = run(statements[:first_stamp(statements)])
        return Status(code=code_pb2.ABORTED), counts

    batches = record_batches(monkeypatch, abort_once)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 2
    assert db.aborts == 1 and len(batches) == 2 and batches[0][1] == batches[1][1]
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    assert db.typed['tr_key_limit'][('key', 0)]['usage'] == 70


@pytest.mark.parametrize('tail_zero', [False, True])
def test_an_impossible_stamp_count_fails_closed(monkeypatch: pytest.MonkeyPatch, tail_zero: bool) -> None:
    """A stamp is a primary-key UPDATE: it changes 0 or 1 rows. Any other count fails closed.

    With a counter-tail zero in the same batch, the stamp's count now comes
    first: the commit fails at once rather than after the sequential rerun,
    whose own stamps would report the same count. Nothing commits either way.
    """
    db, options = fixture()
    before, commits = state(db), db.commits

    def miscount(tx: Any, statements: list[Statement], number: int, run: Callable[..., Any]) -> Any:
        if not any(map(is_stamp, statements)):
            return None
        status, counts = run(statements)
        counts[first_stamp(statements)] = 2
        lock = lock_index(statements)
        if tail_zero and lock is not None:
            counts[lock] = 0
        return status, counts

    batches = record_batches(monkeypatch, miscount)
    with pytest.raises(FailedPrecondition):
        invoke(db, options)
    assert state(db) == before and db.commits == commits
    assert len(batches) == 1


def test_a_claim_zero_before_a_failing_stamp_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefix's business fallback precedes a later statement's error, as before."""
    db, options = fixture()

    def claim_zero_then_fail(tx: Any, statements: list[Statement], number: int, run: Callable[..., Any]) -> Any:
        if number != 1:
            return None
        _, counts = run(statements[:first_stamp(statements)])
        counts[0] = 0
        return Status(code=code_pb2.FAILED_PRECONDITION, message='injected'), counts

    record_batches(monkeypatch, claim_zero_then_fail)
    result = invoke(db, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 2
    assert db.typed['tr_credit_balance'][('workspace', 0)]['total_usage'] == 70
    aid = options['authorization_id']
    assert db.gateway_authorizations[aid]['terminal_at'] == NOW


def test_a_window_boundary_crossed_by_the_fold_reruns_with_the_stamps_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial, options = fixture()
    folded_db = clone(initial)
    invoke(folded_db, copy.deepcopy(options))
    real = current.window_floors
    samples = 0

    def floors(now: datetime) -> dict[str, datetime]:
        nonlocal samples
        samples += 1
        sampled = real(now)
        # The fold's second sample sees the next day.
        return {**sampled, 'daily': sampled['daily'].replace(year=2099)} if samples == 2 else sampled

    monkeypatch.setattr(current, 'window_floors', floors)
    batches = record_batches(monkeypatch)
    result = invoke(initial, options)
    assert result['outcome'] == 'settled' and result['attempts'] == 2
    assert lock_index(batches[0][1]) is not None and batches[-1][1] == stamps(options)
    assert state(initial) == state(folded_db)


@pytest.mark.parametrize('shape', ['refund', 'overrun'])
def test_shapes_without_the_folded_tail_keep_their_stamps_batch(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    db, options = fixture(success=False) if shape == 'refund' else fixture(actual=150)
    batches = record_batches(monkeypatch)
    calls = record_statements(monkeypatch)
    result = invoke(db, options)
    assert result['outcome'] == 'settled'
    assert all(lock_index(statements) is None for _, statements in batches)
    assert [statements for _, statements in batches if any(map(is_stamp, statements))] == [stamps(options)]
    # Their counter releases still come after the stamps, never under them.
    statements = transaction_statements(calls)
    stamp_at = max(index for index, sql in enumerate(statements) if 'set terminal_at=if(' in sql)
    assert not any(any(table in sql for table in ('tr_credit_balance', 'tr_key_limit'))
                   for sql in statements[:stamp_at])
