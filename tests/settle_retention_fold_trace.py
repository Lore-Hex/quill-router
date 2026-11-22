"""Verify the only permitted retention-fold trace delta from frozen main.

A commit on the async fence that marks no durable intent (the frozen apply,
from the drain or a snapshot settle) stamps retention itself with
storage_gcp_settle_outbox.done_retention_statements (F1-001). The frozen
reference (tests/fakes/frozen_main) runs those stamps as a batch of their own
after the speculative batch that carries the counter tail. The live code
carries them in that batch, immediately ahead of the tail's credit lock
statement, so the counter rows are held exclusively for one round trip less.
Its executable sources stay untouched; this module moves the stamps back,
only where every check below holds:

- the batch carries the folded counter tail: the stamps are followed at once
  by the credit lock statement (lock_credit_release_statement);
- the batch writes no tr_settle_outbox row. A done-mark or a one-commit
  intent carries the same stamps ahead of the tail in both legs, and those
  batches are left exactly as they are;
- the stamps are exactly what done_retention_statements builds, from the live
  code, for this batch's own settle: the authorization its settled-mark
  names, the reservation its claim names (the batch's first statement), the
  settle intent kind;
- the fake's per-statement record of every batch statement follows the batch
  record in batch order, so the stamps' records move with them.

The moved stamps become a batch_update of their own right after the batch's
records, as frozen main issued it, and each move adds that one batch RPC back
to transaction_batch_update_calls. With ``fenced`` set, every batch that
carries the folded tail and writes no outbox row must carry the stamps (the
fold must be present); without it, none may. Nothing else is touched, so any
other drift still fails the comparison with the frozen leg.
"""
from __future__ import annotations

from typing import Any

from tests.settle_release_lock_trace import LOCK
from trusted_router import storage_gcp_settle_outbox as outbox

AUTHORIZATION_STAMP = 'UPDATE tr_gateway_authorization SET terminal_at=IF('
RESERVATION_STAMP = 'UPDATE tr_reservation SET terminal_at=IF('
CLAIM = 'UPDATE tr_reservation SET settled=true, '
SETTLED_MARK = 'UPDATE tr_gateway_authorization SET settled=true, '
OUTBOX_WRITES = ('INSERT INTO tr_settle_outbox', 'UPDATE tr_settle_outbox')
BATCH_RPC = 'transaction_batch_update_calls'

Statement = tuple[str, dict[str, Any], dict[str, Any]]
Call = tuple[str, str, tuple, dict]


def _is_stamp(sql: str) -> bool:
    return sql.startswith((AUTHORIZATION_STAMP, RESERVATION_STAMP))


def _record(statement: Statement) -> Call:
    sql, params, types = statement
    return ('_FakeTransaction', 'execute_update', (sql,), {'params': params, 'param_types': types})


def _stamps_for(parameter_types: Any, statements: list[Statement], start: int) -> list[Statement]:
    """The stamps done_retention_statements builds for this batch's own settle."""
    claim, first_stamp = statements[0], statements[start]
    assert claim[0].startswith(CLAIM), f'folded stamps in a batch that does not open with its claim: {claim[0]}'
    marks = [s for s in statements[:start] if s[0].startswith(SETTLED_MARK)]
    assert len(marks) == 1, f'folded stamps without exactly one settled-mark ahead of them: {statements}'
    authorization_id = marks[0][1]['authorization_id']
    params = first_stamp[1]
    assert params['aid'] == authorization_id and params['record_id'] == authorization_id, params
    assert params['kind'] == 'settle', f'only a settle folds its stamps: {params}'
    return outbox.done_retention_statements(
        parameter_types, authorization_id=authorization_id, intent_kind=params['kind'],
        reservation_id=claim[1]['rid'], now=params['now'],
    )


def _split(parameter_types: Any, statements: list[Statement], *, fenced: bool) -> tuple[list[Statement], list[Statement]]:
    """(batch without the folded stamps, the stamps); the stamps are empty when nothing moves."""
    stamps = [index for index, statement in enumerate(statements) if _is_stamp(statement[0])]
    locks = [index for index, statement in enumerate(statements) if statement[0].startswith(LOCK)]
    writes_outbox = any(statement[0].startswith(OUTBOX_WRITES) for statement in statements)
    if not locks or writes_outbox:
        # Not the fence's folded shape: the unfolded stamps batch, the
        # sequential path, a done-mark or a one-commit intent. Both legs agree.
        return statements, []
    assert len(locks) == 1, f'more than one credit lock statement in a batch: {statements}'
    if not stamps:
        assert not fenced, f'fenced folded tail without its retention stamps: {statements}'
        return statements, []
    assert fenced, f'retention stamps folded into an unfenced commit: {statements}'
    start, end = stamps[0], stamps[-1] + 1
    assert stamps == list(range(start, end)), f'retention stamps not contiguous: {statements}'
    assert end == locks[0], f'retention stamps not immediately ahead of the credit lock: {statements}'
    built = _stamps_for(parameter_types, statements, start)
    assert statements[start:end] == built, f'not the stamps the live code builds: {statements[start:end]}'
    return statements[:start] + statements[end:], built


def normalize_retention_fold_trace(trace: list[Call], rpc_counts: list[int], *, rpc_names: tuple[str, ...],
                                   fenced: bool, parameter_types: Any) -> tuple[list[Call], list[int], int]:
    """Return a new trace and counts only after checking every moved call.

    The third value is how many folded batches were split back.
    """
    normalized: list[Call] = []
    moved = 0
    index = 0
    while index < len(trace):
        cls, method, args, kwargs = trace[index]
        if cls != '_FakeTransaction' or method != 'batch_update':
            normalized.append(trace[index])
            index += 1
            continue
        statements = list(args[0])
        kept, stamps = _split(parameter_types, statements, fenced=fenced)
        if not stamps:
            normalized.append(trace[index])
            index += 1
            continue
        assert len(args) == 1 and kwargs == {}, (args[1:], kwargs)
        records = trace[index + 1:index + 1 + len(statements)]
        assert records == [_record(statement) for statement in statements], (
            'the batch statements\' own records do not follow the folded batch in order')
        normalized.append((cls, method, (kept,), kwargs))
        normalized.extend(_record(statement) for statement in kept)
        normalized.append((cls, method, (stamps,), {}))
        normalized.extend(_record(statement) for statement in stamps)
        moved += 1
        index += 1 + len(statements)
    counts = list(rpc_counts)
    counts[rpc_names.index(BATCH_RPC)] += moved
    return normalized, counts, moved
