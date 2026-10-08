"""Cut 2: frozen-main money, exact transaction traces, and held-lock RPCs."""
from __future__ import annotations

import ast
import copy
import hashlib
import itertools
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted
from google.cloud.spanner_v1 import param_types

from tests.fakes import authorize_main_6e793645 as main
from tests.fakes.spanner import FakeSpannerDatabase, _FakeTransaction
from tests.test_authorize_speculative_batch import _options
from tests.test_spanner_batch_dml import NOW, _database, _state
from trusted_router import storage_gcp_authorize as current
from trusted_router import storage_gcp_strict_budget

# Digests from git show 6e793645, independent of the working oracle source.
PARENT_AST_SHA256 = {'authorize_atomic': 'feacd422efb011b89ac8320046b9ce232130c83b3e37980c2035d7ac5fc29740', 'reserve_credit': '1bc9be9a2b5bdcfe000985ad94aa2ae5eb869e9ad1294b8256223703f2ee303f', 'reserve_credit_with_pause': 'd893f82d26f936e20a0aee14a93aaf41af068739ba3807003d9d1b030bf0cf39', 'reserve_key_statement': '77cf28c602a6e9719bdcdff05ee6813f25e243c86e58e17ab7b6e56de96a2f04', 'reserve_key': '6bc7eb1871ca31710d621e270758b2f14535e1ffd6a68cf7f49ba20157a41b62'}


def _ast_sha256(node: ast.AST) -> str:
    def canonical(value: object) -> object:
        if isinstance(value, ast.AST):
            return [type(value).__name__, [
                [name, canonical(child)] for name, child in ast.iter_fields(value)
                # Python 3.12 added this field. Ignore only its empty default;
                # actual type parameters must still change the digest.
                if not (name == 'type_params' and child == [])
            ]]
        if isinstance(value, list):
            return [canonical(child) for child in value]
        return repr(value)

    return hashlib.sha256(json.dumps(canonical(node)).encode()).hexdigest()


@pytest.mark.parametrize('name', PARENT_AST_SHA256)
def test_authorize_main_ast_pin(name):
    tree = ast.parse(Path(main.__file__).read_text())
    node = next(n for n in tree.body if getattr(n, 'name', None) == name)
    assert _ast_sha256(node) == PARENT_AST_SHA256[name]


@pytest.fixture
def hold_trace(monkeypatch):
    """One event per client RPC; statement indexes include ordered batch DML.

    Faults fire once before a selected statement, including in a batch prefix.
    Commit/rollback are explicit events, independent of callback return values.
    """
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=1))
    for module in (main, current, storage_gcp_strict_budget):
        monkeypatch.setattr(module, 'utcnow', lambda: NOW)

    def events(tx):
        if not hasattr(tx, 'hold_events'):
            tx.hold_events = []
            tx.db.hold_traces.append(tx.hold_events)
        return tx.hold_events

    def statement(tx):
        tx.db.hold_statement_count += 1
        if tx.db.hold_statement_count == tx.db.hold_abort_at:
            tx.db.hold_injected = True
            raise Aborted('injected statement abort', errors=[SimpleNamespace()])

    for method, label in [('execute_sql', 'sql'), ('execute_update', 'dml')]:
        original = getattr(_FakeTransaction, method)
        def call(tx, sql, *, params=None, param_types=None, _original=original, _label=label):
            if getattr(tx, '_in_returning', False):
                return _original(tx, sql, params=params, param_types=param_types)
            if not tx._in_batch:
                events(tx).append((_label, [(sql, copy.deepcopy(params), param_types)]))
            statement(tx)
            if (tx.db.hold_hide_winner and sql.startswith('SELECT reservation_id')
                    and tx.db.transaction_tags[-1] != 'tr_authorize_replay'):
                return []
            return _original(tx, sql, params=params, param_types=param_types)
        monkeypatch.setattr(_FakeTransaction, method, call)
    batch = _FakeTransaction.batch_update
    def batched(tx, statements, **kwargs):
        events(tx).append(('batch', copy.deepcopy(statements)))
        return batch(tx, statements, **kwargs)
    monkeypatch.setattr(_FakeTransaction, 'batch_update', batched)
    rollback = _FakeTransaction.rollback
    def rolled_back(tx):
        events(tx).append(('rollback', []))
        return rollback(tx)
    monkeypatch.setattr(_FakeTransaction, 'rollback', rolled_back)
    commit = FakeSpannerDatabase._try_commit
    def committed(db, tx):
        events(tx).append(('commit', []))
        statement(tx)
        return commit(db, tx)
    # The fake's _try_commit does not catch Aborted; inject commit conflicts via
    # its native False/retry contract, after recording the actual commit RPC.
    def commit_conflict(db, tx):
        try:
            return committed(db, tx)
        except Aborted:
            return False
    monkeypatch.setattr(FakeSpannerDatabase, '_try_commit', commit_conflict)


