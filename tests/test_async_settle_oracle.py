"""Frozen c2c8f606 (PR B stack) gateway + enqueue vs actual legacy entry effects."""
# ruff: noqa: F811, S102 - shared fixture and AST-pinned source execution
from __future__ import annotations

import ast
import copy
import json
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
from tests.fakes.spanner_order import record_statements
from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_authorize_hold_time import _ast_sha256
from tests.test_billing_snapshot import endpoint_from_candidate
from trusted_router import storage_gcp_settle_outbox as outbox
from trusted_router.catalog_data import Model
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewaySettleRequest

FROZEN = Path(__file__).parent/'fakes'
PINS = {
    'async_settle_storage_gcp_settle_outbox_main.txt': '428fd9249f4be5bdc48230100612a63ccc13fe4998ca13e073a61da66d5dbdea',
    'async_settle_storage_gcp_counter_dml_main.txt': 'b5e1b5626728fe0e1b2e984ba2a4bb5498abbefd25ad445d12d7d8b45c680ec6',
    'async_settle_storage_gcp_authorize_main.txt': '5d35774336ffe5b230e98553e8521ff6efd683c2a5720d2e8e607ea6579d4b8e',
    'async_settle_main.txt': '6655bf2c51630ea7cc24063777adafad1ecafe29249dd03a6e7262978ed8bda3',
    'async_settle_enqueue_main.txt': 'd1d895e4927690e5353674f6b7bc9407539949bddcc74613f1458829177f3c12',
}


# The one statement the current finalize adds to frozen main's trace: the
# credit row's lock, in the folded batch, in front of the release it locks for
# (storage_gcp_counter_dml.lock_credit_release_statement).
CREDIT_RELEASE_LOCK = (
    '@{LOCK_SCANNED_RANGES=EXCLUSIVE} UPDATE tr_credit_balance '
    'SET reserved = reserved, total_usage = total_usage '
    'WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold '
    'AND NOT COALESCE(in_debt, FALSE)'
)


def _without_credit_release_lock(
    statements: list[str], raw_calls: list[tuple[str, str, tuple, dict]],
) -> tuple[list[str], list[tuple[str, str, tuple, dict]]]:
    """The current trace with the lock statement taken out, after checking it.

    It must sit immediately before the folded batch's credit release, name that
    release's row and hold and nothing else, and appear nowhere else.
    """
    folded = [i for i, sql in enumerate(statements)
              if sql.startswith('update tr_credit_balance set reserved = reserved - @hold')
              and 'not exists' in sql]
    locks = [i for i, sql in enumerate(statements) if sql == CREDIT_RELEASE_LOCK.lower()]
    assert locks == [i - 1 for i in folded]
    kept_statements = [sql for i, sql in enumerate(statements) if i not in locks]
    kept_calls: list[tuple[str, str, tuple, dict]] = []
    removed = 0
    for cls, method, args, kwargs in raw_calls:
        if method == 'execute_update' and args[0] == CREDIT_RELEASE_LOCK:
            removed += 1
            continue
        if method == 'batch_update':
            batch = list(args[0])
            for index in [i for i, (sql, _, _) in enumerate(batch) if sql == CREDIT_RELEASE_LOCK]:
                _, lock_params, lock_types = batch[index]
                release_sql, release_params, release_types = batch[index + 1]
                assert 'SELECT 1 FROM tr_trust_event' in release_sql
                assert lock_params == {k: v for k, v in release_params.items() if k != 'actual'}
                assert lock_types == {k: v for k, v in release_types.items() if k != 'actual'}
            args = ([entry for entry in batch if entry[0] != CREDIT_RELEASE_LOCK], *args[1:])
        kept_calls.append((cls, method, args, kwargs))
    assert removed == len(locks)
    return kept_statements, kept_calls


def test_frozen_source_pins():
    # Interpreter-stable canonical AST digest (ast.dump output differs across Python versions).
    for filename, digest in PINS.items():
        assert _ast_sha256(ast.parse((FROZEN/filename).read_text())) == digest


