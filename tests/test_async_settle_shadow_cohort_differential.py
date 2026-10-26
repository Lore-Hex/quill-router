"""PROCESS scope: import, application construction and lifespan do not import or
install this package when the pin is empty, money async is on, or the cohort
workspace list is empty. Exact pin, money off and a nonempty list is an active
process: it imports and installs the package process-wide.

REQUEST scope: an outside-cohort request in an active process enters no package
function, imports nothing, makes the base v1 wrapper calls and preserves bytes,
rows and SQL. Every request in a dormant process has the same guarantee.
Fresh-process recording covers all three dormant configurations and an active
positive control. In-process recording covers fresh/replay, absent markers,
settle/refund and chat/Responses streaming branches, with literal v1 counts.
The unchanged historical money oracle remains a separate comparison.
"""
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

_COMPILED = {}
_APPS = {}
_FRESH_CLIENT = oracle._client


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


def registered_client(cfg):
    """One registration per exact settings/private-state/handler configuration.

    Settings captured by route dependencies are restored before each lease;
    intentional in-test cfg changes remain visible to those dependencies.
    Each lease gets fresh HTTP transport, ingress limits and application state.
    Backend/SQL/request caches are still reset by the unchanged oracle.
    """
    values = copy.deepcopy(cfg.__dict__)
    private = copy.deepcopy(cfg.__pydantic_private__)
    extra = copy.deepcopy(cfg.__pydantic_extra__)
    fields = set(cfg.__pydantic_fields_set__)
    key = (oracle.gateway.register.__code__, cfg.model_dump_json(),
           tuple(sorted((k, repr(v)) for k, v in private.items())),
           tuple(sorted(fields)), repr(extra))
    if key not in _APPS:
        result = _FRESH_CLIENT(cfg)
        _APPS[key] = (result.app, cfg, dict(result.app.state._state))
        return result
    app, settings, state = _APPS[key]
    settings.__dict__ = values
    settings.__pydantic_private__ = private
    settings.__pydantic_fields_set__ = fields
    settings.__pydantic_extra__ = extra
    cfg.__dict__ = settings.__dict__
    cfg.__pydantic_private__ = settings.__pydantic_private__
    app.state._state.clear()
    app.state._state.update(state)
    reset_app_ingress(app)
    return TestClient(app)


@pytest.fixture(scope="session")
def registered_apps(request):
    """Worker-local construction cache, shared by both HTTP modules."""
    if not getattr(request.session, '_p4a_registered_apps', False):
        request.session._p4a_registered_apps = True
        request.addfinalizer(close_registered_apps)
    return registered_client


def close_registered_apps():
    for _app, _, state in _APPS.values():
        legacy = state.get('async_settle_shadow')
        if legacy is not None:
            legacy.executor.shutdown()
        shell = state.get('async_settle_shadow_cohort')
        if shell is not None:
            shell.close()
            shell.worker.join(2)
    _APPS.clear()


@pytest.fixture(autouse=True)
def shared_differential_apps(monkeypatch, registered_apps):
    monkeypatch.setattr(oracle, '_client', registered_apps)


def reuse_registered_apps(monkeypatch):
    monkeypatch.setattr(oracle, '_client', registered_client)


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
    authorize_hook_fault_http(monkeypatch)


