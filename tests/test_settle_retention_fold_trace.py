"""Negative controls for the frozen-main retention-fold trace allowance."""
from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from google.cloud.spanner_v1 import param_types

from tests.settle_retention_fold_trace import (
    BATCH_RPC,
    SETTLED_MARK,
    Statement,
    normalize_retention_fold_trace,
)
from trusted_router.storage_gcp_counter_dml import (
    claim_reservation_statement,
    lock_credit_release_statement,
    release_credit_no_debt_statement,
    release_key_statement,
)
from trusted_router.storage_gcp_settle_outbox import done_retention_statements

Record = tuple[str, str, tuple[Any, ...], dict[str, Any]]

NOW = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
FLOORS = {'daily': NOW, 'weekly': NOW - dt.timedelta(days=1), 'monthly': NOW - dt.timedelta(days=5)}
RPC = ('snapshot_execute_sql_calls', 'transaction_execute_sql_calls', 'transaction_execute_update_calls',
       BATCH_RPC, 'commits', 'rollback_calls')
COUNTS = [3, 1, 0, 2, 1, 0]
SNAPSHOT: Record = ('_FakeSnapshot', 'execute_sql', ('SELECT 1',), {})
# The settled-mark's own text is long and payload-bound; its prefix and the
# authorization it names are what the allowance reads.
MARK: Statement = (SETTLED_MARK + 'payload=@payload WHERE authorization_id=@authorization_id AND settled=false',
                   {'authorization_id': 'auth-1', 'payload': '{}'},
                   {'authorization_id': param_types.STRING, 'payload': param_types.STRING})
GENERATION: Statement = ('INSERT INTO tr_generation (generation_id) VALUES (@generation_id)',
                         {'generation_id': 'gen-1'}, {'generation_id': param_types.STRING})
DONE_MARK: Statement = ("UPDATE tr_settle_outbox SET status=@status WHERE authorization_id=@aid AND intent_kind=@kind",
                        {'status': 'done', 'aid': 'auth-1', 'kind': 'settle'},
                        {'status': param_types.STRING, 'aid': param_types.STRING, 'kind': param_types.STRING})
INTENT: Statement = ('INSERT INTO tr_settle_outbox (authorization_id) VALUES (@aid)',
                     {'aid': 'auth-1'}, {'aid': param_types.STRING})


def update(statement: Statement) -> Record:
    sql, params, types = statement
    return ('_FakeTransaction', 'execute_update', (sql,), {'params': params, 'param_types': types})


def claim() -> Statement:
    return claim_reservation_statement(
        param_types, 'res-1', actual_micro=30, settled_usage_type='Credits', terminal_at=NOW,
        defer_retention=True, outbox_available=True, async_fence=True,
    )


def stamps(*, authorization_id: str = 'auth-1', intent_kind: str = 'settle', reservation_id: str = 'res-1',
           now: Any = NOW) -> list[Statement]:
    # The finalize passes its own datetime as `now`, bound as a TIMESTAMP.
    return done_retention_statements(param_types, authorization_id=authorization_id, intent_kind=intent_kind,
                                     reservation_id=reservation_id, now=now)


def tail() -> list[Statement]:
    return [
        lock_credit_release_statement(param_types, 'ws-1', 50, shard=0),
        release_credit_no_debt_statement(param_types, 'ws-1', 50, 30, shard=0),
        *(release_key_statement(param_types, 'kh-1', 50, 30, book_to_byok=False, window_floors=FLOORS,
                                shard=0, windows=windows) for windows in ('current', 'stale')),
    ]


def batch_trace(batch: list[Statement]) -> list[Record]:
    return [('_FakeTransaction', 'batch_update', (batch,), {}), *[update(entry) for entry in batch]]


def folded(head: list[Statement] | None = None) -> list[Record]:
    """The live fenced frozen apply: stamps ride ahead of the counter tail."""
    head = [claim(), MARK, GENERATION] if head is None else head
    return [SNAPSHOT, *batch_trace([*head, *stamps(), *tail()]), SNAPSHOT]


