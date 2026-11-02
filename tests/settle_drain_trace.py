"""Verify the only permitted watchdog trace delta from frozen main.

These oracles run one legacy drain with no accepted async rows. The watchdog
adds one NULL-only bulk candidate predicate and, unless its budget expired,
one bounded advisory read of each shard. No other trace/RPC change is allowed.
"""
from __future__ import annotations

from trusted_router import storage_gcp_settle_outbox as storage

LEGACY_PREDICATE = 'AND async_version IS NULL '
RECOVERY_PREDICATE = 'AND async_version IS NOT NULL '


def normalize_legacy_drain_trace(trace, rpc_counts, *, expected_recovery_reads, parameter_types):
    """Return new trace/count lists only after checking every removed SQL call."""
    assert expected_recovery_reads in (0, 16)
    candidate_sql = (
        f"SELECT {', '.join(storage.OUTBOX_COLUMNS)} FROM tr_settle_outbox"  # noqa: S608 - fixed column inventory
        "@{FORCE_INDEX=tr_settle_outbox_due_v2} "
        "WHERE queue_shard IS NOT NULL AND next_attempt_at IS NOT NULL "
        "AND status='pending' AND next_attempt_at <= @now "
        + LEGACY_PREDICATE + "ORDER BY next_attempt_at LIMIT @limit"
    )
    normalized, recovery_shards = [], []
    candidate_count = 0
    for cls, method, args, kwargs in trace:
        sql = args[0] if args and isinstance(args[0], str) else ''
        if RECOVERY_PREDICATE.rstrip() in sql:
            assert cls == '_FakeSnapshot' and method == 'execute_sql'
            params = kwargs['params']
            expected_sql, expected_params, types = storage.shard_due_statement(
                parameter_types, params['shard'], params['now'], async_only=True,
            )
            assert args == (expected_sql,)
            assert set(params) == set(expected_params)
            assert params['limit'] == 2
            assert kwargs['param_types'] == types
            assert kwargs['timeout'] == .2 and kwargs['retry'] is None
            assert kwargs['request_options'] == {'priority': 'PRIORITY_LOW'}
            assert set(kwargs) == {'params', 'param_types', 'timeout', 'retry', 'request_options'}
            recovery_shards.append(params['shard'])
            continue
        if LEGACY_PREDICATE.rstrip() in sql:
            assert cls == '_FakeSnapshot' and method == 'execute_sql'
            assert 'FORCE_INDEX=tr_settle_outbox_due_v2' in sql
            assert args == (candidate_sql,)
            candidate_count += 1
            args = (sql.replace(LEGACY_PREDICATE, '', 1),)
        normalized.append((cls, method, args, kwargs))
    assert len(recovery_shards) == expected_recovery_reads
    assert recovery_shards == list(range(expected_recovery_reads))
    assert candidate_count == 1
    counts = list(rpc_counts)
    assert counts[0] >= expected_recovery_reads
    counts[0] -= expected_recovery_reads
    return normalized, counts