def authorize_hook_fault_http(monkeypatch, fault=None):
    import sys

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
    reached = [set(), set()]
    injected = []
    operation_calls = []
    operation = oracle.gateway._authorize_gateway_sync_impl
    def counted(*args, **kwargs):
        operation_calls.append(1)
        return operation(*args, **kwargs)
    monkeypatch.setattr(oracle.gateway, '_authorize_gateway_sync_impl', counted)
    hook = oracle.gateway._v2_authorize_hook
    leg = 0
    def traced(*args, **kwargs):
        if not args[0].async_settle_shadow_policy:
            return hook(*args, **kwargs)
        if fault[0] == 'entry':
            injected.append(leg)
            raise RuntimeError('authorize hook entry fault')
        fired = False
        def trace(frame, event, arg):
            nonlocal fired
            site = (frame.f_code.co_filename, frame.f_lineno)
            if event == 'line' and site in fault[1]:
                reached[leg].add(site)
                if fault[0] == 'inject' and not fired:
                    fired = True
                    injected.append(leg)
                    raise RuntimeError('authorize hook statement fault')
            return trace
        previous = sys.gettrace()
        sys.settrace(trace)
        try:
            return hook(*args, **kwargs)
        finally:
            sys.settrace(previous)
    if fault is not None:
        monkeypatch.setattr(oracle.gateway, '_v2_authorize_hook', traced)
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
            for leg in range(2):  # noqa: B007 - read by the request-thread trace closure
                before = len(operation_calls)
                response = client.post('/v1/internal/gateway/authorize', json=payload,
                    headers={'X-TR-Settlement-Shadow-Policy': 'shadow-v2'})
                assert len(operation_calls) == before+1
                assert response.status_code == 200, response.text
                responses.append((response.status_code, list(response.headers.multi_items()), response.content,
                    {k: copy.deepcopy(getattr(db, k)) for k in saved}))
            outputs.append((responses, {k: copy.deepcopy(getattr(db, k)) for k in saved}))
        assert outputs[0] == outputs[1]
        if fault is None or fault[0] == 'record':
            assert calls == [1, 1]
        else:
            assert injected == [0, 1]
        return reached
    finally:
        client.close()
        if legacy is not None:
            legacy.executor.shutdown()


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_registered_app_reuse_matches_fresh_app_after_settings_change(monkeypatch):
    factory = _FRESH_CLIENT
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
    oracle.pin_clocks(patch)
    client = oracle._client(cfg)
    client.app.state.async_settle_shadow = None
    responses = []
    try:
        for _ in range(2):
            prior = len(wrapper_calls)
            response = recorded_post(client, path, payload, headers)
            expected = {'authorize': [], 'settle': ['settle'], 'refund': ['refund']}[path.rsplit('/', 1)[1]]
            assert wrapper_calls[prior:] == expected
            assert response.status_code == 200, response.text
            responses.append((response.status_code, list(response.headers.multi_items()), response.content))
    finally:
        client.close()
    return (responses, [], {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))})


@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('dormant', ['empty', 'nonmember', 'money'])
def test_current_handlers_zero_entries_imports_and_literal_v1_calls(monkeypatch, dormant):
    from trusted_router.services import async_settle_shadow as v1
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
@pytest.mark.parametrize('fault', ['import', 'entry', 'inside', 'outer'])
def test_terminal_hook_faults_real_http_equal_pin_off(env, monkeypatch, kind, fault):
    terminal_hook_fault_http(env, monkeypatch, kind, fault)