def as_frozen_main_issued() -> list[Record]:
    """What the frozen reference records for the same apply: the stamps after the tail, alone."""
    return [SNAPSHOT, *batch_trace([claim(), MARK, GENERATION, *tail()]), *batch_trace(stamps()), SNAPSHOT]


def normalize(trace: list[Record], *, fenced: bool = True) -> tuple[list[Record], list[int], int]:
    return normalize_retention_fold_trace(trace, COUNTS, rpc_names=RPC, fenced=fenced, parameter_types=param_types)


def test_only_the_verified_fold_is_moved_back_without_mutating_inputs() -> None:
    trace = folded()
    before = copy.deepcopy(trace)
    actual, counts, moved = normalize(trace)
    assert actual == as_frozen_main_issued()
    assert moved == 1
    # Exactly one batch RPC is added back, and no other count changes.
    assert counts == [3, 1, 0, 3, 1, 0] and COUNTS == [3, 1, 0, 2, 1, 0]
    assert trace == before


@pytest.mark.parametrize('fenced', [False, True])
@pytest.mark.parametrize('shape', ['stamps_alone', 'done_mark', 'one_commit', 'sequential'])
def test_shapes_both_legs_share_are_left_exactly_as_they_are(shape: str, fenced: bool) -> None:
    if shape == 'stamps_alone':
        # The unfolded form: a refund, an overrun, a payout, the sequential path.
        trace = [SNAPSHOT, *batch_trace(stamps())]
    elif shape == 'done_mark':
        trace = folded([claim(), MARK, DONE_MARK])
    elif shape == 'one_commit':
        trace = folded([claim(), MARK, INTENT])
    else:
        trace = [SNAPSHOT, update(claim()), update(MARK), *batch_trace(stamps())]
    actual, counts, moved = normalize(copy.deepcopy(trace), fenced=fenced)
    assert (actual, counts, moved) == (trace, COUNTS, 0)


def test_an_unfenced_folded_tail_without_stamps_is_left_alone() -> None:
    trace = [SNAPSHOT, *batch_trace([claim(), MARK, GENERATION, *tail()])]
    assert normalize(copy.deepcopy(trace), fenced=False) == (trace, COUNTS, 0)


def test_a_drifted_statement_still_differs_from_frozen_main() -> None:
    # Only the stamps move: a tail whose values changed reaches the comparison changed.
    trace = folded()
    batch = list(trace[1][2][0])
    index = len(batch) - 3
    batch[index] = (batch[index][0], {**batch[index][1], 'actual': 31}, batch[index][2])
    trace = [SNAPSHOT, *batch_trace(batch), SNAPSHOT]
    actual, _, moved = normalize(trace)
    assert moved == 1 and actual != as_frozen_main_issued()


def test_a_moved_stamp_with_another_timestamp_still_differs_from_frozen_main() -> None:
    # The stamps' timestamp is the commit's own, which the batch names nowhere
    # else; the allowance moves the stamps as they are, so a drifted one
    # reaches the comparison drifted.
    later = NOW + dt.timedelta(seconds=1)
    trace = [SNAPSHOT, *batch_trace([claim(), MARK, GENERATION, *stamps(now=later), *tail()]), SNAPSHOT]
    actual, _, moved = normalize(trace)
    assert moved == 1 and actual != as_frozen_main_issued()


def edit_batch(change: Callable[[list[Statement]], list[Statement]]) -> list[Record]:
    """A folded trace whose batch, and the fake's records of it, are changed together."""
    batch = list(folded()[1][2][0])
    return [SNAPSHOT, *batch_trace(change(batch)), SNAPSHOT]


def with_param(statement: Statement, field: str, value: Any) -> Statement:
    return statement[0], {**statement[1], field: value}, statement[2]


# Index of the first stamp in folded()'s batch, and of the credit lock.
FIRST, LOCK_AT = 3, 5


def test_a_fenced_folded_tail_must_carry_its_stamps() -> None:
    # Fenced, the fold must be present: the stamps run separately again fails.
    with pytest.raises(AssertionError, match='without its retention stamps'):
        normalize(edit_batch(lambda b: b[:FIRST] + b[LOCK_AT:]), fenced=True)


def test_stamps_folded_into_an_unfenced_commit_are_rejected() -> None:
    with pytest.raises(AssertionError, match='unfenced commit'):
        normalize(folded(), fenced=False)


