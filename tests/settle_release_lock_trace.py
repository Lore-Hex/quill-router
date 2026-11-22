"""Verify the only permitted release-lock trace delta from frozen main.

The frozen reference (tests/fakes/frozen_main) predates the releases taking
their counter rows exclusively at the scan, and its executable sources stay
untouched. The live leg's settle and refund trace differs from it in exactly
these calls, each rebuilt here from the live statement code and checked
before it is changed:

- the sequential credit release (storage_gcp_counter_dml.release_credit) and
  every key release (release_key_statement, any window form) start with
  EXCLUSIVE_SCAN_HINT; the hint is removed;
- the folded batch's lock statement (lock_credit_release_statement) sits
  immediately before release_credit_no_debt_statement for the same row and
  hold; it is removed, from the batch_update and from the fake's
  per-statement execute_update record of it.

Every release in the live trace must carry its change: an unhinted release,
or a folded release without its lock, fails here, and so does any other
hinted statement. Nothing else is touched and no RPC count is adjusted (the
lock rides in its release's batch_update), so any other drift still fails
the comparison with the frozen leg.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from trusted_router import storage_gcp_counter_dml as counters

HINT = counters.EXCLUSIVE_SCAN_HINT
CREDIT_RELEASE = 'UPDATE tr_credit_balance SET reserved = reserved - @hold, '
KEY_RELEASE = 'UPDATE tr_key_limit SET reserved = reserved - @hold, '
LOCK = HINT + 'UPDATE tr_credit_balance SET reserved = reserved, '  # noqa: S608 - fixed statement prefix
NO_DEBT_SUBQUERY = 'SELECT 1 FROM tr_trust_event'
CREDIT_PARAMS = {'hold', 'actual', 'ws', 'shard'}
KEY_PARAMS = {'hold', 'actual', 'kh', 'shard', 'day_floor', 'week_floor', 'month_floor'}

Statement = tuple[str, dict[str, Any], dict[str, Any]]


class _ReleaseCreditRecorder:
    """A transaction stand-in that records release_credit's one statement.

    It answers that statement with no row, on which release_credit returns
    before it issues anything else.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_sql(self, sql: str, **kwargs: Any) -> list[Any]:
        self.calls.append((sql, kwargs))
        return []


def _release_credit(parameter_types: Any, params: dict[str, Any]) -> Statement:
    assert set(params) == CREDIT_PARAMS, params
    recorder = _ReleaseCreditRecorder()
    count = counters.release_credit(
        recorder, parameter_types, params['ws'], params['hold'], params['actual'], shard=params['shard'],
    )
    assert count == 0 and len(recorder.calls) == 1, recorder.calls
    sql, kwargs = recorder.calls[0]
    assert set(kwargs) == {'params', 'param_types'}, kwargs
    return sql, kwargs['params'], kwargs['param_types']


def _key_release(parameter_types: Any, sql: str, params: dict[str, Any]) -> Statement:
    assert set(params) == KEY_PARAMS, params
    floors = {'daily': params['day_floor'], 'weekly': params['week_floor'], 'monthly': params['month_floor']}
    for book_to_byok in (False, True):
        for windows in ('current', 'stale', 'any'):
            built = counters.release_key_statement(
                parameter_types, params['kh'], params['hold'], params['actual'],
                book_to_byok=book_to_byok, window_floors=floors, shard=params['shard'], windows=windows,
            )
            if built[0] == sql:
                return built
    raise AssertionError(f'not a key release the live code builds: {sql}')


def _folded_pair(parameter_types: Any, release: Statement) -> tuple[Statement, Statement]:
    """The (lock, release) pair the live folded batch builds for `release`."""
    params = release[1]
    assert set(params) == CREDIT_PARAMS, params
    lock = counters.lock_credit_release_statement(
        parameter_types, params['ws'], params['hold'], shard=params['shard'],
    )
    built = counters.release_credit_no_debt_statement(
        parameter_types, params['ws'], params['hold'], params['actual'], shard=params['shard'],
    )
    return lock, built


def _is_folded_release(sql: str) -> bool:
    return sql.startswith(CREDIT_RELEASE) and NO_DEBT_SUBQUERY in sql