def terminal_hook_fault_http(env, monkeypatch, kind, fault):
    import builtins
    import sys

    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    reuse_registered_apps(monkeypatch)
    oracle.pin_clocks(monkeypatch)
    faults = []
    statement_mode = isinstance(fault, tuple)
    capture_mode = statement_mode and len(fault) > 2
    reached_legs = [set(), set()]
    if statement_mode:
        from tests.test_async_settle_shadow import NOW
        monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    observed_lines = set()
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
            # A cold v2 app performs its control read before request recording.
            # Verify that setup changed only the exact marker-read counters,
            # then start both money/HTTP transcripts from the same baseline.
            reads = {'snapshot_calls', 'snapshot_execute_sql_calls', 'snapshot_sql', 'snapshot_sql_params'}
            extra = db.snapshot_execute_sql_calls-saved['snapshot_execute_sql_calls']
            assert extra in ((0, 1) if pin else (0,))
            assert db.snapshot_sql == saved['snapshot_sql'] + [
                'SELECT body FROM tr_entities WHERE kind=@kind AND id=@id']*extra
            assert db.snapshot_sql_params == saved['snapshot_sql_params'] + [
                {'kind': 'async_settle_shadow_control_v2', 'id': 'stop-v2'}]*extra
            assert db.snapshot_calls == saved['snapshot_calls'] + [{}]*extra
            for key, value in saved.items():
                if key in reads:
                    setattr(db, key, copy.deepcopy(value))
                else:
                    assert getattr(db, key) == value, key
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
                    if fault == 'outer':
                        local.setattr(oracle.gateway, '_v2_'+kind+'_hook', broken)
                    elif fault == 'entry':
                        local.setattr(module, 'terminal_http', broken)
                    elif fault == 'import':
                        original = builtins.__import__
                        def importer(name, globals=None, locals=None, fromlist=(), level=0, original=original):
                            if name == module.__name__ and 'terminal_http' in fromlist:
                                faults.append(fault)
                                raise ImportError('injected package import failure')
                            return original(name, globals, locals, fromlist, level)
                        local.setattr(builtins, '__import__', importer)
                money_calls = []
                boundary = oracle.gateway._terminal_once
                def counted(hook, callback, money_calls=money_calls, boundary=boundary):
                    def money():
                        money_calls.append(1)
                        return callback()
                    return boundary(hook, money if callback.__name__ == '<lambda>' else callback)
                if pin:
                    local.setattr(oracle.gateway, '_terminal_once', counted)
                shell = None
                if pin and statement_mode:
                    if capture_mode and len(fault) > 4 and fault[4]:
                        def capture_failure(*args, **kwargs):
                            raise RuntimeError('capture helper failure')
                        local.setattr(module, 'capture_prices', capture_failure)
                    from tests.test_async_settle_shadow_cohort_runtime import installed
                    shell, _, _ = installed()
                    client.app.state.async_settle_shadow_cohort = shell
                    hook_name = '_v2_capture_hook' if capture_mode else '_v2_'+kind+'_hook'
                    original_hook = getattr(oracle.gateway, hook_name)
                    def traced(*args, original_hook=original_hook, **kwargs):
                        if fault[0] == 'entry':
                            faults.append('entry')
                            raise RuntimeError('capture hook entry fault')
                        injected = []
                        def trace(frame, event, arg):
                            site = (frame.f_code.co_filename, frame.f_lineno)
                            if event == 'line' and site in fault[1]:
                                observed_lines.add(site)
                                reached_legs[leg].add(site)  # noqa: B023 - synchronous HTTP leg owns the trace
                                if fault[0] == 'inject' and not injected:
                                    injected.append(site)
                                    faults.append(site)
                                    raise RuntimeError('hook statement fault')
                            return trace
                        previous = sys.gettrace()
                        sys.settrace(trace)
                        try:
                            return original_hook(*args, **kwargs)
                        finally:
                            sys.settrace(previous)
                    local.setattr(oracle.gateway, hook_name, traced)
                responses = []
                try:
                    for leg in range(2):  # noqa: B007 - read by the request-thread trace closure
                        before_money = len(money_calls)
                        if pin and statement_mode and before_money:
                            # A statement fault can strand observer-only locks.
                            # Give replay its own observer so the same statement
                            # is injected on both HTTP legs; money state persists.
                            shell.close()
                            shell.join()
                            shell, _, _ = installed()
                            client.app.state.async_settle_shadow_cohort = shell
                        response = client.post(path, json=payload, headers=headers)
                        if pin:
                            assert len(money_calls) == before_money+1
                        assert response.status_code == 200, response.text
                        responses.append((response.status_code, list(response.headers.multi_items()), response.content,
                            {k: copy.deepcopy(getattr(db, k)) for k in saved}))
                    outputs.append((responses, {k: copy.deepcopy(getattr(db, k)) for k in saved}))
                finally:
                    if shell is not None:
                        shell.close()
                        shell.join()
                    client.close()
        assert outputs[0][0] == outputs[1][0]
        for name in saved:
            assert outputs[0][1][name] == outputs[1][1][name], name
        if not statement_mode:
            assert faults == [fault, fault]
        elif fault[0] == 'entry':
            assert faults == ['entry', 'entry']
        elif fault[0] == 'inject':
            assert faults == [next(iter(fault[1]))]*(fault[3] if capture_mode else 2)
        return (outputs[1][0], [], outputs[1][1])
    monkeypatch.setattr(oracle, 'transcripts', transcripts)
    oracle.test_flag_off_terminal_http_identity(env, monkeypatch, kind, 'none', False, 'nonmember', False)
    return reached_legs if capture_mode else observed_lines


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


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_every_reached_hook_statement_preserves_real_http(env, monkeypatch, kind, record_property):
    import inspect
    import textwrap

    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    functions = [getattr(oracle.gateway, '_v2_'+kind+'_hook'), module.terminal_http,
        module.Runtime.terminal, module.Runtime.capture_terminal, module.Runtime.admit,
        module.Runtime.dispatch, module.Runtime.release]
    sites = set()
    for function in functions:
        source, start = inspect.getsourcelines(function)
        for node in ast.walk(ast.parse(textwrap.dedent(''.join(source)))):
            if isinstance(node, ast.stmt) and node.lineno != 1:
                sites.add((inspect.getsourcefile(function), start+node.lineno-1))
    with monkeypatch.context() as patch:
        reached = terminal_hook_fault_http(env, patch, kind, ('record', sites))
    assert reached
    capture_source, capture_start = inspect.getsourcelines(module.Runtime.capture_terminal)
    compact_line = next(capture_start+i for i, line in enumerate(capture_source) if 'compact = compact_terminal_facts' in line)
    assert (inspect.getsourcefile(module.Runtime.capture_terminal), compact_line) in reached
    record_property('injected_statement_sites', sorted(reached))
    record_property('http_fault_attempts', len(reached)*2)
    hook = functions[0]
    assert (inspect.getsourcefile(hook), hook.__code__.co_firstlineno+2) in reached
    for site in sorted(reached):
        with monkeypatch.context() as patch:
            setup = globals()['env'].__wrapped__(patch)
            fresh = next(setup)
            try:
                terminal_hook_fault_http(fresh, patch, kind, ('inject', {site}))
            finally:
                next(setup, None)