@pytest.mark.parametrize('scenario', ['ordinary', 'unresolved', 'refresh'])
@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('outbox_enabled', [False, True])
def test_frozen_main_effects_and_operation_trace(env, monkeypatch, scenario, success, outbox_enabled):
    body, auth, _ = prepare(env)
    env[3].async_settle_enabled = False
    env[3].async_settle_protection = False
    env[0].settle_outbox._async_fence = False
    env[3].settle_outbox_enabled = outbox_enabled
    endpoints = {c['endpoint_id']: endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(
            id=endpoint.model_id, name='oracle', provider=endpoint.provider,
            context_length=1_000_000, prepaid_available=True))
    monkeypatch.setattr(gateway, 'endpoint_for_id', endpoints.get)
    repair = GatewaySettleRequest(**json.loads(row_for(env, body).settle_body))
    clock = datetime(2026, 10, 6, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(gateway.dt, 'datetime', Clock)
    monkeypatch.setattr(outbox, '_iso_now', lambda: clock.isoformat())
    from trusted_router import acquisition, storage_gcp_authorize, storage_models
    monkeypatch.setattr(storage_gcp_authorize, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_models, 'utcnow', lambda: clock)
    frozen_globals = dict(vars(gateway))
    exec(compile((FROZEN/'async_settle_main.txt').read_text(), str(FROZEN/'async_settle_main.txt'), 'exec'), frozen_globals)
    frozen_settle = frozen_globals['_settle_gateway_authorization']
    outbox_globals = dict(vars(outbox))
    exec(compile((FROZEN/'async_settle_enqueue_main.txt').read_text(), str(FROZEN/'async_settle_enqueue_main.txt'), 'exec'), outbox_globals)
    from trusted_router import storage_gcp_counter_dml as counters
    frozen_counter = dict(vars(counters))
    frozen_storage = dict(vars(storage_gcp_authorize))
    for module, namespace in (('storage_gcp_counter_dml', frozen_counter),
                              ('storage_gcp_settle_outbox', outbox_globals),
                              ('storage_gcp_authorize', frozen_storage)):
        path = FROZEN / f'async_settle_{module}_main.txt'
        exec(compile(path.read_text(), str(path), 'exec'), namespace)
    frozen_storage['claim_reservation_statement'] = frozen_counter['claim_reservation_statement']
    frozen_storage['resolved_intent_statements'] = outbox_globals['resolved_intent_statements']

    def frozen_finalize(*args, **kwargs):
        # The new explicit gate is plumbing, not a frozen-main input.
        kwargs.pop('async_fence', None)
        return frozen_storage['typed_finalize_atomic'](*args, **kwargs)

    if scenario == 'unresolved':
        # Unsuccessful finalize leaves the authorization unresolved, exposing
        # unexpected reconciliation point reads even with BOTH flags off.
        monkeypatch.setattr(type(env[0]), 'typed_settle_one_commit_result', lambda *a, **kw: None)
        env[1].reservations.pop(auth.credit_reservation_id)
    if scenario == 'refresh':
        monkeypatch.setattr(type(env[0]), 'typed_settle_one_commit_result', lambda *a, **kw: None)
        row = row_for(env, body)
        row.async_version = None
        row.intent_kind = 'settle' if success else 'refund'
        outbox.SpannerSettleOutbox(env[1], env[0]._param_types).enqueue(row)
    db = env[1]
    names = ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
             'generation_records', 'operational_analytics_outbox', 'analytics_outbox',
             'reservation_idemp')
    saved = {name: copy.deepcopy(getattr(db, name)) for name in names}
    outputs = []
    for frozen in (True, False):
        for name, val in saved.items():
            setattr(db, name, copy.deepcopy(val))
        with monkeypatch.context() as patch:
            patch.setattr(storage_gcp_authorize, "_OUTBOX_AVAILABILITY_CACHE", {})
            patch.setattr(acquisition, "_usage_check_after", OrderedDict())
            calls = record_statements(patch)
            if frozen:
                patch.setattr(outbox.SpannerSettleOutbox, 'enqueue', outbox_globals['enqueue'])
                patch.setattr(counters, 'claim_reservation', frozen_counter['claim_reservation'])
                patch.setattr(storage_gcp_authorize, 'typed_finalize_atomic', frozen_finalize)
            # Preserve exact SQL bytes, parameters and types, including batch
            # boundaries and snapshot reads; normalized SQL alone misses drift.
            raw_calls = []

            def record_raw(cls, method, trace):
                original = getattr(cls, method)

                def wrapped(self, *args, **kwargs):
                    trace.append((cls.__name__, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
                    return original(self, *args, **kwargs)

                patch.setattr(cls, method, wrapped)

            for method in ('execute_update', 'execute_sql', 'batch_update'):
                record_raw(_FakeTransaction, method, raw_calls)
            record_raw(_FakeSnapshot, 'execute_sql', raw_calls)
            rpc_counters = ('snapshot_execute_sql_calls', 'transaction_execute_sql_calls',
                            'transaction_execute_update_calls', 'transaction_batch_update_calls',
                            'commits', 'rollback_calls')
            rpc_before = [getattr(db, name) for name in rpc_counters]
            snapshot_start = len(db.snapshot_sql)
            function = frozen_settle if frozen else gateway._settle_gateway_authorization
            result = function(repair, success=success, settings=env[3])
            result.get('data', {}).pop('timing', None)
            outputs.append((result, {name: copy.deepcopy(getattr(db, name)) for name in names},
                            [sql for _, sql in calls], raw_calls,
                            [getattr(db, name) - before for name, before in zip(rpc_counters, rpc_before, strict=True)],
                            list(zip(db.snapshot_sql[snapshot_start:], db.snapshot_sql_params[snapshot_start:], strict=True))))
    # The effects, the RPC counts and the snapshot reads are compared as they
    # are. The statement traces are equal but for the one lock statement the
    # current finalize puts in front of a folded credit release.
    frozen_out, current_out = outputs
    statements, raw_calls = _without_credit_release_lock(current_out[2], current_out[3])
    assert frozen_out == (current_out[0], current_out[1], statements, raw_calls, *current_out[4:])
    assert CREDIT_RELEASE_LOCK.lower() not in frozen_out[2]
    assert (len(current_out[2]) - len(frozen_out[2])) == int(
        success and scenario != 'unresolved'
    )


@pytest.mark.parametrize('scenario', ['ordinary', 'unresolved', 'refresh'])
@pytest.mark.parametrize('success', [False, True])
def test_frozen_main_effects_with_nonmember_shadow(env, monkeypatch, scenario, success):
    from trusted_router.config import Settings
    env[3]._async_settle_shadow_workspace_ids = Settings(
        environment="test", async_settle_shadow_workspaces="nonmember").async_settle_shadow_workspace_ids
    test_frozen_main_effects_and_operation_trace(env, monkeypatch, scenario, success, True)
