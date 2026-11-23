"""Negative controls for the frozen-main release-lock trace allowance."""
from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from google.cloud.spanner_v1 import param_types

from tests.settle_release_lock_trace import HINT, LOCK, Statement, normalize_release_lock_trace
from trusted_router.storage_gcp_counter_dml import (
    lock_credit_release_statement,
    release_credit_no_debt_statement,
    release_key_statement,
)

Record = tuple[str, str, tuple[Any, ...], dict[str, Any]]

FLOORS = {'daily': dt.datetime(2026, 10, 6, tzinfo=dt.UTC), 'weekly': dt.datetime(2026, 10, 5, tzinfo=dt.UTC),
          'monthly': dt.datetime(2026, 10, 1, tzinfo=dt.UTC)}
OTHER: Record = ('_FakeTransaction', 'execute_update', ('UPDATE untouched SET value=@value',),
                 {'params': {'value': 7}, 'param_types': {'value': param_types.INT64}})
SNAPSHOT: Record = ('_FakeSnapshot', 'execute_sql', ('SELECT 1',), {})
CREDIT_SQL = ('UPDATE tr_credit_balance SET reserved = reserved - @hold, total_usage = total_usage + @actual '
              'WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold '
              'THEN RETURN total_credits - total_usage - reserved, COALESCE(in_debt, FALSE)')
CREDIT_PARAMS = {'hold': 50, 'actual': 70, 'ws': 'ws-1', 'shard': 0}
CREDIT_TYPES = {'hold': param_types.INT64, 'actual': param_types.INT64,
                'ws': param_types.STRING, 'shard': param_types.INT64}


def update(statement: Statement) -> Record:
    sql, params, types = statement
    return ('_FakeTransaction', 'execute_update', (sql,), {'params': params, 'param_types': types})


def key(windows: str, *, byok: bool = False, actual: int = 70) -> Statement:
    return release_key_statement(param_types, 'kh-1', 50, actual, book_to_byok=byok,
                                 window_floors=FLOORS, shard=0, windows=windows)


def sequential() -> list[Record]:
    """A settle that releases one credit hold and one key hold, statement by statement."""
    kwargs = {'params': dict(CREDIT_PARAMS), 'param_types': dict(CREDIT_TYPES)}
    return [
        SNAPSHOT, OTHER,
        ('_FakeTransaction', 'execute_sql', (HINT + CREDIT_SQL,), kwargs),
        # The fake's execution of that returning DML, after the hint is gone.
        ('_FakeTransaction', 'execute_update', (CREDIT_SQL,), copy.deepcopy(kwargs)),
        update(key('current')), update(key('any', byok=True)),
    ]


def folded() -> list[Record]:
    """A folded finalize batch and the fake's per-statement records of it."""
    other = (OTHER[2][0], OTHER[3]['params'], OTHER[3]['param_types'])
    batch = [other, lock_credit_release_statement(param_types, 'ws-1', 50, shard=0),
             release_credit_no_debt_statement(param_types, 'ws-1', 50, 30, shard=0),
             key('current', actual=30), key('stale', actual=30)]
    return [SNAPSHOT, ('_FakeTransaction', 'batch_update', (batch,), {}), *[update(entry) for entry in batch]]


def as_frozen_main_issued(trace: list[Record]) -> list[Record]:
    """What the frozen reference records for the same settle: no hint, no lock."""
    out: list[Record] = []
    for cls, method, args, kwargs in trace:
        if method == 'batch_update':
            batch = [(sql.removeprefix(HINT), params, types) for sql, params, types in args[0]
                     if not sql.startswith(LOCK)]
            out.append((cls, method, (batch,), kwargs))
        elif not args[0].startswith(LOCK):
            out.append((cls, method, (args[0].removeprefix(HINT),), kwargs))
    return out


@pytest.mark.parametrize('shape', [sequential, folded])
def test_only_the_verified_release_lock_delta_is_removed_without_mutating_inputs(
    shape: Callable[[], list[Record]],
) -> None:
    trace = shape()
    before = copy.deepcopy(trace)
    actual, changed = normalize_release_lock_trace(trace, parameter_types=param_types)
    assert actual == as_frozen_main_issued(trace)
    assert trace == before
    if shape is sequential:
        assert changed == {'credit_hint': 1, 'key_hint': 2}
    else:
        # The batch entry and the fake's record of each changed statement.
        assert changed == {'lock': 2, 'key_hint': 4}


def test_a_drifted_release_still_differs_from_frozen_main() -> None:
    # Only the hint and the lock statement are taken out: a release whose
    # values changed reaches the oracle's comparison changed.
    trace = sequential()
    cls, method, args, kwargs = trace[4]
    trace[4] = (cls, method, args, {**kwargs, 'params': {**kwargs['params'], 'actual': 71}})
    actual, _ = normalize_release_lock_trace(trace, parameter_types=param_types)
    assert actual != as_frozen_main_issued(sequential())


def unhint(record: Record) -> Record:
    cls, method, args, kwargs = record
    return cls, method, (args[0].removeprefix(HINT),), kwargs