def reset_trace(db, abort_at=None, hide=False):
    db.hold_traces = []
    db.hold_statement_count = 0
    db.hold_abort_at = abort_at
    db.hold_injected = False
    db.hold_hide_winner = hide


def setup_case(case):
    armed, credit, shape, idem, funding, paused, key_state = case
    db = _database()
    db.now = NOW
    reset_trace(db)
    opts = _options()
    if idem != 'fresh':
        assert main.authorize_atomic(db, param_types, **opts)['outcome'] == 'accepted'
    opts.update(
        trust_settings=SimpleNamespace(spend_lease_trust_eligibility_enabled=armed),
        has_credit_candidate=credit, reservation_usage_type='Credits' if credit else 'BYOK',
        speculate_key_limit=shape != 'sequential', skip_key_limit=shape == 'skip',
        strict_budget=shape == 'strict',
    )
    if idem == 'mismatch':
        opts['idempotency_fingerprint'] = 'different'
    row = db.typed['tr_credit_balance'][('workspace', 0)]
    row.update(total_credits=1000 if funding == 'first' else 0,
               billing_pause_causes=['manual'] if paused else [])
    if credit:
        opts['credit_shard_candidates'] = (0, 1)
        db.typed['tr_credit_balance'][('workspace', 1)] = {
            **row, 'shard': 1, 'total_credits': 1000 if funding == 'later' else 0,
        }
    key = db.typed['tr_key_limit'][('key', 0)]
    if key_state == 'missing':
        db.typed['tr_key_limit'].clear()
    elif key_state == 'insufficient':
        key['limit_micro'] = 0
    elif key_state == 'no_hold':
        key['limit_micro'] = None
    elif key_state == 'excluded':
        key['include_byok'] = False
    reset_trace(db, hide=idem == 'race')
    return db, opts


CASES = list(itertools.product(
    [False, True], [False, True], ['speculative', 'skip', 'sequential', 'strict'],
    ['fresh', 'replay', 'mismatch', 'race'], ['first', 'later', 'none'],
    [False, True], ['accepted', 'insufficient', 'missing', 'no_hold', 'excluded'],
))


def run_case(module, case, abort_at=None, sequential=False):
    db, opts = setup_case(case)
    db.hold_abort_at = abort_at
    if sequential:
        opts['speculate_key_limit'] = False
    result = module.authorize_atomic(db, param_types, **opts)
    return result, _state(db), db


_CREDIT_RESERVE = 'UPDATE tr_credit_balance SET reserved = reserved + @est '
_NOT_MARKED = ' AND NOT COALESCE(in_debt, FALSE)'


def _with_debt_mark(statement):
    """Main's credit reservation as it is now: a row marked in debt refuses
    (fast-admission design section 4.7), the one condition added right after
    the headroom test. Every other statement is main's, unchanged."""
    sql, *rest = (statement,) if isinstance(statement, str) else statement
    if sql.startswith(_CREDIT_RESERVE) and _NOT_MARKED not in sql:
        head, at, tail = sql.partition('>= @est')
        sql = head + at + _NOT_MARKED + tail
    return sql if isinstance(statement, str) else (sql, *rest)


def _with_debt_marks(traces):
    return [
        trace if trace is None else [
            (label, [_with_debt_mark(statement) for statement in statements])
            for label, statements in trace
        ]
        for trace in traces
    ]


def expected_traces(case, parent):
    return _with_debt_marks(_main_traces(case, parent))


def _main_traces(case, parent):
    armed, credit, shape, idem, funding, paused, key = case
    traces = copy.deepcopy(parent.hold_traces)
    if not credit or shape not in ('speculative', 'skip') or idem in ('replay', 'mismatch'):
        return traces
    # A missed credit or key prefix always re-enters main's sequential oracle.
    credit_miss = funding != 'first' or (armed and paused)
    key_miss = shape == 'speculative' and key in ('insufficient', 'missing', 'no_hold')
    if credit_miss or key_miss:
        _, _, sequential = run_case(main, case, sequential=True)
        return [None, *sequential.hold_traces]
    # Successful prefix: move precisely the original credit RPC into the batch.
    first = traces[0]
    credit_event = first.pop(1)
    sql, params, types = credit_event[1][0]
    sql = sql.removesuffix(' THEN RETURN billing_pause_causes, pause_epoch')
    if armed:
        sql += ' AND COALESCE(ARRAY_LENGTH(billing_pause_causes), 0) = 0'
    assert first[1][0] == 'batch'
    first[1][1].insert(0, (sql, params, types))
    return traces


