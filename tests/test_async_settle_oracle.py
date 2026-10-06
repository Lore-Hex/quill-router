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
            snapshot_start = len(db.snapshot_sql)
            function = frozen_settle if frozen else gateway._settle_gateway_authorization
            result = function(repair, success=success, settings=env[3])
            result.get('data', {}).pop('timing', None)
            outputs.append((result, {name: copy.deepcopy(getattr(db, name)) for name in names},
                            [sql for _, sql in calls],
                            list(zip(db.snapshot_sql[snapshot_start:], db.snapshot_sql_params[snapshot_start:], strict=True))))
    assert outputs[0] == outputs[1]
