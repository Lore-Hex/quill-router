"""Dormancy means no entry into this package, no import of its modules, the same
v1 wrapper calls, and the same bytes, rows and SQL as the historical oracle,
for pin empty, exact pin/nonmember, and money on across the represented fresh,
replay, marker-absent, settle/refund and chat/Responses stream handler branches.
The independent P3 handler fixture supplies the v1 call-order baseline; the
unchanged 576-request historical money oracle remains separate. This does not
claim every possible HTTP error branch, imported dependency immutability,
network enclave execution, timing equality, or production certification.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests import test_async_settle_shadow_http as oracle
from tests.test_async_settle_handler import env  # noqa: F401
from tests.test_async_settle_shadow_cohort import exhaustive, untraced  # noqa: F401
from tests.test_gateway_authorize_spanner_operations import fixed_operation_catalog  # noqa: F401

_COMPILED = {}
_APPS = {}


def reuse_frozen_compilation(monkeypatch, path=None):
    """Compile the unchanged oracle once; rebuild its globals for every request."""
    path = path or Path(oracle.__file__).parent/'fakes/shadow_main_routes_internal_gateway.py.txt'
    if path not in _COMPILED:
        functions = [n for n in ast.parse(path.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        _COMPILED[path] = (compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), {})
    code, namespace = _COMPILED[path]
    def frozen_routes():
        # Existing compiled endpoints retain this globals dictionary. Refresh
        # it for each oracle leg, including its frozen per-request caches.
        namespace.clear()
        namespace.update(vars(oracle.gateway))
        exec(code, namespace)  # noqa: S102 - unchanged pinned oracle, separate globals
        return namespace
    monkeypatch.setattr(oracle, 'frozen_routes', frozen_routes)


def reset_app_ingress(app):
    """Restore the real limiter to fresh-app state without disabling its checks."""
    import inspect

    from trusted_router.storage_rate_limits import InMemoryRateLimits
    found = 0
    for middleware in app.user_middleware:
        dispatch = middleware.kwargs.get('dispatch')
        if inspect.isfunction(dispatch):
            limiter = inspect.getclosurevars(dispatch).nonlocals.get('ingress_rate_limits')
            if isinstance(limiter, InMemoryRateLimits):
                limiter.reset()
                found += 1
    assert found == 1


def reuse_registered_apps(monkeypatch):
    """Reuse route compilation, resetting settings and app state for every leg.

    The frozen and live handlers have separate real FastAPI apps. Each request
    still uses a fresh HTTP client, backend state and the original byte/SQL oracle.
    Lifecycle installation is exercised independently by the live HTTP tests.
    """
    factory = oracle._client
    apps = _APPS
    def client(cfg):
        old = oracle.gateway.register.__code__.co_filename
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
        reset_app_ingress(app)
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


P3_BASE = Path(__file__).parent/'fixtures/async_settlement/p4a_p3_gateway.py.txt'


def test_actual_p3_base_is_independently_pinned():
    assert hashlib.sha256(P3_BASE.read_bytes()).hexdigest() == 'ef6eaf5e797b92af06bfa5769415e01883bd2c84cad86987ba2316dc2bfec1c4'


def recorded_post(client, path, payload, headers):
    """Record all owned public functions/methods, including worker-thread calls."""
    import builtins
    import sys
    import threading
    entered, imports = [], []
    original_import = builtins.__import__
    before = {name: value for name, value in sys.modules.items() if 'shadow_cohort' in name and name.startswith('trusted_router.')}
    def importer(name, globals=None, locals=None, fromlist=(), level=0):
        if 'shadow_cohort' in name or any('shadow_cohort' in item for item in (fromlist or ())):
            imports.append((name, fromlist))
        return original_import(name, globals, locals, fromlist, level)
    def recorder(frame, event, arg):
        module = frame.f_globals.get('__name__', '')
        if event == 'call' and module.startswith('trusted_router.') and 'shadow_cohort' in module:
            entered.append((module, frame.f_code.co_qualname))
    profile, thread_profile = sys.getprofile(), threading.getprofile()
    try:
        builtins.__import__ = importer
        threading.setprofile(recorder)
        sys.setprofile(recorder)
        response = client.post(path, json=payload, headers=headers)
    finally:
        sys.setprofile(profile)
        threading.setprofile(thread_profile)
        builtins.__import__ = original_import
    after = {name: value for name, value in sys.modules.items() if 'shadow_cohort' in name and name.startswith('trusted_router.')}
    assert entered == [] and imports == [] and before == after
    return response


def dormant_transcripts(patch, store, db, cfg, path, payload, headers, wrapper_calls):
    from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
    oracle.pin_clocks(patch)
    frozen = oracle.frozen_routes()
    saved = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
    outputs = []
    for old in (True, False):
        for key, value in saved.items():
            setattr(db, key, copy.deepcopy(value))
        store._credit_shard_counts.invalidate('ws-rpc' if 'authorize' in path else 'ws-v1')
        with patch.context() as local:
            local.setattr(oracle.acquisition, '_usage_check_after', oracle.OrderedDict())
            local.setattr(oracle.storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            local.setattr(oracle.gateway, '_BROADCAST_EMPTY_CACHE', oracle.OrderedDict())
            frozen['_BROADCAST_EMPTY_CACHE'] = oracle.OrderedDict()
            if old:
                local.setattr(oracle.gateway, 'register', frozen['register'])
            client = oracle._client(cfg)
            # Lifecycle is outside the measured request interval. Both installed
            # observer types remain absent so calls cannot be hidden in a fake.
            client.app.state.async_settle_shadow = None
            calls = []
            def record(cls, method, calls=calls):
                original = getattr(cls, method)
                def wrapped(self, *args, **kwargs):
                    calls.append((cls.__name__, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
                    return original(self, *args, **kwargs)
                local.setattr(cls, method, wrapped)
            for method in ('execute_update', 'execute_sql', 'batch_update'):
                record(_FakeTransaction, method)
            record(_FakeSnapshot, 'execute_sql')
            responses = []
            try:
                for _ in range(2):
                    prior_calls = len(wrapper_calls)
                    response = recorded_post(client, path, payload, headers)
                    expected = {'authorize': [], 'settle': ['settle'], 'refund': ['refund']}[path.rsplit('/', 1)[1]]
                    assert wrapper_calls[prior_calls:] == expected
                    assert response.status_code == 200, response.text
                    responses.append((response.status_code, list(response.headers.multi_items()), response.content))
                outputs.append((responses, calls, {k: copy.deepcopy(getattr(db, k)) for k in saved}))
            finally:
                client.close()
    assert outputs[0] == outputs[1]
    kind = path.rsplit('/', 1)[1]
    # Literals from P3: observe_entry once on fresh, once on replay, per leg.
    assert wrapper_calls == ([] if kind == 'authorize' else ['settle', 'settle', 'settle', 'settle']
                             if kind == 'settle' else ['refund', 'refund', 'refund', 'refund'])
    return outputs[1]


@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('dormant', ['empty', 'nonmember', 'money'])
def test_actual_base_zero_entries_imports_and_same_v1_calls(monkeypatch, dormant):
    from trusted_router.services import async_settle_shadow as v1
    reuse_frozen_compilation(monkeypatch, P3_BASE)
    reuse_registered_apps(monkeypatch)
    original_transcripts, observe = oracle.transcripts, v1.observe_entry
    calls = []
    async def observed(*args, **kwargs):
        calls.append(kwargs['kind'])
        return await observe(*args, **kwargs)
    monkeypatch.setattr(v1, 'observe_entry', observed)
    def transcripts(patch, store, db, cfg, path, payload, headers, signer_present=False):
        cfg.async_settle_shadow_policy = '' if dormant == 'empty' else 'shadow-v2'
        cfg.async_settle_enabled = dormant == 'money'
        cfg._async_settle_shadow_workspace_ids = frozenset({'nonmember'} if dormant == 'nonmember' else {'ws-rpc', 'ws-v1'})
        calls.clear()
        return dormant_transcripts(patch, store, db, cfg, path, payload, headers, calls)
    monkeypatch.setattr(oracle, 'transcripts', transcripts)
    for route in ('chat.completions', 'responses'):
        for marker in (False, True):
            with monkeypatch.context() as patch:
                store, db, key = oracle._seed_typed_gateway_store()
                payload = oracle._body(key.hash).model_dump()
                payload.update(route_type=route, stream=True, requested_parameters=[], invocation_nonce='dormant-stream')
                headers = {'X-TR-Settlement-Shadow-Policy': 'shadow-v2'} if marker else {}
                cfg = oracle.settings()
                transcripts(patch, store, db, cfg, '/v1/internal/gateway/authorize', payload, headers)
        for kind in ('settle', 'refund'):
            with monkeypatch.context() as patch:
                setup = env.__wrapped__(patch)
                fixture = next(setup)
                try:
                    # Reuse unchanged preparation/SQL machinery, explicitly carry
                    # both streaming route shapes through fresh and replay handlers.
                    def stream_transcripts(patch, store, db, cfg, path, payload, headers, signer_present=False, route=route):
                        payload.update(route_type=route, streamed=True)
                        return transcripts(patch, store, db, cfg, path, payload, headers)
                    patch.setattr(oracle, 'transcripts', stream_transcripts)
                    oracle.test_flag_off_terminal_http_identity(fixture, patch, kind, 'none', False, 'nonmember', False)
                finally:
                    next(setup, None)
    monkeypatch.setattr(oracle, 'transcripts', original_transcripts)


@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('fault', ['import', 'entry', 'inside'])
def test_terminal_hook_faults_real_http_equal_pin_off(env, monkeypatch, kind, fault):
    import builtins

    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    reuse_registered_apps(monkeypatch)
    oracle.pin_clocks(monkeypatch)
    faults = []
    def transcripts(patch, store, db, cfg, path, payload, headers, signer_present=False):
        cfg.async_settle_enabled = False
        cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
        saved = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
        outputs = []
        for pin in ('', 'shadow-v2'):
            cfg.async_settle_shadow_policy = pin
            patch.setattr(oracle.acquisition, '_usage_check_after', oracle.OrderedDict())
            patch.setattr(oracle.storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            patch.setattr(oracle.gateway, '_BROADCAST_EMPTY_CACHE', oracle.OrderedDict())
            for key, value in saved.items():
                setattr(db, key, copy.deepcopy(value))
            store._credit_shard_counts.invalidate('ws-v1')
            client = oracle._client(cfg)
            client._transport.raise_server_exceptions = False
            client.app.state.async_settle_shadow = None
            def broken(*args, **kwargs):
                faults.append(fault)
                if fault == 'inside':
                    # Deliberately faulty hook tries two calls then throws.
                    args[-1]()
                    args[-1]()
                raise RuntimeError('injected terminal observation failure')
            client.app.state.async_settle_shadow_cohort = SimpleNamespace(terminal=broken, port=SimpleNamespace(lost=SimpleNamespace(set=lambda: None)))
            with patch.context() as local:
                if pin:
                    if fault == 'entry':
                        local.setattr(module, 'terminal_http', broken)
                    elif fault == 'import':
                        original = builtins.__import__
                        def importer(name, globals=None, locals=None, fromlist=(), level=0, original=original):
                            if name == module.__name__ and 'terminal_http' in fromlist:
                                faults.append(fault)
                                raise ImportError('injected package import failure')
                            return original(name, globals, locals, fromlist, level)
                        local.setattr(builtins, '__import__', importer)
                responses = []
                try:
                    for _ in range(2):
                        response = client.post(path, json=payload, headers=headers)
                        assert response.status_code == 200, response.text
                        responses.append((response.status_code, list(response.headers.multi_items()), response.content))
                    outputs.append((responses, {k: copy.deepcopy(getattr(db, k)) for k in saved}))
                finally:
                    client.close()
        assert outputs[0][0] == outputs[1][0]
        for name in saved:
            assert outputs[0][1][name] == outputs[1][1][name], name
        assert faults == [fault, fault]
        return (outputs[1][0], [], outputs[1][1])
    monkeypatch.setattr(oracle, 'transcripts', transcripts)
    oracle.test_flag_off_terminal_http_identity(env, monkeypatch, kind, 'none', False, 'nonmember', False)


def test_terminal_boundary_keeps_original_callback_error_and_once_only():
    error = ValueError('money failure')
    calls = []
    def money():
        calls.append(1)
        raise error
    def broken(callback):
        try:
            callback()
        except ValueError:
            pass
        raise RuntimeError('later hook error')
    with pytest.raises(ValueError) as caught:
        oracle.gateway._terminal_once(broken, money)
    assert caught.value is error and calls == [1]
