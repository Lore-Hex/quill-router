"""Parent fidelity and actual entry-path/transaction-shape C1 differentials."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from tests.fakes import settle_c1_main as main
from tests.test_settle_c1 import fixed_time  # noqa: F401 - shared frozen-clock fixture

# _ast_sha256(node) from `git show
# c5486e78:src/trusted_router/storage_gcp_{authorize,counter_dml}.py`.
# Pin source provenance without requiring git/history in CI or mutation copies.
PARENT_AST_SHA256 = {
    'typed_finalize_atomic': 'b4bff6ea9a70cfee37e7b4c9421c29220349bcf470e7c200259272002b434d9b',
    '_dispose_open_transaction': '6519d94aa00de120dcd6d9484129e1072bf9d0dbd015e3cba58d8fbf965a7192',
    '_release_key_or_skip_deleted': 'babb68857395c1da14fa707f4f1c43ebb30c324ddaeba88a620455336933ff09',
    'release_credit': '51a4d4145074e48f23ad27572f518e343eafc55196286acb55f21dc8b750402b',
    'release_key': 'c020e05d55daebb33514b5afd98bc759b3bb4650edf7d815b3383b167806d363',
    '_WINDOW_BUMP_SQL': 'fdfc11ac1cd94597b41681c126c1dee6643e9e4197150d1dc1e7f66556ae63b2',
    '_CURRENT_WINDOW_BUMP_SQL': 'ae3a1d7313e9d6b041a9c87dc963492463bb37ce83aa772716a916e3c75d1f62',
    '_CURRENT_WINDOW_PREDICATE_SQL': '0bb3f848ee5e9efbbc03e50e25a0d90cccdf3f6f71cc26a583ba77f060fec67f',
}


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
def test_frozen_functions_match_c5486e78(name: str) -> None:
    nodes = ast.parse(Path(main.__file__).read_text()).body
    node = next(n for n in nodes if getattr(n, 'name', None) == name or (
        isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets
        )
    ))
    # The sole permitted source change routes the parent's local counter
    # imports to the frozen functions; no statements/SQL are normalized away.
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module == 'tests.fakes.settle_c1_main':
            child.module = 'trusted_router.storage_gcp_counter_dml'
    assert _ast_sha256(node) == PARENT_AST_SHA256[name]


ENTRY_SCENARIOS = [
    'ordinary', 'rollover', 'null_starts', 'debt', 'deleted_key',
    'credit_underflow', 'floor_advance', 'refund', 'refund_debt', 'replay_settled',
]


@pytest.mark.parametrize('entry_path', ['one_commit', 'two_commit'])
@pytest.mark.parametrize('scenario', ENTRY_SCENARIOS)
def test_entry_path_money_and_operations(
    monkeypatch: pytest.MonkeyPatch, scenario: str, entry_path: str,
) -> None:
    """A parent success must really commit T-I + T-F together, without fallback.

    C1 may decline on debt, a missing key or a floor advance; pin those rolled
    back attempts as well as the durable enqueue and sequential retry. Every
    attempt is observed, including commits with no money writes on replay.
    """
    import copy
    from datetime import timedelta
    from typing import Any

    from google.cloud.spanner_v1 import param_types

    from tests.fakes.spanner import _FakeTransaction
    from tests.fakes.spanner_order import credit_before_key, record_statements
    from tests.test_settle_c1 import debt_row, prepare
    from tests.test_settle_one_commit import _one_commit_fixture
    from tests.test_settle_speculative_batch import clone, invoke, state
    from tests.test_spanner_batch_dml import NOW
    from trusted_router import storage_gcp_authorize as current
    from trusted_router import storage_gcp_settle_outbox as outbox
    from trusted_router.spend_windows import window_floors
    from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox

    base = {'null_starts': 'ordinary', 'floor_advance': 'ordinary',
            'refund_debt': 'refund'}.get(scenario, scenario)
    initial, options = prepare(base, True, False)
    _, _, intent, sample = _one_commit_fixture(success=options['success'], refill=True)
    intent.authorization_id = options['authorization_id']
    intent.reservation_id = options['reservation_id']
    if scenario == 'refund_debt':
        initial.typed['tr_trust_event'] = {('workspace', 'debt'): debt_row()}
    key = initial.typed['tr_key_limit'].get(('key', 0))
    if scenario == 'null_starts':
        key.update(day_start=None, week_start=None, month_start=None)
    if scenario == 'floor_advance':
        floors = window_floors(NOW)
        key.update(day_start=floors['daily'], week_start=floors['weekly'],
                   month_start=floors['monthly'])
    monkeypatch.setattr(outbox, '_iso_now', lambda: NOW.isoformat())
    monkeypatch.setattr(outbox, '_iso_after_seconds',
                        lambda seconds: (NOW + timedelta(seconds=seconds)).isoformat())

    def observe(impl: Any) -> Any:
        db = clone(initial)
        clock = NOW
        phase = ''
        attempts = []
        committed = []
        commit_states = []
        batches = []
        original_run, original_commit = db.run_in_transaction, db._try_commit
        original_batch = _FakeTransaction.batch_update

        def run(fn: Any, **kwargs: Any) -> Any:
            def tracked(tx: Any) -> Any:
                attempts.append((phase, tx))
                return fn(tx)
            return original_run(tracked, **kwargs)

        def commit(tx: Any) -> Any:
            result = original_commit(tx)
            if result:
                committed.append(tx)
                commit_states.append((phase, copy.deepcopy(db.settle_outbox),
                                      db.typed['tr_credit_balance'][('workspace', 0)]['total_usage']))
            return result

        def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
            nonlocal clock
            batches.append((tx, [sql.lower() for sql, _, _ in statements]))
            result = original_batch(tx, statements, **kwargs)
            if scenario == 'floor_advance' and phase in {'one_commit', 'finalize'}:
                clock = NOW + timedelta(days=1)
            return result

        with monkeypatch.context() as patch:
            calls = record_statements(patch)
            patch.setattr(db, 'run_in_transaction', run)
            patch.setattr(db, '_try_commit', commit)
            patch.setattr(_FakeTransaction, 'batch_update', batch)
            patch.setattr(main, 'utcnow', lambda: clock)
            patch.setattr(current, 'utcnow', lambda: clock)
            benchmark = SpannerAnalyticsOutbox(db, param_types)
            declined = False
            if entry_path == 'one_commit':
                phase = 'one_commit'
                try:
                    result = invoke(db, dict(copy.deepcopy(options),
                        settle_outbox_intent=copy.deepcopy(intent),
                        intent_initial_delay_seconds=60,
                        benchmark_statement=benchmark.enqueue_statement(sample)), impl)
                except current.OneCommitSettleDeclined:
                    declined = True
            if entry_path == 'two_commit' or declined:
                phase = 'enqueue'
                assert outbox.SpannerSettleOutbox(db, param_types).enqueue(
                    copy.deepcopy(intent), initial_delay_seconds=60,
                ) == outbox.ENQ_INSERTED
                phase = 'finalize'
                result = invoke(db, dict(copy.deepcopy(options),
                    settle_outbox_done=(intent.authorization_id, intent.intent_kind)), impl)
                if result['outcome'] == 'settled':
                    phase = 'benchmark'
                    benchmark.enqueue(sample)

        is_current = impl is current.typed_finalize_atomic
        guard_fallback = is_current and scenario in {
            'debt', 'deleted_key', 'credit_underflow', 'floor_advance',
        }
        replay = scenario == 'replay_settled'
        underflow = scenario == 'credit_underflow'
        expected_decline = entry_path == 'one_commit' and (guard_fallback or replay or underflow)
        assert declined == expected_decline
        expected = []
        if entry_path == 'one_commit':
            expected.append(('one_commit', 'rollback' if expected_decline else 'commit'))
        if entry_path == 'two_commit' or expected_decline:
            expected.append(('enqueue', 'commit'))
            # A floor advance has already happened in the declined one-commit
            # attempt, so the next speculative batch uses the new floors.
            retry = replay or (guard_fallback and not (
                scenario == 'floor_advance' and entry_path == 'one_commit'
            ))
            if retry:
                expected.append(('finalize', 'rollback'))
            expected.append(('finalize', 'rollback' if underflow else 'commit'))
            if not (underflow or replay):
                expected.append(('benchmark', 'commit'))
        actual = [(label, 'commit' if tx in committed else 'rollback') for label, tx in attempts]
        assert actual == expected
        assert all(tx in committed or tx.rolled_back for _, tx in attempts)
        assert db.rollback_calls == sum(status == 'rollback' for _, status in expected)
        assert db.commits == len(committed)
        prefix = 'tr_settle' if options['success'] else 'tr_refund'
        tags = {'one_commit': prefix + '_one_commit',
                'finalize': 'tr_finalize' if options['success'] else 'tr_refund_finalize',
                'enqueue': 'auto:SpannerSettleOutbox.enqueue.insert_txn', 'benchmark': None}
        assert db.transaction_tags == [tags[label] for label, _ in expected]
        assert result['outcome'] == ('error' if underflow else 'already_settled' if replay else 'settled')

        for label, tx in attempts:
            sqls = [sql for called_tx, sql in calls if called_tx is tx]
            credit_before_key(sqls, require_both=False)
            money = [sql for sql in sqls if sql.startswith(('update tr_credit_balance', 'update tr_key_limit'))]
            if label in {'enqueue', 'benchmark'} or replay:
                assert not money
                continue
            assert sqls[0].startswith('select ') and 'tr_reservation' in sqls[0]
            claim = next(i for i, sql in enumerate(sqls) if sql.startswith('update tr_reservation'))
            auth = next(i for i, sql in enumerate(sqls) if sql.startswith('update tr_gateway_authorization'))
            first_money = next(i for i, sql in enumerate(sqls) if sql in money)
            assert claim < auth < first_money
            assert money[0].startswith('update tr_credit_balance')
            if not is_current or scenario in {'refund', 'refund_debt'}:
                # The parent MUST keep credit/debt/key outside its prefix batch.
                assert not any('tr_credit_balance' in sql or 'tr_key_limit' in sql
                               for batched_tx, statements in batches if batched_tx is tx for sql in statements)
            if label == 'one_commit':
                intent_insert = next(i for i, sql in enumerate(sqls) if sql.startswith('insert into tr_settle_outbox'))
                benchmark_insert = next(i for i, sql in enumerate(sqls) if sql.startswith('insert into tr_analytics_outbox'))
                assert auth < intent_insert < benchmark_insert < first_money
            folded = any('update tr_credit_balance' in sql
                         for batched_tx, statements in batches if batched_tx is tx for sql in statements)
            if folded:
                expected_money = ['credit', 'key', 'key']
            elif underflow:
                expected_money = ['credit']
            else:
                credit_count = 3 if scenario == 'debt' else 2 if scenario == 'refund_debt' else 1
                key_count = 2 if scenario in {'rollover', 'null_starts', 'deleted_key', 'floor_advance'} else 1
                expected_money = ['credit'] * credit_count + ['key'] * key_count
            assert ['credit' if 'tr_credit_balance' in sql else 'key' for sql in money] == expected_money

        if not (underflow or replay):
            row = db.settle_outbox[(intent.authorization_id, intent.intent_kind)]
            assert row['status'] == 'done' and row['settle_body'] is None
            assert row['auto_refill_next_attempt_at'] == (NOW + timedelta(seconds=60)).isoformat()
            assert len(db.analytics_outbox) == 1
            assert db.reservations[intent.reservation_id]['terminal_at'] == row['terminal_at']
            assert db.gateway_authorizations[intent.authorization_id]['terminal_at'] == row['terminal_at']
            for label, rows, usage in commit_states:
                assert usage == (0 if label == 'enqueue' else options['actual_micro'])
                assert rows[(intent.authorization_id, intent.intent_kind)]['status'] == (
                    'pending' if label == 'enqueue' else 'done'
                )
            charge_commits = [label for label, rows, usage in commit_states
                              if rows[(intent.authorization_id, intent.intent_kind)]['status'] == 'done'
                              and label != 'benchmark']
            assert charge_commits == ['one_commit' if entry_path == 'one_commit' and not declined else 'finalize']
        if underflow:
            assert db.typed == initial.typed
            assert not db.reservations[options['reservation_id']]['settled']
        if scenario in {'debt', 'refund_debt'}:
            assert db.typed['tr_trust_event'][('workspace', 'debt')]['unrecovered_micro'] == (0 if scenario == 'refund_debt' else 20)
        result.pop('attempts', None)
        return result, state(db), copy.deepcopy(db.analytics_outbox)

    assert observe(main.typed_finalize_atomic) == observe(current.typed_finalize_atomic)