@pytest.mark.parametrize('case', CASES, ids=lambda case: '-'.join(map(str, case)))
def test_frozen_main_matrix(hold_trace, case):
    old_result, old_state, parent = run_case(main, case)
    result, state, db = run_case(current, case)
    assert (result, state) == (old_result, old_state)
    expected = expected_traces(case, parent)
    if expected[0] is None:
        # Rollback before any classification RPC, even when INSERTs ran.
        assert [e[0] for e in db.hold_traces[0]] == ['sql', 'batch', 'rollback']
        batch = db.hold_traces[0][1][1]
        assert batch[0][0].startswith('UPDATE tr_credit_balance')
        assert len(batch) == (4 if case[2] == 'speculative' else 3)
        assert db.hold_traces[1:] == expected[1:]
    else:
        assert db.hold_traces == expected
    # Inject ABORTED at EVERY statement/commit in BOTH actual transaction paths.
    # Each retry must retain all ids/timestamps and the exact durable outcome.
    for module, baseline, baseline_result, baseline_state in (
        (main, parent, old_result, old_state), (current, db, result, state),
    ):
        for index in range(1, baseline.hold_statement_count + 1):
            retried, durable, faulted = run_case(module, case, abort_at=index)
            assert faulted.hold_injected
            assert (retried, durable) == (baseline_result, baseline_state)
            # Strip the discarded aborted attempt: the remaining complete
            # attempts must be identical to the no-fault execution.
            complete = [t for t in faulted.hold_traces if t[-1][0] in ('commit', 'rollback')]
            # A commit conflict is also a discarded attempt.
            if len(complete) > len(baseline.hold_traces):
                complete.pop(next(i for i, t in enumerate(complete) if t[-1][0] == 'commit'))
            assert complete == baseline.hold_traces


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('shape', ['speculative', 'skip'])
def test_only_commit_after_first_credit_write(hold_trace, armed, shape):
    case = (armed, True, shape, 'fresh', 'first', False, 'accepted')
    for module, after in ((main, 2), (current, 1)):
        _, _, db = run_case(module, case)
        assert len(db.hold_traces) == 1
        trace = db.hold_traces[0]
        first_write = next(i for i, (_, statements) in enumerate(trace)
                           if any(sql.startswith('UPDATE tr_credit_balance') for sql, _, _ in statements))
        assert len(trace) - first_write - 1 == after
        assert trace[-1] == ('commit', [])
        assert trace[0][1][0][0].startswith('SELECT reservation_id')


@pytest.mark.parametrize('causes', [None, [], [''], ['[]'], ['["x"]'], [' '], ['[ ]'], ['x'], ['x', 'y']])
def test_pause_predicate_matches_python_for_ddl_values(causes):
    from trusted_router.storage_gcp_counter_dml import reserve_credit_statement
    from trusted_router.trust_eligibility import billing_paused_row

    db = _database()
    db.typed['tr_credit_balance'][('workspace', 0)]['billing_pause_causes'] = causes
    sql, params, types = reserve_credit_statement(param_types, 'workspace', 100, check_pause=True)
    count = _FakeTransaction(db).execute_update(sql, params=params, param_types=types)
    assert count == int(not billing_paused_row([causes, None]))


@pytest.mark.parametrize('causes,paused', [(None, False), ('', False), ('[]', False),
                                          ('["x"]', True), (' ', True), ('[ ]', True)])
def test_historical_scalar_pause_predicate(causes, paused):
    # Historical Python inputs are pinned too, but the DDL cannot store these
    # scalar strings. Native SQL cases test them as individual array elements.
    from trusted_router.trust_eligibility import billing_paused_row

    assert billing_paused_row([causes, None]) is paused


@pytest.mark.parametrize('armed', [False, True])
@pytest.mark.parametrize('code', [6, 9])  # ALREADY_EXISTS / FAILED_PRECONDITION
@pytest.mark.parametrize('prefix', [1, 2, 3])
def test_credit_miss_precedes_later_batch_error(monkeypatch, armed, code, prefix):
    from google.rpc.status_pb2 import Status

    case = (armed, True, 'speculative', 'fresh', 'none', False, 'accepted')
    db, opts = setup_case(case)
    original = _FakeTransaction.batch_update
    def partial(tx, statements, **kwargs):
        status, counts = original(tx, statements[:prefix], **kwargs)
        assert status.code == 0 and counts[0] == 0
        return Status(code=code, message='later insert error'), counts
    monkeypatch.setattr(_FakeTransaction, 'batch_update', partial)
    before = _state(db)
    assert current.authorize_atomic(db, param_types, **opts)['outcome'] == 'insufficient_credits'
    assert _state(db) == before
    assert db.commits == 0 and db.rollback_calls == 2