@pytest.mark.parametrize('damage', [
    'gap-before-lock', 'after-lock', 'split-stamps', 'one-stamp', 'extra-stamp',
    'stamp-kind', 'stamp-sql', 'stamp-authorization', 'stamp-reservation', 'mark-authorization',
    'claim-reservation', 'no-claim', 'no-mark', 'two-marks', 'two-locks', 'stamps-reordered',
])
def test_an_unreviewed_batch_delta_is_rejected(damage: str) -> None:
    if damage == 'gap-before-lock':
        trace = edit_batch(lambda b: [*b[:LOCK_AT], GENERATION, *b[LOCK_AT:]])
    elif damage == 'after-lock':
        trace = edit_batch(lambda b: [*b[:FIRST], *b[LOCK_AT:LOCK_AT + 1], *b[FIRST:LOCK_AT], *b[LOCK_AT + 1:]])
    elif damage == 'split-stamps':
        trace = edit_batch(lambda b: [*b[:FIRST + 1], GENERATION, *b[FIRST + 1:]])
    elif damage == 'one-stamp':
        trace = edit_batch(lambda b: [*b[:FIRST], *b[FIRST + 1:]])
    elif damage == 'extra-stamp':
        trace = edit_batch(lambda b: [*b[:LOCK_AT], b[FIRST + 1], *b[LOCK_AT:]])
    elif damage == 'stamps-reordered':
        trace = edit_batch(lambda b: [*b[:FIRST], b[FIRST + 1], b[FIRST], *b[LOCK_AT:]])
    elif damage in ('stamp-kind', 'stamp-authorization', 'stamp-reservation'):
        override = {'stamp-kind': {'intent_kind': 'refund'},
                    'stamp-authorization': {'authorization_id': 'auth-2'},
                    'stamp-reservation': {'reservation_id': 'res-2'}}[damage]
        trace = edit_batch(lambda b: [*b[:FIRST], *stamps(**override), *b[LOCK_AT:]])
    elif damage == 'stamp-sql':
        trace = edit_batch(lambda b: [*b[:FIRST], (b[FIRST][0] + ' ', *b[FIRST][1:]), *b[FIRST + 1:]])
    elif damage == 'mark-authorization':
        trace = edit_batch(lambda b: [b[0], with_param(b[1], 'authorization_id', 'auth-2'), *b[2:]])
    elif damage == 'claim-reservation':
        trace = edit_batch(lambda b: [with_param(b[0], 'rid', 'res-2'), *b[1:]])
    elif damage == 'no-claim':
        trace = edit_batch(lambda b: b[1:])
    elif damage == 'no-mark':
        trace = edit_batch(lambda b: [b[0], *b[2:]])
    elif damage == 'two-marks':
        trace = edit_batch(lambda b: [b[0], b[1], *b[1:]])
    else:  # two-locks
        trace = edit_batch(lambda b: [*b, b[LOCK_AT]])
    with pytest.raises(AssertionError):
        normalize(trace)


@pytest.mark.parametrize('damage', ['record-missing', 'record-reordered', 'record-params', 'batch-kwargs',
                                    'batch-extra-arg', 'record-stray'])
def test_an_unreviewed_record_delta_is_rejected(damage: str) -> None:
    trace = folded()
    if damage == 'record-missing':
        del trace[2 + FIRST]
    elif damage == 'record-reordered':
        trace[2 + FIRST], trace[3 + FIRST] = trace[3 + FIRST], trace[2 + FIRST]
    elif damage == 'record-params':
        trace[2 + FIRST][3]['params']['now'] = NOW + dt.timedelta(seconds=1)
    elif damage == 'batch-kwargs':
        cls, method, args, _ = trace[1]
        trace[1] = (cls, method, args, {'timeout': 1})
    elif damage == 'batch-extra-arg':
        cls, method, args, kwargs = trace[1]
        trace[1] = (cls, method, (*args, None), kwargs)
    else:  # record-stray: another call between the batch and its records
        trace.insert(2, SNAPSHOT)
    with pytest.raises(AssertionError):
        normalize(trace)