def hint(record: Record) -> Record:
    cls, method, args, kwargs = record
    return cls, method, (HINT + args[0],), kwargs


def edit_batch(trace: list[Record], change: Callable[[list[Statement]], list[Statement]]) -> None:
    cls, method, args, kwargs = trace[1]
    trace[1] = (cls, method, (change(list(args[0])),), kwargs)


@pytest.mark.parametrize('damage', [
    'unhinted-key', 'unhinted-credit', 'unhinted-credit-alone', 'hinted-other', 'key-sql', 'key-params',
    'credit-sql', 'credit-params', 'credit-types', 'stray-returning', 'returning-params',
])
def test_an_unreviewed_sequential_delta_is_rejected(damage: str) -> None:
    trace = sequential()
    if damage == 'unhinted-key':
        trace[4] = unhint(trace[4])
    elif damage == 'unhinted-credit':
        trace[2] = unhint(trace[2])
    elif damage == 'unhinted-credit-alone':
        trace[2] = unhint(trace[2])
        del trace[3]
    elif damage == 'hinted-other':
        trace[1] = hint(trace[1])
    elif damage == 'key-sql':
        cls, method, args, kwargs = trace[5]
        trace[5] = (cls, method, (args[0] + ' ',), kwargs)
    elif damage == 'key-params':
        trace[5][3]['params']['extra'] = 1
    # The credit release and the fake's returning record of it change together.
    elif damage == 'credit-sql':
        for index in (2, 3):
            cls, method, args, kwargs = trace[index]
            trace[index] = (cls, method, (args[0].replace('reserved >= @hold', 'reserved > @hold'),), kwargs)
    elif damage == 'credit-params':
        for index in (2, 3):
            trace[index][3]['params']['extra'] = 1
    elif damage == 'credit-types':
        for index in (2, 3):
            trace[index][3]['param_types']['hold'] = param_types.STRING
    elif damage == 'stray-returning':
        # The fake's returning record without the hinted release before it.
        del trace[2]
    elif damage == 'returning-params':
        trace[3][3]['params']['hold'] = 51
    with pytest.raises(AssertionError):
        normalize_release_lock_trace(trace, parameter_types=param_types)


def _with_lock_param(field: str, value: Any) -> Callable[[list[Statement]], list[Statement]]:
    return lambda batch: [batch[0], (batch[1][0], {**batch[1][1], field: value}, batch[1][2]), *batch[2:]]


@pytest.mark.parametrize('damage', [
    'batch-lock-missing', 'record-lock-missing', 'batch-lock-moved', 'record-lock-moved', 'lock-hold',
    'lock-shard', 'lock-workspace', 'lock-unhinted', 'second-lock', 'hinted-folded', 'unhinted-batch-key',
    'record-lock-alone', 'batch-stray-lock', 'batch-unhinted-credit',
])
def test_an_unreviewed_folded_delta_is_rejected(damage: str) -> None:
    trace = folded()
    if damage == 'batch-lock-missing':
        edit_batch(trace, lambda batch: [entry for index, entry in enumerate(batch) if index != 1])
    elif damage == 'record-lock-missing':
        del trace[3]
    elif damage == 'batch-lock-moved':
        edit_batch(trace, lambda batch: [batch[1], batch[0], *batch[2:]])
    elif damage == 'record-lock-moved':
        trace[2], trace[3] = trace[3], trace[2]
    elif damage in ('lock-hold', 'lock-shard', 'lock-workspace'):
        field, value = {'lock-hold': ('hold', 49), 'lock-shard': ('shard', 1), 'lock-workspace': ('ws', 'ws-2')}[damage]
        trace[3][3]['params'][field] = value
        edit_batch(trace, _with_lock_param(field, value))
    elif damage == 'lock-unhinted':
        trace[3] = unhint(trace[3])
        edit_batch(trace, lambda batch: [batch[0], (batch[1][0].removeprefix(HINT), *batch[1][1:]), *batch[2:]])
    elif damage == 'second-lock':
        trace.insert(3, copy.deepcopy(trace[3]))
        edit_batch(trace, lambda batch: [*batch[:2], *batch[1:]])
    elif damage == 'hinted-folded':
        trace[4] = hint(trace[4])
        edit_batch(trace, lambda batch: [*batch[:2], (HINT + batch[2][0], *batch[2][1:]), *batch[3:]])
    elif damage == 'unhinted-batch-key':
        edit_batch(trace, lambda batch: [*batch[:3], (batch[3][0].removeprefix(HINT), *batch[3][1:]), *batch[4:]])
    elif damage == 'record-lock-alone':
        # A lock record whose folded release never ran.
        del trace[4]
    elif damage == 'batch-stray-lock':
        edit_batch(trace, lambda batch: [*batch, batch[1]])
    elif damage == 'batch-unhinted-credit':
        # A credit release in the batch that is neither hinted nor the folded one.
        unfolded = (CREDIT_SQL.split(' THEN RETURN')[0], dict(CREDIT_PARAMS), dict(CREDIT_TYPES))
        edit_batch(trace, lambda batch: [*batch, unfolded])
    with pytest.raises(AssertionError):
        normalize_release_lock_trace(trace, parameter_types=param_types)
