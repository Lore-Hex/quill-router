# ruff: noqa: F811 - imported pytest fixture
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_async_settle_admission import request
from tests.test_async_settle_ticket import runtime, settings
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401
)
from tests.test_spanner_batch_dml import _state
from trusted_router.detached_jws import canonical
from trusted_router.routes.internal import gateway


@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('workspaces', ['', 'nonmember'])
@pytest.mark.parametrize('headers', [[], [b'async-v1'], [b'async-v1', b'async-v1'], [b'wrong']])
@pytest.mark.parametrize('has_signer', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('replay', [False, True])
@pytest.mark.parametrize('eligible', [False, True])
def test_frozen_authorize_response_matrix(monkeypatch, workspaces, headers, has_signer, enabled, replay, eligible):
    _, db, key = _seed_typed_gateway_store()
    cfg = settings(async_settle_enabled=enabled, async_settle_shadow_workspaces=workspaces)
    rt = runtime()
    if not has_signer:
        rt.signer = None
    req = request(rt)
    req.scope['headers'] = [(b'x-tr-settlement-mode', value) for value in headers]
    calls = []
    req.app.state.async_settle_shadow = SimpleNamespace(authorize=lambda *a, **k: calls.append((a, k)))
    original = gateway._gateway_authorize_response
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(gateway, '_gateway_authorize_response', capture)
    monkeypatch.setattr(gateway, '_new_gateway_authorization_id', lambda: 'auth-frozen-shadow')
    import time
    monkeypatch.setattr(time, 'time', lambda: 1791244801)
    body = _body(key.hash)
    body.route_type = 'chat.completions' if eligible else None
    body.invocation_nonce = 'nonce-frozen-shadow'
    gateway._authorize_gateway_sync(req, body, cfg)
    captured['idempotent_replay'] = replay
    path = Path(__file__).parent/'fakes/shadow_main_routes_internal_gateway.py.txt'
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_gateway_authorize_response')
    namespace = dict(vars(gateway))
    # Compile only frozen source, under its real on-disk path for coverage.
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)  # noqa: S102 - frozen test oracle
    before = _state(db)
    frozen = namespace['_gateway_authorize_response'](**captured)
    current = original(**captured)
    assert canonical(current) == canonical(frozen)
    assert _state(db) == before
    assert calls == []


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_opted_in_binding_is_after_hold_without_new_rpc(monkeypatch):
    import copy
    import time

    from trusted_router.async_settle_shadow_binding import verify_binding
    from trusted_router.services.async_settle_shadow import Runtime
    _, db, key = _seed_typed_gateway_store()
    cfg = settings(async_settle_enabled=False, async_settle_shadow_workspaces='ws-rpc')
    rt = runtime()
    shadow = Runtime(cfg, rt)
    req = request(rt)
    req.app.state.async_settle_shadow = shadow
    original, observations = shadow.authorize, []
    def authorize(*args, **kwargs):
        before = copy.deepcopy(_state(db))
        commits = db.commits
        original(*args, **kwargs)
        observations.append((commits, db.commits, before, _state(db)))
    monkeypatch.setattr(shadow, 'authorize', authorize)
    monkeypatch.setattr(gateway, '_new_gateway_authorization_id', lambda: 'auth-opted-shadow')
    body = _body(key.hash)
    body.route_type, body.invocation_nonce = 'chat.completions', 'nonce-opted-shadow'
    data = gateway._authorize_gateway_sync(req, body, cfg)['data']
    assert [(before > 0, before == after, old == new) for before, after, old, new in observations] == [(True, True, True)]
    claims = verify_binding(data['billing_shadow_binding'], [shadow.signer.trusted], int(time.time()))
    assert (claims.authorization_id, claims.workspace_id, claims.snapshot_hash, claims.async_eligible) == ('auth-opted-shadow', 'ws-rpc', data['billing_snapshot_hash'], False)
    assert 'settlement_ticket' not in data and data['billing_snapshot']['candidates']
    shadow.executor.shutdown()
