"""Frozen e504e22f authorize effects plus live route metadata integration."""
from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest
from google.cloud.spanner_v1 import param_types

from tests.test_async_settle_admission import request
from tests.test_async_settle_ticket import runtime, settings
from tests.test_authorize_hold_time import (
    _ast_sha256,
    expected_traces,
    hold_trace,  # noqa: F401 - shared RPC tracing fixture
    main,
    run_case,
    setup_case,
)
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - shared routing fixture
)
from tests.test_spanner_batch_dml import _state
from trusted_router import storage_gcp_authorize as current
from trusted_router.routes.internal import gateway

# Exact authorize_atomic from the supplied merged-main base e504e22fd4b5695f13ba4bd262a372a23efdc9c0.
ATOMIC_SHA256 = '725daa8cc4f5e0faa92c5d465beb54a97fe563b4c519c0d72f2a16170d2e0042'


def test_atomic_source_is_byte_identical_in_ast():
    tree = ast.parse(Path(current.__file__).read_text())
    node = next(n for n in tree.body if getattr(n, 'name', None) == 'authorize_atomic')
    assert _ast_sha256(node) == ATOMIC_SHA256


@pytest.mark.usefixtures('hold_trace')
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('shape', ['speculative', 'skip', 'sequential', 'strict'])
@pytest.mark.parametrize('idem', ['fresh', 'replay', 'mismatch', 'race'])
def test_flag_preserves_frozen_main_durable_effect_and_operations(enabled, shape, idem):
    case = (True, True, shape, idem, 'first', False, 'accepted')
    old_result, old_state, parent = run_case(main, case)
    db, opts = setup_case(case)
    opts['trust_settings'].async_settle_enabled = enabled
    result = current.authorize_atomic(db, param_types, **opts)
    assert result == old_result and _state(db) == old_state
    assert db.hold_traces == expected_traces(case, parent)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('optin', [False, True])
def test_live_authorize_metadata_after_identical_hold(monkeypatch, enabled, optin):
    store, db, key = _seed_typed_gateway_store()
    config = settings()
    config.async_settle_enabled = enabled
    rt = runtime()
    calls = []
    original = rt.admission.read
    def read(ws):
        # Admission is strictly after successful commit, outside the hold.
        assert db.commits > 0 and db.gateway_authorizations
        calls.append(ws)
        return original(ws)
    rt.admission.read = read
    monkeypatch.setattr(gateway, '_new_gateway_authorization_id', lambda: 'auth-live')
    b = _body(key.hash)
    b.route_type = 'chat.completions'
    b.invocation_nonce = 'nonce-live'
    result = gateway._authorize_gateway_sync(request(rt, optin), b, config)['data']
    assert bool(result.get('async_eligible')) is (enabled and optin)
    assert len(calls) == int(enabled and optin)
    assert ('billing_snapshot' in result) is optin
    auth = store.get_gateway_authorization('auth-live')
    before = copy.deepcopy(_state(db))
    # Replay retains exactly the original nonce/hold and never re-freezes prices.
    b.invocation_nonce = 'new-nonce'
    replay = gateway._authorize_gateway_sync(request(rt, optin), b, config)['data']
    assert replay['invocation_nonce'] == auth.invocation_nonce == 'nonce-live'
    assert _state(db) == before
    assert not replay.get('async_eligible')
    assert 'settlement_ticket' not in replay
    assert len(calls) == int(enabled and optin)
