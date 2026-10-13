"""Same-request dormant differentials reusing the existing HTTP byte/SQL oracle."""
from __future__ import annotations

import ast
import copy
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests import test_async_settle_shadow_http as oracle
from tests.test_async_settle_handler import env  # noqa: F401
from tests.test_async_settle_shadow_cohort import exhaustive, untraced  # noqa: F401
from tests.test_gateway_authorize_spanner_operations import fixed_operation_catalog  # noqa: F401


def reuse_frozen_compilation(monkeypatch):
    """Compile the unchanged oracle once; rebuild its globals for every request."""
    path = Path(oracle.__file__).parent/'fakes/shadow_main_routes_internal_gateway.py.txt'
    functions = [n for n in ast.parse(path.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    code = compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec')
    namespace = {}
    def frozen_routes():
        # Existing compiled endpoints retain this globals dictionary. Refresh
        # it for each oracle leg, including its frozen per-request caches.
        namespace.clear()
        namespace.update(vars(oracle.gateway))
        exec(code, namespace)  # noqa: S102 - unchanged pinned oracle, separate globals
        return namespace
    monkeypatch.setattr(oracle, 'frozen_routes', frozen_routes)


def reuse_registered_apps(monkeypatch):
    """Reuse route compilation, resetting settings and app state for every leg.

    The frozen and live handlers have separate real FastAPI apps. Each request
    still uses a fresh HTTP client, backend state and the original byte/SQL oracle.
    Lifecycle installation is exercised independently by the live HTTP tests.
    """
    factory = oracle._client
    apps = {}
    def client(cfg):
        old = oracle.gateway.register.__code__.co_filename.endswith('.txt')
        if old not in apps:
            result = factory(cfg)
            apps[old] = (result.app, cfg, dict(result.app.state._state))
            return result
        app, settings, state = apps[old]
        values, private = copy.deepcopy(cfg.__dict__), copy.deepcopy(cfg.__pydantic_private__)
        settings.__dict__.clear()
        settings.__dict__.update(values)
        settings.__pydantic_private__ = private
        settings.__pydantic_fields_set__ = set(cfg.__pydantic_fields_set__)
        settings.__pydantic_extra__ = copy.deepcopy(cfg.__pydantic_extra__)
        app.state._state.clear()
        app.state._state.update(state)
        return TestClient(app)
    monkeypatch.setattr(oracle, '_client', client)


def forbid_cohort_calls(monkeypatch):
    factory = oracle._client
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(1)
        raise AssertionError('dormant request reached v2 observation')
    def client(*args, **kwargs):
        result = factory(*args, **kwargs)
        result.app.state.async_settle_shadow_cohort = SimpleNamespace(authorize_http=unexpected, terminal=unexpected)
        return result
    monkeypatch.setattr(oracle, '_client', client)
    return calls


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_authorize_shadow_exception_preserves_http_bytes_and_money(monkeypatch):
    oracle.pin_clocks(monkeypatch)
    store, db, key = oracle._seed_typed_gateway_store()
    cfg = oracle.settings(async_settle_shadow_policy='', async_settle_shadow_workspaces='ws-rpc',
                          async_settle_enabled=False)
    client = oracle._client(cfg)
    client._transport.raise_server_exceptions = False
    legacy = getattr(client.app.state, 'async_settle_shadow', None)
    client.app.state.async_settle_shadow = None
    calls = []
    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('injected observation failure')
    client.app.state.async_settle_shadow_cohort = SimpleNamespace(authorize_http=broken)
    payload = oracle._body(key.hash).model_dump(mode='json')
    saved = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
    outputs = []
    try:
        for pin in ('', 'shadow-v2'):
            monkeypatch.setattr(oracle.acquisition, '_usage_check_after', oracle.OrderedDict())
            monkeypatch.setattr(oracle.storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            monkeypatch.setattr(oracle.gateway, '_BROADCAST_EMPTY_CACHE', oracle.OrderedDict())
            for key, value in saved.items():
                setattr(db, key, copy.deepcopy(value))
            store._credit_shard_counts.invalidate('ws-rpc')
            cfg.async_settle_shadow_policy = pin
            responses = []
            for _ in range(2):
                response = client.post('/v1/internal/gateway/authorize', json=payload,
                    headers={'X-TR-Settlement-Shadow-Policy': 'shadow-v2'})
                assert response.status_code == 200, response.text
                responses.append((response.status_code, list(response.headers.multi_items()), response.content))
            outputs.append((responses, {k: copy.deepcopy(getattr(db, k)) for k in saved}))
        assert outputs[0] == outputs[1]
        assert calls == [1, 1]
    finally:
        client.close()
        if legacy is not None:
            legacy.executor.shutdown()


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_registered_app_reuse_matches_fresh_app_after_settings_change(monkeypatch):
    factory = oracle._client
    reuse_frozen_compilation(monkeypatch)
    reuse_registered_apps(monkeypatch)
    store, db, key = oracle._seed_typed_gateway_store()
    body = oracle._body(key.hash).model_dump(mode='json')
    saved = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
    def restore():
        for k, value in saved.items():
            setattr(db, k, copy.deepcopy(value))
    cold = oracle.settings(async_settle_shadow_policy='shadow-v2', async_settle_enabled=False)
    with monkeypatch.context() as patch:
        oracle.transcripts(patch, store, db, cold, '/v1/internal/gateway/authorize', body, {})
    cfg = oracle.settings(async_settle_shadow_policy='shadow-v2', async_settle_enabled=True,
                          async_settle_shadow_workspaces='nonmember')
    restore()
    with monkeypatch.context() as patch:
        reused = oracle.transcripts(patch, store, db, cfg, '/v1/internal/gateway/authorize', body, {}, True)
    restore()
    with monkeypatch.context() as patch:
        patch.setattr(oracle, '_client', factory)
        fresh = oracle.transcripts(patch, store, db, cfg, '/v1/internal/gateway/authorize', body, {}, True)
    assert reused == fresh


@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('shard', range(4))
@exhaustive
def test_existing_authorize_replay_matrix_v2_nonmember(monkeypatch, shard):
    reuse_frozen_compilation(monkeypatch)
    reuse_registered_apps(monkeypatch)
    calls = forbid_cohort_calls(monkeypatch)
    original = oracle.transcripts
    def transcripts(patch, store, db, cfg, *args):
        cfg.async_settle_shadow_policy = 'shadow-v2'
        return original(patch, store, db, cfg, *args)
    monkeypatch.setattr(oracle, 'transcripts', transcripts)
    # Every input tuple of the established authorize differential, including
    # both money-flag values and both signer states, goes through both handlers.
    cases = list(product(['none', 'exact', 'duplicate', 'bad'], [True, False], [False, True], ['', 'nonmember'], [False, True]))
    for header, eligible, admission, optin, present in cases[shard::4]:
        with monkeypatch.context() as patch:
            oracle.test_flag_off_authorize_http_identity(patch, header, eligible, admission, optin, present)
    assert calls == []


@pytest.mark.parametrize('shard', range(4))
@exhaustive
def test_existing_settle_refund_replay_matrix_v2_nonmember(monkeypatch, shard):
    reuse_frozen_compilation(monkeypatch)
    reuse_registered_apps(monkeypatch)
    calls = forbid_cohort_calls(monkeypatch)
    original = oracle.transcripts
    def transcripts(patch, store, db, cfg, *args):
        cfg.async_settle_shadow_policy = 'shadow-v2'
        return original(patch, store, db, cfg, *args)
    monkeypatch.setattr(oracle, 'transcripts', transcripts)
    cases = list(product(['settle', 'refund'], ['none', 'valid', 'invalid', 'oversize', 'duplicate'],
            [False, True], ['', 'nonmember'], [False, True]))
    for kind, header, admission, optin, present in cases[shard::4]:
        with monkeypatch.context() as patch:
            setup = env.__wrapped__(patch)
            fixture = next(setup)
            try:
                oracle.test_flag_off_terminal_http_identity(fixture, patch, kind, header, admission, optin, present)
            finally:
                next(setup, None)
    assert calls == []