def test_fresh_process_import_construction_and_lifespan():
    import json
    import subprocess
    import sys

    path = Path(__file__).parent/'fixtures/async_settlement/p4a_lifecycle_recorder.py'
    result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=180)  # noqa: S603 - fixed local witness and enum arguments
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert set(evidence) == {'empty', 'no_members', 'money', 'active'}
    for mode in ('empty', 'no_members', 'money'):
        assert evidence[mode] == dict(imports=[], entries=[], installed=False)
    assert evidence['active']['imports'] and evidence['active']['entries']
    assert evidence['active']['installed'] is True


def test_registered_app_factory_exact_configuration_and_reset(registered_apps):
    before = len(_APPS)
    cfg = oracle.settings(source_revision='b'*40)
    first = registered_apps(cfg)
    first.app.state.test_only_dirty = True
    second = registered_apps(oracle.settings(source_revision='b'*40))
    assert second.app is first.app
    assert not hasattr(second.app.state, 'test_only_dirty')
    # Both public configuration and the resolved private cohort affect install.
    changed = registered_apps(oracle.settings(source_revision='b'*40, async_settle_enabled=False))
    private = oracle.settings(source_revision='b'*40)
    private._async_settle_shadow_workspace_ids = frozenset({'different'})
    third = registered_apps(private)
    assert len({id(first.app), id(changed.app), id(third.app)}) == 3
    assert len(_APPS) == before + 3
    for client in (first, second, changed, third):
        client.close()


def hook_statement_sites(hook):
    import inspect
    import textwrap

    source, start = inspect.getsourcelines(hook)
    return {(inspect.getsourcefile(hook), start+node.lineno-1)
        for node in ast.walk(ast.parse(textwrap.dedent(''.join(source))))
        if isinstance(node, ast.stmt) and node.lineno != 1}


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_authorize_hook_entry_and_every_reached_statement_preserve_http(monkeypatch, record_property):
    hook = oracle.gateway._v2_authorize_hook
    with monkeypatch.context() as patch:
        reached = authorize_hook_fault_http(patch, ('record', hook_statement_sites(hook)))
    assert reached[0] == reached[1] and reached[0]
    assert (hook.__code__.co_filename, hook.__code__.co_firstlineno+2) in reached[0]
    record_property('injected_statement_sites', sorted(reached[0]))
    record_property('http_fault_attempts', 2+2*len(reached[0]))
    for fault in [('entry', set()), *[('inject', {site}) for site in sorted(reached[0])]]:
        with monkeypatch.context() as patch:
            authorize_hook_fault_http(patch, fault)


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_capture_hook_entry_and_every_reached_statement_preserve_http(monkeypatch, kind, record_property):
    hook = oracle.gateway._v2_capture_hook
    def run(fault):
        with monkeypatch.context() as patch:
            setup = globals()['env'].__wrapped__(patch)
            fresh = next(setup)
            try:
                return terminal_hook_fault_http(fresh, patch, kind, fault)
            finally:
                next(setup, None)
    for helper_fails in (False, True):
        reached = run(('record', hook_statement_sites(hook), 'capture', 0, helper_fails))
        sites = reached[0] | reached[1]
        assert sites and reached[0] != reached[1]  # replay-only price acquisition is exercised
        assert (hook.__code__.co_filename, hook.__code__.co_firstlineno+2) in sites
        record_property('injected_statement_sites_'+str(helper_fails), sorted(sites))
        record_property('http_fault_attempts_'+str(helper_fails), 2+sum(map(len, reached)))
        faults = [('entry', set(), 'capture', 2, helper_fails)]
        faults += [('inject', {site}, 'capture', sum(site in leg for leg in reached), helper_fails) for site in sorted(sites)]
        for fault in faults:
            run(fault)
