"""Frozen c2c8f606 (PR B stack) gateway + enqueue vs actual legacy entry effects."""
# ruff: noqa: F811, S102 - shared fixture and AST-pinned source execution
from __future__ import annotations

import ast
import copy
import json
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
    'async_settle_main.txt': '6655bf2c51630ea7cc24063777adafad1ecafe29249dd03a6e7262978ed8bda3',
    'async_settle_enqueue_main.txt': 'd1d895e4927690e5353674f6b7bc9407539949bddcc74613f1458829177f3c12',
}


def test_frozen_source_pins():
    # Interpreter-stable canonical AST digest (ast.dump output differs across Python versions).
    for filename, digest in PINS.items():
        assert _ast_sha256(ast.parse((FROZEN/filename).read_text())) == digest


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('outbox_enabled', [False, True])
def test_frozen_main_effects_and_operation_trace(env, monkeypatch, enabled, success, outbox_enabled):
    body, auth, _ = prepare(env)
    env[3].async_settle_enabled = enabled
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
    from trusted_router import storage_gcp_authorize, storage_models
    monkeypatch.setattr(storage_gcp_authorize, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_models, 'utcnow', lambda: clock)
    frozen_globals = dict(vars(gateway))
    exec(compile((FROZEN/'async_settle_main.txt').read_text(), 'frozen_gateway', 'exec'), frozen_globals)
    frozen_settle = frozen_globals['_settle_gateway_authorization']
    outbox_globals = dict(vars(outbox))
    exec(compile((FROZEN/'async_settle_enqueue_main.txt').read_text(), 'frozen_enqueue', 'exec'), outbox_globals)
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
            calls = record_statements(patch)
            if frozen:
                patch.setattr(outbox.SpannerSettleOutbox, 'enqueue', outbox_globals['enqueue'])
            function = frozen_settle if frozen else gateway._settle_gateway_authorization
            result = function(repair, success=success, settings=env[3])
            result.get('data', {}).pop('timing', None)
            outputs.append((result, {name: copy.deepcopy(getattr(db, name)) for name in names},
                            [sql for _, sql in calls]))
    assert outputs[0] == outputs[1]
