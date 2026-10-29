"""Negative controls for the shared, narrowly scoped frozen-main trace allowance."""
from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest
from google.cloud.spanner_v1 import param_types

from tests.settle_drain_trace import normalize_legacy_drain_trace
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox, shard_due_statement


def sample():
    database = MagicMock()
    snapshot = database.snapshot.return_value.__enter__.return_value
    snapshot.execute_sql.return_value = []
    SpannerSettleOutbox(database, param_types).due(limit=20, legacy_only=True)
    args, kwargs = snapshot.execute_sql.call_args
    candidate = ('_FakeSnapshot', 'execute_sql', args, kwargs)
    recovery = []
    for shard in range(16):
        sql, params, types = shard_due_statement(param_types, shard, '2026-10-06T00:01:01Z', async_only=True)
        recovery.append(('_FakeSnapshot', 'execute_sql', (sql,), {
            'params': params, 'param_types': types, 'timeout': .2, 'retry': None,
            'request_options': {'priority': 'PRIORITY_LOW'},
        }))
    other = ('_FakeTransaction', 'execute_update', ('UPDATE untouched SET value=@value',), {'params': {'value': 7}})
    return [candidate, other, *recovery], [20, 3, 2, 1, 4, 0]


@pytest.mark.parametrize('expected', [0, 16])
def test_only_verified_delta_is_removed_without_mutating_inputs(expected):
    trace, counts = sample()
    if expected == 0:
        trace = trace[:2]
        counts[0] -= 16
    before = copy.deepcopy((trace, counts))
    actual, rpc = normalize_legacy_drain_trace(trace, counts, expected_recovery_reads=expected, parameter_types=param_types)
    cls, method, args, kwargs = trace[0]
    assert actual == [(cls, method, (args[0].replace('AND async_version IS NULL ', ''),), kwargs), trace[1]]
    assert rpc == [4, 3, 2, 1, 4, 0]
    assert (trace, counts) == before


@pytest.mark.parametrize('damage', [
    '15-reads', '17-reads', 'limit', 'priority', 'second-candidate', 'not-candidate',
    'class', 'method', 'sql', 'timeout', 'retry', 'duplicate-shard',
])
def test_unreviewed_trace_delta_is_rejected(damage):
    trace, counts = sample()
    if damage == '15-reads':
        trace.pop()
    elif damage == '17-reads':
        trace.append(copy.deepcopy(trace[-1]))
    elif damage == 'second-candidate':
        trace.insert(1, copy.deepcopy(trace[0]))
    elif damage == 'not-candidate':
        cls, method, args, kwargs = trace[0]
        # Same index/predicate, but a different query must never be normalized.
        trace[0] = (cls, method, (args[0].replace('SELECT ', 'SELECT DISTINCT ', 1),), kwargs)
    elif damage == 'class':
        trace[-1] = ('_FakeTransaction', *trace[-1][1:])
    elif damage == 'method':
        trace[-1] = (trace[-1][0], 'execute_update', *trace[-1][2:])
    elif damage == 'sql':
        cls, method, args, kwargs = trace[-1]
        trace[-1] = (cls, method, (args[0] + ' ',), kwargs)
    elif damage == 'duplicate-shard':
        trace[-1] = copy.deepcopy(trace[-2])
    else:
        kwargs = trace[-1][3]
        if damage == 'limit':
            kwargs['params']['limit'] = 3
        elif damage == 'priority':
            kwargs['request_options']['priority'] = 'PRIORITY_HIGH'
        elif damage == 'timeout':
            kwargs['timeout'] = .3
        elif damage == 'retry':
            kwargs['retry'] = 'retry-policy'
    with pytest.raises(AssertionError):
        normalize_legacy_drain_trace(trace, counts, expected_recovery_reads=16, parameter_types=param_types)