def _unhinted_release(sql: str, parameter_types: Any, params: dict[str, Any], types: dict[str, Any],
                      *, method: str, changed: Counter[str]) -> str:
    """The statement as frozen main issued it: a verified hinted release loses its hint.

    The folded release, a lock statement and the fake's own execution of a
    returning release are the caller's to handle; any other hinted statement
    and any unhinted release fail.
    """
    if not sql.startswith(HINT):
        assert not sql.startswith(KEY_RELEASE), f'key release without the exclusive-scan hint: {sql}'
        assert not sql.startswith(CREDIT_RELEASE), f'credit release without the exclusive-scan hint: {sql}'
        return sql
    body = sql.removeprefix(HINT)
    if method == 'execute_sql' and body.startswith(CREDIT_RELEASE):
        assert (sql, params, types) == _release_credit(parameter_types, params), sql
        changed['credit_hint'] += 1
        return body
    if method == 'execute_update' and body.startswith(KEY_RELEASE):
        assert (sql, params, types) == _key_release(parameter_types, sql, params), sql
        changed['key_hint'] += 1
        return body
    raise AssertionError(f'unreviewed hinted statement ({method}): {sql}')


def _normalize_batch(batch: Any, parameter_types: Any, changed: Counter[str]) -> list[Statement]:
    entries = list(batch)
    kept: list[Statement] = []
    for index, entry in enumerate(entries):
        assert isinstance(entry, tuple) and len(entry) == 3, entry
        sql, params, types = entry
        if sql.startswith(LOCK):
            following = entries[index + 1] if index + 1 < len(entries) else None
            assert following is not None and _is_folded_release(following[0]), (
                f'lock statement not followed by its folded release: {entries}')
            continue  # checked with the release that follows it
        if _is_folded_release(sql):
            lock, built = _folded_pair(parameter_types, entry)
            assert entry == built, entry
            assert index > 0 and entries[index - 1] == lock, f'folded release without its lock: {entries}'
            changed['lock'] += 1
            kept.append(entry)
            continue
        kept.append((_unhinted_release(sql, parameter_types, params, types,
                                       method='execute_update', changed=changed), params, types))
    return kept


def normalize_release_lock_trace(trace: list[tuple[str, str, tuple, dict]], *,
                                 parameter_types: Any) -> tuple[list[tuple[str, str, tuple, dict]], Counter[str]]:
    """Return a new trace only after checking every removed or changed call.

    The counter says how many calls of each kind were changed: credit_hint,
    key_hint and lock (each a batch entry or a per-statement record).
    """
    changed: Counter[str] = Counter()
    normalized: list[tuple[str, str, tuple, dict]] = []
    returning: Statement | None = None
    for index, (cls, method, args, kwargs) in enumerate(trace):
        expected_returning, returning = returning, None
        if cls != '_FakeTransaction' or method not in ('execute_sql', 'execute_update', 'batch_update'):
            normalized.append((cls, method, args, kwargs))
            continue
        if method == 'batch_update':
            normalized.append((cls, method, (_normalize_batch(args[0], parameter_types, changed), *args[1:]),
                               kwargs))
            continue
        sql = args[0]
        assert isinstance(sql, str) and len(args) == 1, args
        params: dict[str, Any] = kwargs.get('params') or {}
        types: dict[str, Any] = kwargs.get('param_types') or {}
        if sql.startswith(LOCK):
            following = trace[index + 1] if index + 1 < len(trace) else None
            assert following is not None and following[1] == 'execute_update' and _is_folded_release(
                following[2][0]), f'lock statement not followed by its folded release: {sql}'
            continue  # checked with the release that follows it
        if _is_folded_release(sql):
            assert method == 'execute_update' and set(kwargs) == {'params', 'param_types'}, (method, kwargs)
            lock, built = _folded_pair(parameter_types, (sql, params, types))
            assert (sql, params, types) == built, sql
            previous = trace[index - 1] if index > 0 else None
            assert previous is not None and previous[:3] == (cls, 'execute_update', (lock[0],)) and (
                previous[3] == {'params': lock[1], 'param_types': lock[2]}), f'folded release without its lock: {sql}'
            changed['lock'] += 1
            normalized.append((cls, method, args, kwargs))
            continue
        if method == 'execute_update' and sql.startswith(CREDIT_RELEASE):
            # The fake runs a returning release's DML through execute_update,
            # after the hint is gone; real Spanner returns its rows only to
            # execute_sql. Only that record may name the release unhinted.
            assert expected_returning == (sql, params, types), f'unhinted credit release: {sql}'
            normalized.append((cls, method, args, kwargs))
            continue
        if sql.startswith(HINT):
            assert set(kwargs) == {'params', 'param_types'}, kwargs
        unhinted = _unhinted_release(sql, parameter_types, params, types, method=method, changed=changed)
        if method == 'execute_sql' and unhinted != sql:
            returning = (unhinted, params, types)
        normalized.append((cls, method, (unhinted,), kwargs))
    return normalized, changed
