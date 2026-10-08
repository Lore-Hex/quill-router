"""Frozen-main route → finalize → insert → drain → retention differential.

Frozen files compile under their own paths, never under live coverage paths.
"""
# ruff: noqa: F811, F401, S102 - shared fixtures and pinned executable oracle
from __future__ import annotations

import copy
import datetime as dt
import importlib
import inspect
import json
import os
import re
import sys
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
from types import FunctionType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.fakes.frozen_package import (
    ALIAS,
    FROZEN_BUILTINS,
    PINS,
    execution_guard,
    fake_store,
    module,
)
from tests.fakes.spanner import _FakeBatch, _FakeSnapshot, _FakeTransaction
from tests.test_async_settle_handler import SUPPORTED, env, prepare
from tests.test_async_settle_proof import catalog, legacy_body, restore, save
from tests.test_settle_outbox_drain import _client
from trusted_router import (
    storage_gcp_analytics_outbox,
    storage_gcp_authorize,
    storage_gcp_settle_outbox,
)
from trusted_router.services import settle_outbox_drain


def guard_unavailable():
    """3.11 proves rejection, never reports a silently skipped execution proof."""
    if sys.version_info >= (3, 12):
        return False
    from tests.fakes.frozen_package import _UNSUPPORTED_INTERPRETER
    with pytest.raises(AssertionError) as error:
        with execution_guard():
            pytest.fail('unsupported interpreter entered the frozen leg')
    assert str(error.value) == _UNSUPPORTED_INTERPRETER
    return True


def test_frozen_main_provenance():
    # Importing the loader validates every file and the independently pinned archive.
    assert 'src/trusted_router/routes/internal/gateway.py' in PINS
    assert 'src/trusted_router/partner_billing.py' in PINS


def frozen_environment(patch, cfg, body):
    store, db = fake_store()
    settings = module('config').Settings(**cfg)
    store.trust_settings = settings
    module('storage').configure_store(store)
    # Catalog inputs are fixture-owned in both legs; construct frozen endpoint
    # and model objects so their validators/methods cannot call live producers.
    from tests.test_billing_snapshot import endpoint_from_candidate
    builder = FunctionType(endpoint_from_candidate.__code__, {
        **endpoint_from_candidate.__globals__,
        **{name: getattr(module(value.__module__.removeprefix('trusted_router.')), value.__name__)
           for name, value in endpoint_from_candidate.__globals__.items()
           if name in {'ModelEndpoint', 'PriceTier'}}})
    builder.__kwdefaults__ = endpoint_from_candidate.__kwdefaults__
    endpoints = {c['endpoint_id']: builder(c) for c in body['billing_snapshot']['candidates']}
    gateway = module('routes.internal.gateway')
    for endpoint in endpoints.values():
        patch.setitem(gateway.MODELS, endpoint.model_id, module('catalog_data').Model(
            id=endpoint.model_id, name='proof', provider=endpoint.provider,
            context_length=1_000_000, prepaid_available=True))
    patch.setattr(gateway, 'endpoint_for_id', endpoints.get)
    patch.setattr(module('services.settle_outbox_apply'), 'endpoint_for_id', endpoints.get)
    patch.setattr(module('storage_gcp_authorize'), '_OUTBOX_AVAILABILITY_CACHE', {})
    store.settle_outbox = module('storage_gcp_settle_outbox').SpannerSettleOutbox(
        db, store._param_types, async_fence=settings.async_settle_protection)
    main = module('main')
    module('storage').configure_store(store)
    client = TestClient(main.create_app(
        settings, configure_store_arg=False, init_observability=False))
    return store, db, settings, client


def inventory(seen):
    # Optional per-process output makes xdist's union reproducible without races.
    directory = os.environ.get('FROZEN_MAIN_INVENTORY_DIR')
    if directory:
        path = Path(directory) / f'{os.getpid()}.json'
        previous = {tuple(row) for row in json.loads(path.read_text())} if path.exists() else set()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(previous | seen), indent=2) + '\n')


def frozen_runtime():
    # Run the same fixture factories with snapshot classes and literal key inputs.
    # In particular, never attach the live Runtime (or its admission callback).
    from tests import test_async_settle_ticket as fixture

    # Admission callbacks retain this dictionary. Copying every fixture global
    # also retains unused live router definitions (for example UsageType).
    # Give the unchanged fixture factories only their actual dependencies.
    namespace = {'__name__': fixture.__name__, '__builtins__': FROZEN_BUILTINS,
                 'Ed25519PrivateKey': fixture.Ed25519PrivateKey,
                 'FIXTURE': fixture.FIXTURE, 'PURPOSE': module('async_settle_ticket').PURPOSE}
    for name in ('Admission', 'AdmissionCache', 'DrainHealth', 'Runtime',
                 'TicketSigner', 'TrustedKey'):
        value = getattr(fixture, name)
        namespace[name] = getattr(module(value.__module__.removeprefix('trusted_router.')), name)
    namespace['signer'] = FunctionType(fixture.signer.__code__, namespace)
    return FunctionType(fixture.runtime.__code__, namespace)()


def test_guard_rejects_live_callback_even_after_return(env):
    if guard_unavailable():
        return
    _, auth, _ = prepare(env)
    with pytest.raises(AssertionError, match='trusted_router.storage_models:GatewayAuthorization.record_finalization'):
        with execution_guard():
            auth.record_finalization(success=True, actual_microdollars=2,
                                     selected_usage_type='Credits', generation=None)


@pytest.mark.parametrize('target', ['native', 'partner', 'unlisted', 'generated', 'worker'])
def test_guard_has_no_omitted_function_or_module_exemption(target):
    if guard_unavailable():
        return
    from threading import Thread

    from trusted_router import partner_billing, storage_errors, storage_models
    from trusted_router.routes.internal import gateway

    calls = {
        'native': (lambda: gateway._native_batch_cost_or_error(2, route_type=None, provider='openai'),
                   'trusted_router.routes.internal.gateway:_native_batch_cost_or_error'),
        'partner': (lambda: partner_billing.partner_billing_mode(
            requested_model_id='openai/billing-v1', route_type=None, idempotency_key=None),
                    'trusted_router.partner_billing:partner_billing_mode'),
        'unlisted': (lambda: storage_errors.is_transient_store_error(ValueError()),
                     'trusted_router.storage_errors:is_transient_store_error'),
        'generated': (lambda: storage_models.CreditAccount(workspace_id='test'),
                      'trusted_router.storage_models:CreditAccount.__init__'),
    }
    callback, expected = calls['partner' if target == 'worker' else target]
    with pytest.raises(AssertionError, match=expected):
        with execution_guard():
            if target == 'worker':
                thread = Thread(target=callback)
                thread.start()
                thread.join()
            else:
                callback()


def test_guard_rejects_cached_live_alias(monkeypatch):
    if guard_unavailable():
        return
    from trusted_router.storage_errors import transient_store_error_types

    transient_store_error_types()  # warm the C cache; no Python body on a hit
    with monkeypatch.context() as patch:
        patch.setattr(module('storage_errors'), 'transient_store_error_types', transient_store_error_types)
        with pytest.raises(AssertionError, match='live reference.*trusted_router.storage_errors:transient_store_error_types'):
            with execution_guard():
                module('storage_errors').transient_store_error_types()


@pytest.mark.proof_oracle
@pytest.mark.parametrize('case', SUPPORTED, ids=lambda c: c['name'])
@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('mode', ['no_header_off', 'header_off', 'no_header_protected'])
@pytest.mark.parametrize('commit_path', ['inline', 'repair'])
def test_frozen_main_complete_entry(env, monkeypatch, case, kind, mode, commit_path):
    if guard_unavailable():
        return
    store, db, _, cfg = env
    body, auth, _ = prepare(env, case, kind=kind)
    catalog(monkeypatch, body)
    # CPU-loaded xdist workers must not turn this SQL differential into an
    # upload scheduling test. Keep middleware enabled with an explicit budget.
    cfg.request_body_read_timeout_seconds = 300
    cfg.async_settle_enabled = False
    cfg.async_settle_protection = mode == 'no_header_protected'
    clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    real_datetime = dt.datetime
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, real_datetime)
    class Clock(dt.datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock
    def iso_clock():
        return clock.isoformat().replace('+00:00', 'Z')
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(storage_gcp_authorize, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_gcp_settle_outbox, '_iso_now', lambda: clock.isoformat().replace('+00:00', 'Z'))
    monkeypatch.setattr(storage_gcp_settle_outbox.uuid, 'uuid4', lambda: SimpleNamespace(hex='oracle-owner'))
    # Exercise benchmark INSERTs too; the shared handler fixture disables them.
    store.generation_store._analytics_outbox = storage_gcp_analytics_outbox.SpannerAnalyticsOutbox(
        db, store._param_types)
    initial = save(db)
    outputs = []
    for frozen in (True, False):
        clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
        with monkeypatch.context() as patch:
            if frozen:
                settings_data = cfg.model_dump()
                with execution_guard(settings_data, body) as setup_seen:
                    store, db, settings, client = frozen_environment(patch, settings_data, body)
                inventory(setup_seen)
                authorize = module('storage_gcp_authorize')
                outbox = module('storage_gcp_settle_outbox')
                drain = module('services.settle_outbox_drain')
                acquisition = module('acquisition')
            else:
                from trusted_router import acquisition
                store, db, _, settings = env
                authorize, outbox, drain = storage_gcp_authorize, storage_gcp_settle_outbox, settle_outbox_drain
                store.settle_outbox = outbox.SpannerSettleOutbox(
                    db, store._param_types, async_fence=settings.async_settle_protection)
                client = _client(settings)
            restore(db, initial)
            patch.setattr(authorize, 'utcnow', Clock.now)
            patch.setattr(outbox, '_iso_now', iso_clock)
            patch.setattr(authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            patch.setattr(acquisition, '_usage_check_after', OrderedDict())
            with execution_guard(store, db, client, body) if frozen else nullcontext(set()) as seen:
                trace = []
                def record(cls, method, trace=trace):
                    original = getattr(cls, method)
                    def invoke(self, *args, **kwargs):
                        trace.append((cls.__name__, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
                        return original(self, *args, **kwargs)
                    patch.setattr(cls, method, invoke)
                for method in ('execute_sql', 'execute_update', 'batch_update', 'insert_or_update'):
                    record(_FakeTransaction, method)
                record(_FakeSnapshot, 'execute_sql')
                for method in ('insert_or_update', 'delete', '__exit__'):
                    record(_FakeBatch, method)
                rpc = ('snapshot_execute_sql_calls', 'transaction_execute_sql_calls',
                       'transaction_execute_update_calls', 'transaction_batch_update_calls', 'commits', 'rollback_calls')
                before = [getattr(db, n) for n in rpc]
                with patch.context() as fault:
                    if commit_path == 'repair':
                        fault.setattr(type(store), 'typed_settle_one_commit_result', lambda *a, **kw: None)
                        def crash(*a, **kw):
                            raise RuntimeError('F1 crash after durable insert before finalize')
                        fault.setattr(type(store), 'typed_finalize_gateway_authorization_result', crash)
                    reply = client.post('/v1/internal/gateway/'+kind, json=legacy_body(body),
                                        headers={'X-TR-Settlement-Mode': 'async-v1'} if mode == 'header_off' else {})
                    assert reply.status_code == 200, reply.text
                client.close()
                value = reply.json()
                value.get('data', {}).pop('timing', None)
                stage = save(db)
                clock += dt.timedelta(seconds=61)
                drained = drain.drain_settle_outbox(10, settings=settings)
                outputs.append((value, stage, drained, save(db), trace,
                                [getattr(db, n)-v for n, v in zip(rpc, before, strict=True)]))
            if frozen:
                inventory(seen)
                calls = {(name, qualname) for name, qualname, _, _, _ in seen}
                assert ('trusted_router.storage_models', 'GatewayAuthorization.record_finalization') in calls
                assert ('trusted_router.storage_codec', 'json_body') in calls
                if kind == 'settle':
                    assert ('trusted_router.storage_models', 'Generation.from_settle_body') in calls
                    assert ('trusted_router.storage_models', 'ProviderBenchmarkSample.from_generation') in calls
    def compare(left, right, path=()):
        if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
            for key in left:
                compare(left[key], right[key], (*path, key))
        elif isinstance(left, (list, tuple)) and isinstance(right, type(left)) and len(left) == len(right):
            for i, (a, b) in enumerate(zip(left, right, strict=True)):
                compare(a, b, (*path, i))
        else:
            assert left == right, (path, left, right)
    compare(outputs[0], outputs[1])
    row = db.settle_outbox[(auth.id, kind)]
    assert row['status'] == 'done' and row['settle_body'] is None and row['terminal_at'] is not None
    assert db.reservations[auth.credit_reservation_id]['settled']


@pytest.mark.proof_oracle
@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_frozen_main_protected_header_rejection(env, monkeypatch, kind):
    if guard_unavailable():
        return
    body, _, _ = prepare(env, kind=kind)
    cfg = env[3]
    # CPU-loaded xdist workers must not turn this SQL differential into an
    # upload scheduling test. Keep middleware enabled with an explicit budget.
    cfg.request_body_read_timeout_seconds = 300
    cfg.async_settle_enabled = False
    initial = save(env[1])
    results = []
    for frozen in (True, False):
        with monkeypatch.context() as patch:
            if frozen:
                settings_data = cfg.model_dump()
                with execution_guard(settings_data, body) as setup_seen:
                    _, db, _, client = frozen_environment(patch, settings_data, body)
                    client.app.state.async_settle = frozen_runtime()
                inventory(setup_seen)
                restore(db, initial)
            else:
                db = env[1]
                client = _client(cfg)
            if not frozen:
                client.app.state.async_settle = env[2]
            with execution_guard(db, client, body) if frozen else nullcontext(set()) as seen:
                reply = client.post('/v1/internal/gateway/'+kind, json=body,
                                    headers={'X-TR-Settlement-Mode': 'async-v1'})
                client.close()
                results.append((reply.status_code, reply.content, save(db)))
            if frozen:
                inventory(seen)
    assert results[0] == results[1]
    assert results[1][0] == 200 and b'"reason":"disabled"' in results[1][1]
    assert results[1][2] == initial


@pytest.mark.parametrize('bridge', [
    'partial_cache', 'simple_namespace', 'external_default', 'shared_fake_io',
    'cached_bound_call', 'dataclass_factory', 'captured_callback', 'pydantic_validator',
    'partial_argument', 'partial_keyword', 'bound_method', 'cache_result', 'spoofed_module',
    'external_callable_class', 'bounded_cache_result', 'copied_globals', 'external_cache_result',
])
def test_guard_reviewer_references(monkeypatch, bridge):
    if guard_unavailable():
        return
    import dataclasses
    import functools

    from pydantic import create_model, field_validator

    from tests.fakes import spanner
    from trusted_router import storage_errors

    frozen = module('storage_errors')
    live = storage_errors.transient_store_error_types
    live()  # The construction starts with a warmed C cache.
    assert live.cache_info().currsize
    if bridge == 'partial_cache':
        root = functools.partial(live)
    elif bridge == 'simple_namespace':
        root = SimpleNamespace(callback=live)
    elif bridge == 'external_default':
        def root(callback=functools.partial(live)):
            return callback()
    elif bridge == 'shared_fake_io':
        # The shared fake's module globals must also be roots.
        monkeypatch.setattr(spanner, 'review_callback', functools.partial(live), raising=False)
        root = None
    elif bridge == 'cached_bound_call':
        root = live.__call__
    elif bridge == 'dataclass_factory':
        root = dataclasses.make_dataclass('ReviewDefault', [
            ('value', object, dataclasses.field(default_factory=functools.partial(live)))])
    elif bridge == 'captured_callback':
        callback = storage_errors.is_transient_store_error
        def root():
            return callback(ValueError())
    elif bridge == 'pydantic_validator':
        root = create_model('ReviewModel', value=(object, ...), __validators__={
            'review': field_validator('value')(storage_errors.is_transient_store_error)})
    elif bridge == 'partial_argument':
        root = functools.partial(lambda callback: callback(), live)
    elif bridge == 'partial_keyword':
        root = functools.partial(lambda callback: callback(), callback=live)
    elif bridge == 'bound_method':
        class Holder:
            def __init__(self):
                self.callback = live
            def call(self):
                return self.callback()
        root = Holder().call
    elif bridge in {'cache_result', 'bounded_cache_result', 'external_cache_result'}:
        @functools.lru_cache(maxsize=1 if bridge == 'bounded_cache_result' else None)
        def root():
            return importlib.import_module('trusted_router.storage_errors').transient_store_error_types
        if bridge == 'external_cache_result':
            root = functools.cache(FunctionType(root.__wrapped__.__code__,
                                                {'__name__': 'review_external', 'importlib': importlib}))
        root()  # Live callable is held in C cache state, not a closure/default.
    elif bridge == 'copied_globals':
        root = FunctionType((lambda: None).__code__,
                            {'__name__': 'review_fixture_copy', 'unused_callback': live})
    elif bridge == 'external_callable_class':
        class External:
            __module__ = 'review_external'
            def __call__(self, callback=functools.partial(live)):
                return callback()
        root = External()
    else:
        root = FunctionType(storage_errors.is_transient_store_error.__code__,
                            {'__name__': 'harness_disguise'})
    monkeypatch.setattr(frozen, 'review_bridge', root, raising=False)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard():
            pytest.fail('a dormant live bridge reached the guarded body')


@pytest.mark.parametrize('bridge', ['private_slot', 'mapping_proxy'])
def test_guard_reviewer_separate_warmed_cache(monkeypatch, bridge):
    if guard_unavailable():
        return
    import functools
    from types import MappingProxyType

    from trusted_router.storage_errors import transient_store_error_types

    # A separate wrapper is essential: clearing the live module's own cache
    # would mask a traversal gap by turning the construction into a profiled cold call.
    live = functools.lru_cache(maxsize=1)(transient_store_error_types.__wrapped__)
    expected = live()
    assert live.cache_info().currsize == 1
    if bridge == 'private_slot':
        class Holder:
            __slots__ = ('__callback',)
            def __init__(self, callback):
                self.__callback = callback
            def __call__(self):
                return self.__callback()
        root = Holder(live)
        action = root
    else:
        root = MappingProxyType({'callback': live})
        def action():
            return root['callback']()
    monkeypatch.setattr(module('storage_errors'), 'review_root', root, raising=False)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard():
            assert action() is expected
            assert live.cache_info().hits == 1


@pytest.mark.parametrize('api', [
    '_thread.start_new_thread', '_thread.start_joinable_thread',
    'threading._start_joinable_thread',
])
def test_guard_raw_thread_finishes_before_exit(api):
    if guard_unavailable():
        return
    import _thread
    import sys
    import threading
    import time

    namespace, name = api.split('.')
    owner = _thread if namespace == '_thread' else threading
    if not hasattr(owner, name):
        # Older interpreters cannot enter this native path: attribute
        # resolution fails closed. Pin that reason instead of skipping.
        with pytest.raises(AttributeError, match=name):
            getattr(owner, name)
        return
    values = []
    done = threading.Event()
    def worker():
        values.append(importlib.import_module('trusted_router.storage_errors')
                      .is_transient_store_error(ValueError()))
        done.set()
    original = getattr(owner, name)
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    with pytest.raises(AssertionError, match='live callable'):
        with execution_guard():
            if name == 'start_new_thread':
                ident = getattr(owner, name)(worker, ())
                assert done.wait(10)
                deadline = time.monotonic() + 10
                while ident in sys._current_frames():
                    assert time.monotonic() < deadline, 'raw worker did not exit'
                    time.sleep(.001)
            else:
                handle = getattr(owner, name)(worker)
                handle.join(10)
                assert handle.is_done()
            assert values == [False]
    assert getattr(owner, name) is original
    assert sys.getprofile() is previous
    assert threading.getprofile() is previous_thread


@pytest.mark.parametrize('live_at_shutdown', [False, True])
def test_guard_joins_raw_worker_with_profiler_active(live_at_shutdown):
    if guard_unavailable():
        return
    import _thread
    import threading
    import time

    done = threading.Event()
    def worker():
        time.sleep(.1)  # Still running when the guarded body returns.
        if live_at_shutdown:
            importlib.import_module('trusted_router.storage_errors').is_transient_store_error(ValueError())
        done.set()
    expected = pytest.raises(AssertionError, match='live callable') if live_at_shutdown else nullcontext()
    with expected:
        with execution_guard():
            _thread.start_new_thread(worker, ())
    assert done.is_set()


@pytest.mark.parametrize('live_callback', [False, True])
def test_guard_profiles_raw_exception_cleanup(monkeypatch, live_callback):
    if guard_unavailable():
        return
    import _thread
    import sys
    import threading

    done = threading.Event()
    def hook(unraisable):
        if live_callback:
            importlib.import_module('trusted_router.storage_errors').is_transient_store_error(ValueError())
        done.set()
    def worker():
        raise ValueError('raw exception cleanup witness')
    monkeypatch.setattr(sys, 'unraisablehook', hook)
    expected = pytest.raises(AssertionError, match='live callable') if live_callback else nullcontext()
    with expected:
        with execution_guard():
            _thread.start_new_thread(worker, ())
    assert done.is_set()


def test_guard_rejects_unfinished_raw_worker():
    if guard_unavailable():
        return
    import _thread
    import sys
    import threading
    import time

    release = threading.Event()
    done = threading.Event()
    def worker():
        release.wait(15)
        done.set()
    original = _thread.start_new_thread
    try:
        with pytest.raises(AssertionError, match='worker threads must finish'):
            with execution_guard():
                ident = _thread.start_new_thread(worker, ())
    finally:
        release.set()
        assert done.wait(10)
        deadline = time.monotonic() + 10
        while ident in sys._current_frames():
            assert time.monotonic() < deadline
            time.sleep(.001)
    assert _thread.start_new_thread is original


def test_guard_rejects_prebound_raw_starter():
    if guard_unavailable():
        return
    import _thread
    import threading

    start = _thread.start_new_thread
    ran = threading.Event()
    with pytest.raises(AssertionError, match='unwrapped raw worker'):
        with execution_guard():
            start(ran.set, ())
    assert not ran.is_set()


@pytest.mark.parametrize('path', [
    'direct', 'partial_func', 'partial_args', 'partial_keywords', 'tuple',
    'dict', 'closure', 'bound_method',
])
def test_guard_rejects_reachable_prebound_starter(monkeypatch, path):
    if guard_unavailable():
        return
    import _thread
    import functools

    starter = _thread.start_new_thread
    if path == 'direct':
        root = starter
    elif path == 'partial_func':
        root = functools.partial(starter)
    elif path == 'partial_args':
        root = functools.partial(lambda callback: callback, starter)
    elif path == 'partial_keywords':
        root = functools.partial(lambda callback: callback, callback=starter)
    elif path == 'tuple':
        root = (starter,)
    elif path == 'dict':
        root = {'starter': starter}
    elif path == 'closure':
        def root():
            return starter
    else:
        class Holder:
            def __init__(self, callback):
                self.callback = callback
            def call(self):
                return self.callback
        root = Holder(starter).call
    monkeypatch.setattr(module('storage_errors'), 'review_starter', root, raising=False)
    with pytest.raises(AssertionError, match=(
            'prebound native thread starter reachable from frozen roots; '
            'profiling cannot be guaranteed for threads it creates')):
        with execution_guard():
            pytest.fail('undetected prebound starter reached the frozen leg')


@pytest.mark.parametrize('api', [
    '_thread.start_new_thread', '_thread.start_joinable_thread',
    '_thread._start_joinable_thread', 'threading._start_new_thread',
    'threading._start_joinable_thread', 'Thread.start', 'Thread._bootstrap',
])
def test_guard_rejects_each_prebound_starter(api):
    if guard_unavailable():
        return
    import _thread
    import sys
    import threading

    namespace, name = api.split('.')
    ran = threading.Event()
    owner = (threading.Thread(target=ran.set) if namespace == 'Thread'
             else _thread if namespace == '_thread' else threading)
    if not hasattr(owner, name):
        # Older interpreters cannot enter this native path: attribute
        # resolution fails closed. Pin that reason instead of skipping.
        with pytest.raises(AttributeError, match=name):
            getattr(owner, name)
        return
    # Explicit harness roots obey the same rejection as frozen-module globals.
    with pytest.raises(AssertionError, match='prebound native thread starter'):
        with execution_guard(getattr(owner, name)):
            pytest.fail('undetected prebound starter reached the frozen leg')
    assert not ran.is_set()
    if namespace == 'Thread':
        assert owner.ident is None


@pytest.mark.parametrize('api', ['raw', 'joinable', 'thread'])
@pytest.mark.parametrize('use_partial', [False, True])
@pytest.mark.parametrize('live_callback', [False, True])
def test_guard_new_starter_paths_are_profiled(monkeypatch, api, use_partial, live_callback):
    if guard_unavailable():
        return
    import _thread
    import functools
    import sys
    import threading
    import time

    if api == 'joinable' and not hasattr(_thread, 'start_joinable_thread'):
        # No joinable worker can be created through an absent native API.
        with pytest.raises(AttributeError, match='start_joinable_thread'):
            _ = _thread.start_joinable_thread
        return
    frozen = module('storage_errors')
    # Match the reviewer's detached worker: only the transient held callback
    # supplies live code, with no computed import or external registry lookup.
    namespace = {'__name__': 'review_detached_worker', 'getprofile': (
        (lambda: sys.monitoring.get_events(4)) if sys.version_info[:2] == (3, 12)
        else sys.getprofile)}
    exec('def worker(callback, values, profiles, done):\n'
         ' profiles.append(getprofile())\n'
         ' values.append(callback(ValueError()))\n'
         ' done.set()\n', namespace)
    monkeypatch.setattr(frozen, 'review_worker', namespace['worker'], raising=False)
    values, profiles = [], []
    done = threading.Event()
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    original = _thread.start_new_thread
    expected = pytest.raises(AssertionError, match='live callable') if live_callback else nullcontext()
    with expected:
        with execution_guard():
            frozen.review_live_callback = (importlib.import_module('trusted_router.storage_errors')
                                          if live_callback else frozen).is_transient_store_error
            try:
                args = (frozen.review_live_callback, values, profiles, done)
                if api == 'raw':
                    starter = _thread.start_new_thread
                    if use_partial:
                        starter = functools.partial(starter)
                    ident = starter(frozen.review_worker, args)
                    assert done.wait(10)
                    deadline = time.monotonic() + 10
                    while ident in sys._current_frames():
                        assert time.monotonic() < deadline
                        time.sleep(.001)
                elif api == 'joinable':
                    starter = _thread.start_joinable_thread
                    if use_partial:
                        starter = functools.partial(starter)
                    handle = starter(functools.partial(frozen.review_worker, *args))
                    handle.join(10)
                    assert handle.is_done()
                else:
                    thread = threading.Thread(target=frozen.review_worker, args=args)
                    starter = functools.partial(thread.start) if use_partial else thread.start
                    starter()
                    thread.join(10)
                    assert not thread.is_alive()
            finally:
                del frozen.review_live_callback
    assert values == [False]
    assert len(profiles) == 1 and profiles[0]
    assert _thread.start_new_thread is original
    assert sys.getprofile() is previous
    assert threading.getprofile() is previous_thread


@pytest.mark.parametrize('kind', [
    'mapping_key', 'mapping_value', 'sequence', 'set', 'inherited_private_slot',
    'shadowed_slot', 'wrapped_descriptor', 'cached_property', 'getstate',
])
def test_reference_walk_python_state(kind):
    from collections.abc import Mapping, Sequence, Set
    from functools import cached_property

    from tests.fakes.frozen_package import _references

    marker = object()
    # A synthetic function namespace is ordinary held state, not a registered
    # external module dictionary. GC reaches the marker through that state
    # without invoking container protocols, descriptors or __getstate__.
    namespace = {'__name__': 'review_external', 'marker': marker,
                 'Mapping': Mapping, 'Sequence': Sequence, 'Set': Set}
    exec("""
class ReadableMapping(Mapping):
    def __iter__(self): return iter(data)
    def __getitem__(self, key): return data[key]
    def __len__(self): return len(data)
class ReadableSequence(Sequence):
    def __getitem__(self, index): return data[index]
    def __len__(self): return len(data)
class ReadableSet(Set):
    def __contains__(self, value): return value in data
    def __iter__(self): return iter(data)
    def __len__(self): return len(data)
class Wrapped:
    @property
    def __wrapped__(self): return marker
class State:
    def __getstate__(self): return {'callback': marker}
""", namespace)
    if kind in {'mapping_key', 'mapping_value'}:
        namespace['data'] = {marker: None} if kind == 'mapping_key' else {'callback': marker}
        root = namespace['ReadableMapping']()
    elif kind in {'sequence', 'set'}:
        namespace['data'] = (marker,)
        root = namespace['ReadableSequence' if kind == 'sequence' else 'ReadableSet']()
    elif kind in {'inherited_private_slot', 'shadowed_slot'}:
        class Base:
            __slots__ = ('__callback', 'callback')
        class Child(Base):
            __slots__ = ('callback',)
        root = Child()
        descriptor = Base.__dict__['_Base__callback' if kind == 'inherited_private_slot'
                                   else 'callback']
        descriptor.__set__(root, marker)
        root.callback = None
    elif kind == 'wrapped_descriptor':
        root = namespace['Wrapped']()
    elif kind == 'cached_property':
        class Cached:
            @cached_property
            def callback(self):
                return None
        root = Cached()
        root.__dict__['callback'] = marker
    else:
        root = namespace['State']()
    assert any(value is marker for value in _references([root], namespaces=(ALIAS, 'tests.fakes.spanner')))


@pytest.mark.parametrize('root_kind', ['namespace', 'module', 'logger'])
def test_guard_explicit_harness_root(root_kind):
    if guard_unavailable():
        return
    import functools
    import logging
    from types import ModuleType

    from trusted_router.storage_errors import transient_store_error_types

    transient_store_error_types()
    if root_kind == 'logger':
        io = logging.Logger('review_explicit_io')
    else:
        io = SimpleNamespace() if root_kind == 'namespace' else ModuleType('review_external_io')
    io.callback = functools.partial(transient_store_error_types)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(io):
            pytest.fail('unattached fake IO escaped the root scan')


def test_guard_dynamic_import():
    if guard_unavailable():
        return
    with pytest.raises(AssertionError, match='live callable'):
        with execution_guard():
            importlib.import_module('trusted_router.storage_errors').is_transient_store_error(ValueError())


def test_guard_preexisting_worker_cost_bridge(monkeypatch):
    if guard_unavailable():
        return
    from concurrent.futures import ThreadPoolExecutor

    from trusted_router.routes.internal import gateway

    # Same +1 edit and prestarted worker as the independent bridge.py, without
    # changing any production file in this checkout.
    source = inspect.getsource(gateway._native_batch_cost_or_error)
    assert 'return cost_microdollars\n' in source
    namespace = dict(vars(gateway))
    exec(compile(source.replace('return cost_microdollars\n', 'return cost_microdollars + 1\n'),
                 gateway.__file__, 'exec'), namespace)
    live_cost = namespace['_native_batch_cost_or_error']
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: None).result()
        def bridge(*args, **kwargs):
            return pool.submit(live_cost, *args, **kwargs).result()
        assert bridge(2, route_type=None, provider='openai') == 3
        monkeypatch.setattr(module('routes.internal.gateway'), '_native_batch_cost_or_error', bridge)
        with pytest.raises(AssertionError, match='pre-existing worker'):
            with execution_guard():
                module('routes.internal.gateway')._native_batch_cost_or_error(
                    2, route_type=None, provider='openai')


def test_guard_clears_both_namespaces_and_harness_caches(monkeypatch):
    if guard_unavailable():
        return
    import functools

    from trusted_router import storage_errors

    @functools.cache
    def nested():
        return 42
    @functools.cache
    def outer():
        return nested
    nested()
    outer()
    frozen = module('storage_errors').transient_store_error_types
    live = storage_errors.transient_store_error_types
    frozen()
    live()
    assert frozen.cache_info().currsize and live.cache_info().currsize
    with execution_guard(SimpleNamespace(callback=functools.partial(outer).__call__)):
        assert all(cache.cache_info().currsize == 0 for cache in (frozen, live, outer, nested))


def assert_production_import_fence(root):
    import ast

    forbidden = ('tests', 'frozen_main')
    violations = []
    for path in sorted(root.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in {'__import__', 'import_module'}
                or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'
            ):
                names = [arg.value for arg in node.args[:1]
                         if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            for name in names:
                if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden):
                    violations.append(f'{path.relative_to(root)}:{node.lineno}: {name}')
    assert not violations, 'production imports test assets: ' + ', '.join(violations)


def test_production_import_fence():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    assert_production_import_fence(root / 'src/trusted_router')
    result = subprocess.run(  # noqa: S603 - fixed fresh interpreter, no inherited test imports
        [sys.executable, '-c',
         "import sys; import trusted_router; "
         "assert 'tests.fakes.frozen_package' not in sys.modules; "
         "assert not any(n == 'frozen_main' or n.startswith('frozen_main.') for n in sys.modules)"],
        cwd=root, env={**os.environ, 'PYTHONPATH': str(root / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_import_fence_reviewer_witness(tmp_path):
    import shutil

    source = Path(__file__).resolve().parents[1] / 'src/trusted_router'
    target = tmp_path / 'trusted_router'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    assert_production_import_fence(target)
    path = target / 'storage_errors.py'
    path.write_text(path.read_text() + '\nfrom tests.fakes.frozen_package import module as _review_snapshot_module\n')
    with pytest.raises(AssertionError, match=r'storage_errors.py:.*tests.fakes.frozen_package'):
        assert_production_import_fence(target)


def test_guard_clears_shared_typing_cache_between_legs():
    if guard_unavailable():
        return
    import typing
    from typing import Annotated

    from trusted_router.routes.internal.lightning import Credit

    # This exact shared stdlib cache caused a second-case false positive after
    # the live HTTP app registered its Credit response schema.
    module('billing_snapshot')
    Annotated[Credit, 'f1-review-typing-cache']
    # This is an explicit shared-runtime registry control, not a traversal of
    # every module reachable through typing's interpreter-global namespace.
    cache = next(cleanup.__self__ for cleanup in typing._cleanups
                 if cleanup.__self__.__qualname__ in {'Annotated._class_getitem_inner', 'Annotated'})
    assert cache.cache_info().currsize
    with execution_guard():
        assert cache.cache_info().currsize == 0


GRAPH_WITNESSES = (
    'nested_mapping_slot', 'code_constants', 'frozen_closure',
    'class_descriptor', 'dataclass_frozenset_tuple',
)


def graph_witness(kind, callback):
    """Hold callback only along the intended path, never in helper globals."""
    import dataclasses
    from types import MappingProxyType

    # Minimal globals avoid an alternative path back through this test module.
    namespace = {'__name__': 'frozen_main.graph_witness', '__builtins__': {}}
    if kind == 'nested_mapping_slot':
        class Holder:
            __slots__ = ('payload',)
        root = Holder()
        root.payload = {'outer': MappingProxyType({'inner': {'callback': callback}})}
        return root
    if kind == 'code_constants':
        code = compile('def held(): return None', '<frozen-graph-witness>', 'exec').co_consts[0]
        return FunctionType(code.replace(co_consts=(None, callback)), namespace)
    if kind == 'frozen_closure':
        code = compile('def capture(callback):\n def held(): return callback\n return held',
                       '<frozen-graph-witness>', 'exec')
        exec(code, namespace)
        return namespace.pop('capture')(callback)
    if kind == 'class_descriptor':
        # The getter carries the callable as its default. No closure owns it.
        code = compile('def getter(self, callback): return callback',
                       '<frozen-graph-witness>', 'exec').co_consts[0]
        getter = FunctionType(code, namespace, argdefs=(callback,))
        return type('FrozenDescriptor', (), {'__module__': namespace['__name__'],
                                           'held': property(getter)})
    assert kind == 'dataclass_frozenset_tuple'
    return dataclasses.make_dataclass('FrozenDefault', [
        ('held', object, dataclasses.field(default=frozenset({('nested', (callback,))})))],
        namespace={'__module__': namespace['__name__']})


@pytest.mark.parametrize('kind', GRAPH_WITNESSES)
def test_guard_gc_composed_witness(monkeypatch, kind):
    if guard_unavailable():
        return
    import functools

    from trusted_router.storage_errors import transient_store_error_types

    live = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    live()
    assert live.cache_info().currsize == 1
    root = graph_witness(kind, live)
    monkeypatch.setattr(module('storage_errors'), 'graph_witness', root, raising=False)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard():
            pytest.fail('undetected construction reached the frozen leg')


@pytest.mark.parametrize('kind', GRAPH_WITNESSES)
def test_gc_composed_witness_reaches_marker(kind):
    from tests.fakes.frozen_package import _references

    marker = object()
    assert any(value is marker for value in _references([graph_witness(kind, marker)],
                                                          namespaces=(ALIAS, 'tests.fakes.spanner')))


def test_reference_walk_atomic_code_constants():
    import gc

    from tests.fakes.frozen_package import _references

    marker = object()
    code = (lambda: None).__code__.replace(co_consts=(None, marker))
    # CPython code objects are untracked and omit even Python object constants.
    assert not gc.is_tracked(code)
    assert gc.get_referents(code) == []
    assert any(value is marker for value in _references([code], namespaces=(ALIAS, 'tests.fakes.spanner')))


def test_reference_walk_bound_and_cycle():
    from tests.fakes.frozen_package import _references

    root = []
    root.append(root)
    assert list(_references([root], max_objects=1)) == [root]
    root.append(object())
    with pytest.raises(AssertionError, match='reference graph exceeds 1 objects'):
        list(_references([root], max_objects=1))


def test_guard_live_code_object_argument():
    if guard_unavailable():
        return
    from trusted_router.storage_errors import is_transient_store_error

    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(is_transient_store_error.__code__):
            pytest.fail('undetected code reference reached the frozen leg')


@pytest.mark.parametrize('explicit', ['module', 'dictionary', 'logger'])
def test_reference_registry_boundary_explicit_root_override(monkeypatch, explicit):
    import logging
    import sys
    from types import ModuleType

    from tests.fakes.frozen_package import ALIAS, _references

    marker = object()
    external = ModuleType('f1_external_registry')
    external.marker = marker
    monkeypatch.setitem(sys.modules, external.__name__, external)
    logger = logging.getLogger('f1-external-registry-witness')
    monkeypatch.setattr(logger, 'held_marker', marker, raising=False)
    # Process registries are the only deliberate graph cuts. Ordinary nested
    # object state and unregistered function namespaces have no such boundary.
    assert not any(value is marker for value in _references(
        [SimpleNamespace(registry=external, logger=logger)], namespaces=(ALIAS,)))
    root = {'module': external, 'dictionary': vars(external), 'logger': logger}[explicit]
    assert any(value is marker for value in _references([root], namespaces=(ALIAS,)))


def test_reference_walk_does_not_execute_object_protocols():
    from tests.fakes.frozen_package import _references

    marker = object()
    class Held:
        __slots__ = ('payload',)
        def __iter__(self):
            pytest.fail('reference traversal executed __iter__')
        def __getstate__(self):
            pytest.fail('reference traversal executed __getstate__')
        @property
        def __wrapped__(self):
            pytest.fail('reference traversal executed a property')
    root = Held()
    root.payload = marker
    assert any(value is marker for value in _references([root], namespaces=(ALIAS, 'tests.fakes.spanner')))


def test_guard_explicit_typing_cache_root_is_inspected():
    if guard_unavailable():
        return
    import typing
    from typing import Annotated

    from trusted_router.routes.internal.lightning import Credit

    Annotated[Credit, 'f1-explicit-cache-root']
    # This is an explicit shared-runtime registry control, not a traversal of
    # every module reachable through typing's interpreter-global namespace.
    cache = next(cleanup.__self__ for cleanup in typing._cleanups
                 if cleanup.__self__.__qualname__ in {'Annotated._class_getitem_inner', 'Annotated'})
    assert cache.cache_info().currsize
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(cache):
            pytest.fail('explicit cache root was purged before inspection')


def test_guard_clears_registry_only_typing_cache(monkeypatch):
    if guard_unavailable():
        return
    import functools
    import typing

    @functools.cache
    def callback():
        return 42
    callback()
    # Model 3.14's _tp_cache: the cache is held by the process registry, not a
    # frozen callback closure. The existing Annotated control uses the real API.
    monkeypatch.setattr(typing, '_cleanups', [*typing._cleanups, callback.cache_clear])
    with execution_guard():
        assert callback.cache_info().currsize == 0


def atomic_tzinfo_root(kind, callback):
    from datetime import datetime, time, tzinfo

    class Zone(tzinfo):
        __slots__ = ('callback',)
    zone = Zone()
    zone.callback = callback
    return (datetime(2026, 1, 1, tzinfo=zone) if kind == 'datetime'
            else time(tzinfo=zone)), zone


@pytest.mark.parametrize('kind', ['datetime', 'time'])
def test_reference_walk_atomic_tzinfo(kind):
    import gc

    from tests.fakes.frozen_package import _references

    marker = object()
    root, zone = atomic_tzinfo_root(kind, marker)
    assert root.tzinfo is zone
    assert not gc.is_tracked(root)
    assert gc.get_referents(root) == []  # Yet the tzinfo reference is owned.
    assert any(value is marker for value in _references([root], namespaces=(ALIAS, 'tests.fakes.spanner')))


@pytest.mark.parametrize('kind', ['datetime', 'time'])
def test_guard_atomic_tzinfo_cache(kind):
    if guard_unavailable():
        return
    import functools

    from trusted_router.storage_errors import transient_store_error_types

    cache = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    cache()
    root, _ = atomic_tzinfo_root(kind, cache)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(root):
            pytest.fail('undetected atomic tzinfo construction reached the frozen leg')


ATOMIC_METADATA = ('co_filename', 'co_name', 'co_qualname', 'co_linetable',
                   'co_exceptiontable', 'timezone_offset', 'timezone_name')


def atomic_metadata_root(kind, callback):
    from datetime import timedelta, timezone

    class Text(str):
        pass

    class Blob(bytes):
        pass

    class Delta(timedelta):
        pass

    if kind.startswith('co_'):
        code = (lambda: None).__code__
        original = getattr(code, kind)
        payload = Text(original) if isinstance(original, str) else Blob(original)
        payload.callback = callback
        root = code.replace(**{kind: payload})
        assert getattr(root, kind) is payload
    elif kind == 'timezone_offset':
        payload = Delta(seconds=1)
        payload.callback = callback
        root = timezone(payload)
        assert root.utcoffset(None) is payload
    else:
        payload = Text('frozen-zone')
        payload.callback = callback
        root = timezone(timedelta(0), payload)
        assert root.tzname(None) is payload
    return root


@pytest.mark.parametrize('kind', ATOMIC_METADATA)
def test_reference_walk_atomic_metadata(kind):
    import gc

    from tests.fakes.frozen_package import _references

    marker = object()
    root = atomic_metadata_root(kind, marker)
    assert not gc.is_tracked(root)
    assert gc.get_referents(root) == []
    assert any(value is marker for value in _references([root], namespaces=(ALIAS, 'tests.fakes.spanner')))


@pytest.mark.parametrize('kind', ATOMIC_METADATA)
def test_guard_atomic_metadata_cache(kind):
    if guard_unavailable():
        return
    import functools

    from trusted_router.storage_errors import transient_store_error_types

    cache = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    cache()
    assert cache.cache_info().currsize == 1
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(atomic_metadata_root(kind, cache)):
            pytest.fail('undetected atomic metadata construction reached the frozen leg')


def test_guard_live_code_filename_subclass():
    if guard_unavailable():
        return
    class Filename(str):
        def __contains__(self, item):
            return False

        def startswith(self, prefix, *args):
            return True

    filename = Filename('/live/src/trusted_router/held.py')
    code = (lambda: None).__code__.replace(co_filename=filename)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(code):
            pytest.fail('undetected filename comparison construction reached the frozen leg')


def test_guard_provenance_property_cannot_remove_nested_cache():
    if guard_unavailable():
        return
    import functools

    from trusted_router.storage_errors import transient_store_error_types

    class Holder:
        def __init__(self, callback):
            self.mapping = {'nested': {'callback': callback}}

        @property
        def __module__(self):
            self.mapping['nested'].clear()
            return 'review_external'

    cache = functools.lru_cache(maxsize=1)(transient_store_error_types.__wrapped__)
    cache()
    root = Holder(cache)
    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(root):
            pytest.fail('undetected provenance-property construction reached the frozen leg')
    assert root.mapping['nested']['callback'] is cache


@pytest.mark.parametrize('attribute', ['__class__', '__module__'])
def test_guard_does_not_execute_metadata_properties(attribute):
    if guard_unavailable():
        return
    def read(self):
        pytest.fail('guard executed a metadata property')

    holder = type('MetadataProperties', (), {attribute: property(read)})()
    with execution_guard(holder):
        pass


def test_guard_does_not_execute_namespace_comparisons():
    if guard_unavailable():
        return
    class Name(str):
        def __eq__(self, other):
            pytest.fail('guard executed namespace equality')

        def startswith(self, prefix, *args):
            pytest.fail('guard executed namespace startswith')

    callback = FunctionType((lambda: None).__code__, {'__name__': 'review_external'})
    callback.__module__ = Name('review_external')
    with execution_guard(callback):
        pass


def test_guard_clears_native_cache_despite_shadowed_method():
    if guard_unavailable():
        return
    import functools

    callback = FunctionType((lambda: 42).__code__, {'__name__': 'review_external'})
    cache = functools.lru_cache(maxsize=1)(callback)
    cache()
    cache.cache_clear = lambda: None
    assert cache.cache_info().currsize == 1
    with execution_guard(cache):
        assert cache.cache_info().currsize == 0


@pytest.mark.parametrize('kind', ['dictionary_get', 'key_equality'])
def test_guard_cache_metadata_cannot_remove_cached_live_result(kind):
    if guard_unavailable():
        return
    import functools

    code = compile("def held():\n return importlib.import_module('trusted_router.storage_errors').is_transient_store_error",
                   '<frozen-metadata-witness>', 'exec').co_consts[0]
    callback = FunctionType(code, {'__name__': 'review_external', 'importlib': importlib})
    cache = functools.lru_cache(maxsize=1)(callback)
    cache()
    assert cache.cache_info().currsize == 1

    if kind == 'dictionary_get':
        class Labels(dict):
            def get(self, name, default=None):
                functools._lru_cache_wrapper.cache_clear(self.target)
                return 'review_external'
        labels = Labels(cache.__dict__)
        labels.target = cache
        cache.__dict__ = labels
    else:
        class Name(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                functools._lru_cache_wrapper.cache_clear(self.target)
                return str.__eq__(self, other)
        label = Name('__module__')
        label.target = cache
        del cache.__dict__['__module__']
        cache.__dict__[label] = 'review_external'

    with pytest.raises(AssertionError, match='live reference'):
        with execution_guard(cache):
            pytest.fail('undetected cache-metadata construction reached the frozen leg')
    assert cache.cache_info().currsize == 1


@pytest.mark.parametrize('kind', ['ref', 'ref_subclass', 'overridden_call', 'weak_value',
                                 'weak_key', 'weak_set', 'weak_method', 'finalize',
                                 'proxy', 'callable_proxy'])
def test_guard_weak_reference_targets(monkeypatch, kind):
    if guard_unavailable():
        return
    import functools
    import gc
    import weakref

    from trusted_router.storage_errors import transient_store_error_types

    held = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    held()
    calls = []
    owner = None
    finalizer = None
    if kind == 'ref':
        root = weakref.ref(held)
        assert gc.get_referents(root) == []
    elif kind in {'ref_subclass', 'overridden_call'}:
        class Ref(weakref.ref):
            pass

        class OverriddenRef(weakref.ref):
            def __call__(self):
                calls.append('__call__')
                return None

        root = (Ref if kind == 'ref_subclass' else OverriddenRef)(held)
        assert (type(root).__call__ is weakref.ReferenceType.__call__) == (kind == 'ref_subclass')
        assert weakref.ReferenceType.__call__(root) is held
    elif kind == 'weak_value':
        root = weakref.WeakValueDictionary(cache=held)
    elif kind == 'weak_key':
        root = weakref.WeakKeyDictionary({held: None})
    elif kind == 'weak_set':
        root = weakref.WeakSet([held])
    elif kind == 'weak_method':
        # A real bound live method, with an independent warmed cache retained
        # only by its instance. GC reaches _func_ref; the native base accessor
        # reaches the instance despite WeakMethod's Python __call__ override.
        from trusted_router.storage_models import GatewayAuthorization

        owner = object.__new__(GatewayAuthorization)
        object.__setattr__(owner, 'review_held_cache', held)
        root = weakref.WeakMethod(owner.record_finalization)
        assert weakref.ReferenceType.__call__(root) is owner
        assert any(type(value) is weakref.ReferenceType for value in gc.get_referents(root))
    elif kind == 'finalize':
        # The class registry is an ordinary GC edge, not an external module
        # dictionary. Never execute the callback; detach it in finally.
        root = finalizer = weakref.finalize(held, calls.append, 'finalized')
        finalizer.atexit = False
    elif kind == 'proxy':
        class Holder:
            pass

        owner = Holder()
        owner.cache = held
        root = weakref.proxy(owner)
        assert type(root) is weakref.ProxyType
        assert gc.get_referents(root) == []
    else:
        root = weakref.proxy(held)
        assert type(root) is weakref.CallableProxyType
        assert gc.get_referents(root) == []

    # Confirm containers expose their internal native refs via GC alone.
    # Stop at refs so this check cannot itself resolve the weak target.
    if kind in {'weak_value', 'weak_key', 'weak_set', 'finalize'}:
        pending, seen, refs = [root], set(), []
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            if issubclass(type(value), weakref.ReferenceType):
                refs.append(value)
            elif type(value) is not type(weakref):
                pending.extend(gc.get_referents(value))
        assert any(weakref.ReferenceType.__call__(ref) is held for ref in refs)

    reason = ('weak proxy in frozen reference graph: no safe native target accessor; '
              'hold the strong object instead' if kind in {'proxy', 'callable_proxy'}
              else 'live reference.*trusted_router.storage_')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module('storage_errors'), 'review_weak_root', root, raising=False)
            with pytest.raises(AssertionError, match=reason):
                with execution_guard():
                    pytest.fail('undetected weak reference reached the frozen leg')
        assert calls == [], 'audit dispatched a weakref override or finalizer'
        assert held.cache_info().currsize == 1
        assert held.cache_info().misses == 1
        assert held.cache_info().hits == 0
    finally:
        if finalizer is not None:
            finalizer.detach()


@pytest.mark.parametrize('kind', ['normalized', 'explicit_class', 'explicit_cache',
                                 'registered', 'owned'])
def test_guard_shared_abc_weak_cache_normalization(monkeypatch, kind):
    if guard_unavailable():
        return
    import _abc
    import abc
    import sys
    from types import ModuleType

    from trusted_router.storage_models import GatewayAuthorization

    shared = ModuleType('proof_shared_abc')
    shared.abc = abc
    exec('class Shared(abc.ABC): pass', shared.__dict__)
    base = shared.Shared
    if kind == 'owned':
        base.__module__ = __name__
    cache = vars(base)['_abc_impl']
    if kind == 'registered':
        base.register(GatewayAuthorization)
    else:
        assert not issubclass(GatewayAuthorization, base)
    registry, positive, negative, _ = _abc._get_dump(base)
    assert any(ref() is GatewayAuthorization for ref in registry | positive | negative)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, shared.__name__, shared)
        patch.setattr(module('storage_errors'), 'review_shared_abc', base, raising=False)
        roots = (base,) if kind == 'explicit_class' else ((cache,) if kind == 'explicit_cache' else ())
        if kind == 'normalized':
            with execution_guard(*roots):
                assert _abc._get_dump(base)[:3] == (set(), set(), set())
        else:
            with pytest.raises(AssertionError, match='live reference.*GatewayAuthorization'):
                with execution_guard(*roots):
                    pytest.fail('owned, explicit or registered ABC weak target was discarded')


def test_reference_walk_dead_weakref():
    import weakref

    from tests.fakes.frozen_package import _references

    class Holder:
        pass

    held = Holder()
    root = weakref.ref(held)
    del held
    assert weakref.ReferenceType.__call__(root) is None
    assert list(_references([root])) == [root]


FRAME_WITNESSES = ('active_frame', 'exception_traceback', 'generator_frame',
                   'coroutine_frame', 'exception_context')


@pytest.mark.parametrize('kind', ['extra_local_key', 'exec_mapping', 'global_dict_key',
                                 'proxy_locals_key'])
def test_guard_frame_mapping_edges(monkeypatch, kind):
    if guard_unavailable():
        return
    import functools
    import gc
    import sys

    from tests.fakes.frozen_package import _FRAME_LOCALS_PROXY
    from trusted_router.storage_errors import transient_store_error_types

    held = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    held()
    owner = None
    calls = []
    namespace = {'__name__': 'detached_frame_witness', '__builtins__': {}}
    if kind == 'global_dict_key':
        root = {held: None}
    elif kind == 'exec_mapping':
        class Locals(dict):
            def values(self):
                calls.append('values')
                return ()

            def keys(self):
                calls.append('keys')
                return ()

            def items(self):
                calls.append('items')
                return ()

            def __iter__(self):
                calls.append('__iter__')
                return iter(())

        localns = Locals(held=held)
        captured = []
        namespace.update(__builtins__={'exec': exec}, capture=captured.append, sys=sys)
        exec(compile('def generate(namespace):\n'
                     ' exec("capture(sys._getframe())", '
                     '{"__builtins__": {}, "capture": capture, "sys": sys}, namespace)\n'
                     ' namespace = None\n yield\n', '<exec-mapping-witness>', 'exec'), namespace)
        owner = namespace['generate'](localns)
        next(owner)
        root = captured[0]
        assert root.f_locals is localns
        assert root.f_back is owner.gi_frame and root.f_back.f_back is None
        assert owner.gi_frame.f_locals['namespace'] is None
        assert any(value is localns for value in gc.get_referents(root))
    else:
        exec(compile('def generate():\n yield\n', '<extra-local-witness>', 'exec'), namespace)
        owner = namespace['generate']()
        next(owner)
        root = owner.gi_frame
        assert root.f_back is None
        root.f_locals[held] = None
        if _FRAME_LOCALS_PROXY is None:
            assert type(root.f_locals) is dict
            assert any(key is held for key in dict.keys(root.f_locals))
        else:
            assert type(root.f_locals) is _FRAME_LOCALS_PROXY
            assert any(type(value) is dict and held in value for value in gc.get_referents(root))
        if kind == 'proxy_locals_key':
            # Isolate the supplement from the redundant extra-locals GC edge.
            # CPython already omits fast locals on some frames; this controlled
            # omission proves native proxy keys independently remain traversed.
            native_referents = gc.get_referents
            monkeypatch.setattr(gc, 'get_referents', lambda value: (
                [] if value is root else native_referents(value)))
    frozen = module('storage_errors')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(frozen, 'review_mapping_root',
                          (root, owner) if kind != 'proxy_locals_key' else root, raising=False)
            reason = ('opaque frame' if kind == 'proxy_locals_key' and sys.version_info < (3, 13)
                      else 'live reference')
            with pytest.raises(AssertionError, match=reason):
                with execution_guard():
                    pytest.fail('undetected frame mapping reached the frozen leg')
        assert calls == [], 'audit executed a user-container protocol method'
        assert held.cache_info().currsize == 1
        assert held.cache_info().misses == 1
        assert held.cache_info().hits == 0
    finally:
        if owner is not None:
            owner.close()


@pytest.mark.parametrize('kind', FRAME_WITNESSES)
def test_guard_held_frame_cache(monkeypatch, kind):
    if guard_unavailable():
        return
    import functools

    from trusted_router.storage_errors import transient_store_error_types

    # Independent of the module's cache: normalization cannot erase this edge.
    held = functools.lru_cache(maxsize=2)(transient_store_error_types.__wrapped__)
    held()
    generator = coroutine = None
    if kind == 'active_frame':
        root = inspect.currentframe()
        frame = root
    elif kind in ('exception_traceback', 'exception_context'):
        try:
            raise ValueError('held traceback')
        except ValueError as exc:
            root = exc
        frame = root.__traceback__.tb_frame
        if kind == 'exception_context':
            outer = RuntimeError('context owns the held traceback')
            outer.__context__ = root
            root = outer
            assert root.__traceback__ is None
    elif kind == 'generator_frame':
        def suspended(callback):
            held = callback
            yield
            return held
        generator = suspended(held)
        next(generator)
        frame = root = generator.gi_frame
    else:
        async def suspended(callback):
            held = callback
            return held
        coroutine = suspended(held)
        frame = root = coroutine.cr_frame
    assert frame is not None
    assert held.cache_info().misses == 1
    assert held.cache_info().hits == 0
    frozen = module('storage_errors')
    try:
        # Only the frozen module is a root; the cache/frame are not extra roots.
        with monkeypatch.context() as patch:
            patch.setattr(frozen, 'review_frame_root', root, raising=False)
            reason = 'opaque frame' if sys.version_info < (3, 13) else 'live reference'
            with pytest.raises(AssertionError, match=reason):
                with execution_guard():
                    pytest.fail('undetected held frame reached the frozen leg')
        assert held.cache_info().currsize == 1
        assert held.cache_info().hits == 0
    finally:
        if generator is not None:
            generator.close()
        if coroutine is not None:
            coroutine.close()
        # Break the active-frame local cycle without clearing a running frame.
        del root, frame


@pytest.mark.parametrize('kind', ['frame', 'traceback', 'generator', 'coroutine', 'async_generator'])
def test_reference_walk_native_frames(kind):
    from tests.fakes.frozen_package import _references

    # Synthetic namespaces avoid pytest globals/back-stack alternate paths.
    # The marker is owned only by frame locals after construction; frame/code
    # references to a marker in constants or globals cannot mask missing locals.
    namespace = {'__name__': 'frame_witness', 'sys': sys}
    exec(compile('def finished(held): return sys._getframe()\n'
                 'def capture(held):\n frame = finished(held)\n held = None\n yield frame\n frame = None\n yield\n'
                 'def generate(held):\n yield\n return held\n'
                 'async def coro(held):\n return held\n'
                 'async def agen(held):\n yield held\n',
                 '<frame-witness>', 'exec'), namespace)
    marker = object()
    close = None
    owners = []
    if kind in ('frame', 'traceback'):
        from types import TracebackType
        owner = namespace['capture'](marker)
        frame = next(owner)
        next(owner)  # The owner must not retain an alternate path to the finished frame.
        close = owner.close
        owners = [owner]
        root = frame if kind == 'frame' else TracebackType(None, frame, -1, 1)
    elif kind == 'generator':
        owner = namespace['generate'](marker)
        next(owner)
        close = owner.close
        root = owner
    elif kind == 'coroutine':
        root = namespace['coro'](marker)
        close = root.close
    elif kind == 'async_generator':
        root = namespace['agen'](marker)
    try:
        reached = list(_references([*owners, root], namespaces=(ALIAS, 'tests.fakes.spanner')))
        assert any(value is marker for value in reached)
    finally:
        if close is not None:
            close()


@pytest.mark.parametrize('scope', ['cell', 'free'])
def test_reference_walk_frame_cells(scope):
    from tests.fakes.frozen_package import _references

    # No retained nested function or caller frame can supply an alternate edge.
    # The marker lives only in the detached generator frame's cell/free slot.
    namespace = {'__name__': 'frame_cell_witness', '__builtins__': {}}
    exec(compile('def cell(held):\n'
                 ' def capture(): return held\n'
                 ' del capture\n yield\n'
                 'def free(held):\n'
                 ' def generate():\n  yield\n  return held\n'
                 ' return generate()\n', '<frame-cell-witness>', 'exec'), namespace)
    marker = object()
    owner = namespace[scope](marker)
    next(owner)
    root = owner.gi_frame
    assert root.f_back is None
    assert 'held' in (root.f_code.co_cellvars if scope == 'cell' else root.f_code.co_freevars)
    try:
        reached = list(_references([owner, root], namespaces=(ALIAS, 'tests.fakes.spanner')))
        assert any(value is marker for value in reached)
    finally:
        owner.close()


@pytest.mark.parametrize('explicit', [False, True])
def test_reference_walk_frame_registry_boundary(monkeypatch, explicit):
    import sys
    from types import ModuleType

    from tests.fakes.frozen_package import _references

    external = ModuleType('frame_registry_witness')
    marker = object()
    captured = []
    external.__dict__.update(__builtins__={}, marker=marker,
                             capture=captured.append, sys=sys)
    monkeypatch.setitem(sys.modules, external.__name__, external)
    namespace = {'__name__': 'detached_registry_frame', '__builtins__': {'exec': exec}}
    exec(compile('def generate(namespace):\n'
                 ' exec("capture(sys._getframe())", namespace)\n'
                 ' namespace = None\n yield\n', '<frame-registry-witness>', 'exec'), namespace)
    owner = namespace['generate'](external.__dict__)
    next(owner)
    root = captured[0]
    assert root.f_locals is root.f_globals is external.__dict__
    assert root.f_back is owner.gi_frame and root.f_back.f_back is None
    assert owner.gi_frame.f_locals['namespace'] is None
    try:
        reached = list(_references(
            [owner, root, external] if explicit else [owner, root], namespaces=(ALIAS,)))
        assert any(value is marker for value in reached) is explicit
    finally:
        owner.close()


@pytest.mark.parametrize('inspection', ['references', 'guard'])
@pytest.mark.parametrize('include_owner', [False, True])
def test_guard_frame_colliding_key(inspection, include_owner):
    if inspection == 'guard' and guard_unavailable():
        return
    from tests.fakes.frozen_package import _references

    calls = []

    class Key:
        def __hash__(self):
            calls.append('hash')
            return hash('held')

        def __eq__(self, other):
            from trusted_router.money import microdollars_to_float
            calls.append(microdollars_to_float(1_000_000))
            return False

    namespace = {'__name__': 'detached_frame_witness', '__builtins__': {}}
    exec('def generate():\n yield\n held = 42\n yield\n', namespace)
    owner = namespace['generate']()
    next(owner)
    root = owner.gi_frame
    key = Key()
    root.f_locals[key] = None
    next(owner)
    calls.clear()
    roots = [owner, root] if include_owner else [root]
    expected = (pytest.raises(AssertionError, match='opaque frame.*no safe native locals')
                if not include_owner and sys.version_info < (3, 13) else nullcontext())
    try:
        with expected:
            if inspection == 'references':
                reached = list(_references(roots, namespaces=(ALIAS,)))
                assert any(value is key for value in reached), 'lost an existing locals-dict key'
                assert any(type(value) is int and value == 42 for value in reached), 'lost fast local'
            else:
                with execution_guard(*roots):
                    pass
    finally:
        assert calls == [], 'frame inspection invoked a user key protocol / live money code'
        owner.close()


@pytest.mark.parametrize('root', ['scalar', 123456, 1.25, b'scalar', True, None],
                         ids=['str', 'int', 'float', 'bytes', 'bool', 'none'])
def test_reference_scalar_fast_path_retains_gc_edges(monkeypatch, root):
    import gc

    from tests.fakes.frozen_package import _owner, _references

    marker = object()
    native_referents = gc.get_referents
    # Even these exact scalars must keep every native edge. This synthetic
    # edge catches accidentally moving the fast path ahead of tp_traverse.
    monkeypatch.setattr(gc, 'get_referents', lambda value: (
        [marker] if value is root else native_referents(value)))
    reached = list(_references([root]))
    assert any(value is root for value in reached)
    assert any(value is marker for value in reached)
    assert _owner(root) == 'builtins'


@pytest.mark.parametrize('base, value', [(str, 'scalar'), (int, 123456),
                                       (float, 1.25), (bytes, b'scalar')],
                         ids=['str', 'int', 'float', 'bytes'])
def test_reference_scalar_subclasses_keep_metadata_and_edges(base, value):
    from tests.fakes.frozen_package import _owner, _references

    label = ALIAS + '.scalar_witness'
    scalar = type('Scalar', (base,), {'__module__': label})(value)
    marker = object()
    scalar.held = marker
    assert _owner(scalar) == label
    assert any(value is marker for value in _references([scalar]))


@pytest.mark.parametrize('inspection', ['references', 'guard'])
@pytest.mark.parametrize('owned', [False, True])
def test_guard_frame_trace_code(inspection, owned):
    if inspection == 'guard' and guard_unavailable():
        return
    import gc

    from tests.fakes.frozen_package import _references

    namespace = {'__name__': 'detached_trace_witness', '__builtins__': {}, 'sys': sys}
    exec('def finished(held): return sys._getframe()\n'
         'def generate(held):\n yield\n return held\n'
         'def capture(held):\n frame = finished(held)\n held = None\n yield frame\n', namespace)
    marker = object()
    owner = namespace['capture' if owned else 'generate'](marker)
    yielded = next(owner)
    root = yielded if owned else owner.gi_frame
    root.f_trace = root.f_code
    if sys.version_info < (3, 13):
        assert sum(value is root.f_code for value in gc.get_referents(root)) == (2 if owned else 1)
    roots = [owner, root] if owned else [root]
    expected = (pytest.raises(AssertionError, match='opaque frame')
                if not owned and sys.version_info < (3, 13) else nullcontext())
    try:
        with expected:
            if inspection == 'references':
                assert any(value is marker for value in list(_references(roots, namespaces=(ALIAS,))))
            else:
                with execution_guard(*roots):
                    pass
    finally:
        root.f_trace = None
        owner.close()


@pytest.mark.parametrize('filename', ['<string>', '<ordinary-generator>'])
def test_guard_resumed_owner_never_materializes_locals(filename):
    from tests.fakes.frozen_package import _UNSUPPORTED_INTERPRETER

    calls = []
    class Key:
        def __hash__(self):
            calls.append('hash')
            return hash('self')

        def __eq__(self, other):
            from trusted_router.money import microdollars_to_float
            calls.append(microdollars_to_float(1_000_000))
            return False

    frozen = module('storage_errors')
    namespace = {'__name__': frozen.__name__, '__builtins__': {}}
    exec(compile('def generate():\n yield\n self=42\n yield\n yield\n', filename, 'exec'), namespace)
    owner = namespace['generate']()
    next(owner)
    owner.gi_frame.f_locals[Key()] = None
    next(owner)
    calls.clear()
    entered = False
    expected = (pytest.raises(AssertionError, match=re.escape(_UNSUPPORTED_INTERPRETER))
                if sys.version_info < (3, 12) else nullcontext())
    try:
        with expected:
            with execution_guard(owner) as seen:
                entered = True
                next(owner)  # The vulnerable trampoline is entered HERE.
                assert any(row[1] == 'generate' for row in seen)
        assert entered is (sys.version_info >= (3, 12))
        assert calls == []
    finally:
        owner.close()


def test_guard_unsupported_interpreter_is_explicit(monkeypatch):
    import threading

    from tests.fakes import frozen_package

    previous, previous_thread = sys.getprofile(), threading.getprofile()
    with monkeypatch.context() as patch:
        patch.setattr(sys, 'version_info', (3, 11, 15))
        with pytest.raises(AssertionError) as error:
            with execution_guard():
                pytest.fail('unsupported interpreter entered the frozen leg')
        assert str(error.value) == frozen_package._UNSUPPORTED_INTERPRETER
    assert sys.getprofile() is previous
    assert threading.getprofile() is previous_thread


def test_guard_running_exec_locals_cache():
    # A fresh process isolates the running exec from pytest's frame graph.
    import subprocess
    import textwrap

    script = textwrap.dedent("""
        import functools, sys
        from tests.fakes.frozen_package import execution_guard, _UNSUPPORTED_INTERPRETER
        from trusted_router.money import microdollars_to_float
        held = functools.lru_cache(maxsize=1)(microdollars_to_float)
        assert held(1_000_000) == 1.0
        namespace = {'__name__': 'detached_exec_witness', '__builtins__': {},
                     'sys': sys, 'execution_guard': execution_guard}
        localns = {'held': held}
        try:
            exec('with execution_guard(sys._getframe()):\\n result=held(1_000_000)\\n', namespace, localns)
        except AssertionError as error:
            reason = str(error)
        else:
            raise AssertionError('running exec frame consumed a hidden warmed live cache')
        expected = (_UNSUPPORTED_INTERPRETER if sys.version_info < (3, 12) else
                    'opaque frame' if sys.version_info < (3, 13) else
                    'live reference in frozen namespace: trusted_router.money:microdollars_to_float')
        assert expected in reason, reason
        assert 'result' not in localns
        assert held.cache_info().hits == 0
    """)
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr


def test_reference_running_exec_has_only_plain_locals():
    import _thread
    import gc
    import threading

    from tests.fakes.frozen_package import _references

    marker = object()
    found, errors = [], []
    done = threading.Event()
    def inspect(frame):
        try:
            assert frame.f_back is None
            assert not gc.get_referents(frame)
            if sys.version_info < (3, 13):
                with pytest.raises(AssertionError, match='opaque frame'):
                    list(_references([frame], namespaces=(ALIAS,)))
            else:
                assert type(frame.f_locals) is dict
                found.extend(_references([frame], namespaces=(ALIAS,)))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    # Native exec is the thread target, so no Python caller holds the mapping.
    _thread.start_new_thread(exec, ('inspect(sys._getframe())',
        {'__name__': 'detached_running_exec', '__builtins__': {}, 'sys': sys, 'inspect': inspect},
        {'held': marker}))
    assert done.wait(10)
    assert not errors, errors
    if sys.version_info >= (3, 13):
        assert any(value is marker for value in found)


def test_guard_generated_inventory_uses_code_identity():
    if guard_unavailable():
        return
    cls = module('storage_models').CreditAccount
    with execution_guard() as seen:
        cls(workspace_id='generated-inventory-witness')
    assert any(row[0] == 'trusted_router.storage_models' and row[1] == 'CreditAccount.__init__'
               and row[2] == cls.__init__.__code__.co_firstlineno for row in seen)


@pytest.mark.parametrize('resume', ['next', 'throw'])
def test_guard_audits_resumed_python_event(resume):
    if guard_unavailable():
        return
    frozen = module('storage_errors')
    namespace = {'__name__': frozen.__name__, '__builtins__': {'ValueError': ValueError}}
    exec('def generate():\n try:\n  yield\n  yield\n except ValueError:\n  yield\n', namespace)
    owner = namespace['generate']()
    next(owner)
    try:
        with pytest.raises(AssertionError, match='live callable.*trusted_router.storage_errors:generate'):
            with execution_guard(owner):
                # No nested Python calls in the resumed body can mask a missing
                # resume/throw event. Restore provenance before the final audit.
                namespace['__name__'] = 'trusted_router.storage_errors'
                try:
                    next(owner) if resume == 'next' else owner.throw(ValueError())
                finally:
                    namespace['__name__'] = frozen.__name__
    finally:
        owner.close()


def test_guard_monitoring_slot_lifecycle():
    import _thread
    import threading

    if guard_unavailable():
        return
    if sys.version_info >= (3, 13):
        # These versions retain the separately tested all-thread profile path.
        with execution_guard():
            assert sys.getprofile() is not None
        return
    monitoring = sys.monitoring
    original = _thread.start_new_thread
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    monitoring.use_tool_id(4, 'occupied witness')
    try:
        with pytest.raises(ValueError, match='already in use'):
            with execution_guard():
                pytest.fail('occupied monitoring slot was replaced')
        assert monitoring.get_tool(4) == 'occupied witness'
        assert _thread.start_new_thread is original
    finally:
        monitoring.free_tool_id(4)
    for fail in (False, True):
        with pytest.raises(ValueError, match='body witness') if fail else nullcontext():
            with execution_guard():
                assert monitoring.get_events(4) != 0
                if fail:
                    raise ValueError('body witness')
        assert monitoring.get_tool(4) is None
        assert monitoring.get_events(4) == 0
        for event in (monitoring.events.PY_START, monitoring.events.PY_RESUME,
                      monitoring.events.PY_THROW, monitoring.events.CALL):
            assert monitoring.register_callback(4, event, None) is None
        assert _thread.start_new_thread is original
        assert sys.getprofile() is previous
        assert threading.getprofile() is previous_thread



def test_guard_resumed_module_file_property_is_not_called():
    from types import ModuleType

    from tests.fakes.frozen_package import _UNSUPPORTED_INTERPRETER

    calls = []
    class HostileModule(ModuleType):
        @property
        def __file__(self):
            from trusted_router.money import microdollars_to_float
            calls.append(microdollars_to_float(1_000_000))
            return ModuleType.__dict__['__dict__'].__get__(self)['__file__']

    frozen = module('storage_errors')
    namespace = {'__name__': frozen.__name__, '__builtins__': {}}
    exec('def generate():\n yield\n yield\n', namespace)
    owner = namespace['generate']()
    next(owner)
    frozen.__class__ = HostileModule
    entered = False
    expected = (pytest.raises(AssertionError, match=re.escape(_UNSUPPORTED_INTERPRETER))
                if sys.version_info < (3, 12) else nullcontext())
    try:
        with expected:
            with execution_guard(owner) as seen:
                entered = True
                next(owner)
                assert any(row == ('trusted_router.storage_errors', 'generate', 1,
                                   'src/trusted_router/storage_errors.py',
                                   PINS['src/trusted_router/storage_errors.py']) for row in seen)
        assert entered is (sys.version_info >= (3, 12))
        assert calls == []
    finally:
        frozen.__class__ = ModuleType
        owner.close()


@pytest.mark.parametrize('field', [
    'globals_key', 'globals_value', 'module_key', 'file_key', 'file_value',
    'filename', 'qualname', 'builtin_owner', 'module_owner', 'invalid_builtin_owner',
    'native_builtin_owner', 'builtin_bound_class', 'static_builtin_owner',
])
def test_guard_event_metadata_uses_native_protocols(field):
    from types import ModuleType

    from tests.fakes import frozen_package

    if guard_unavailable():
        return
    calls = []
    test_code = sys._getframe().f_code

    def record(protocol):
        # One helper frame and one protocol frame separate us from its caller.
        # Keep code identity; do not hash the hostile co_filename to attribute it.
        caller = sys._getframe(2)
        calls.append((protocol, caller.f_code, caller.f_lineno))

    class Label(str):
        def __hash__(self):
            record('hash')
            return str.__hash__(self)
        def __eq__(self, other):
            record('eq')
            return str.__eq__(self, other)
        def __str__(self):
            record('str')
            return str.__str__(self)
        def __bool__(self):
            record('bool')
            return True
        def startswith(self, *args):
            record('startswith')
            return str.startswith(self, *args)
        def replace(self, *args):
            record('replace')
            return str.replace(self, *args)
        def __fspath__(self):
            record('fspath')
            return str.__str__(self)

    class HostileModule(ModuleType):
        @property
        def __name__(self):
            record('module name')
            return 'trusted_router.money'

    class InvalidOwner:
        def __bool__(self):
            record('invalid bool')
            return True
        @property
        def __name__(self):
            record('invalid name')
            return 'trusted_router.money'

    class Meta(type):
        def __getattribute__(self, name):
            if name == '__qualname__':
                record('builtin class qualname')
            return type.__getattribute__(self, name)

    class Carrier(list, metaclass=Meta):
        pass

    frozen = module('storage_errors')
    name, source = frozen.__name__, frozen.__file__
    namespace = {'__name__': name, '__builtins__': {}}
    exec('def generate():\n yield\n yield\n', namespace)
    function = namespace['generate']
    if field in ('filename', 'qualname'):
        function.__code__ = function.__code__.replace(**{
            'co_' + field: Label('<string>' if field == 'filename' else 'generate')})
    owner = function()
    next(owner)
    builtin = (str.maketrans if field == 'static_builtin_owner' else
               re.compile('').match if field == 'native_builtin_owner' else
               Carrier().append if field == 'builtin_bound_class' else [].append)
    previous_module = builtin.__module__
    builtin.__module__ = Label(name) if field in ('builtin_owner', 'native_builtin_owner', 'builtin_bound_class', 'static_builtin_owner') else (
        HostileModule(name) if field == 'module_owner' else InvalidOwner())
    key = None
    try:
        with execution_guard(owner) as seen:
            # Install only for the resumed event, after preflight. Restore before
            # postflight, so this witness tests the callback itself.
            if field == 'globals_key':
                namespace.pop('__name__')
                key = Label('__name__')
                namespace[key] = name
            elif field == 'globals_value':
                namespace['__name__'] = Label(name)
            elif field == 'module_key':
                sys.modules.pop(name)
                key = Label(name)
                sys.modules[key] = frozen
            elif field == 'file_key':
                frozen.__dict__.pop('__file__')
                key = Label('__file__')
                frozen.__dict__[key] = source
            elif field == 'file_value':
                frozen.__dict__['__file__'] = Label(source)
            calls.clear()
            try:
                if field in ('builtin_owner', 'module_owner', 'invalid_builtin_owner', 'native_builtin_owner', 'builtin_bound_class', 'static_builtin_owner'):
                    builtin({} if field == 'static_builtin_owner' else '' if field == 'native_builtin_owner' else None)
                    qualname = 'Pattern.match' if field == 'native_builtin_owner' else 'list.append'
                    if field == 'builtin_bound_class':
                        qualname = type.__dict__['__qualname__'].__get__(Carrier) + '.append'
                    elif field == 'static_builtin_owner':
                        qualname = 'str.maketrans'
                    assert any(row[1] == qualname for row in seen) is (field != 'invalid_builtin_owner')
                else:
                    next(owner)
                    assert any(row[:4] == ('trusted_router.storage_errors', 'generate', 1,
                                          'src/trusted_router/storage_errors.py') for row in seen)
                # A C tracer can hash co_filename with the resumed owner (or
                # this test) as its immediate Python caller. Attribute every
                # call before excluding only those filename hash/eq operations.
                guard_calls = [call for call in calls
                               if str.__eq__(call[1].co_filename, frozen_package.__file__) is True]
                assert guard_calls == []
                assert all(field == 'filename' and protocol in ('hash', 'eq')
                           and (code is function.__code__ or code is test_code)
                           for protocol, code, _ in calls), calls
            finally:
                if field == 'globals_key':
                    namespace.pop(key)
                    namespace['__name__'] = name
                elif field == 'globals_value':
                    namespace['__name__'] = name
                elif field == 'module_key':
                    sys.modules.pop(key)
                    sys.modules[name] = frozen
                elif field == 'file_key':
                    frozen.__dict__.pop(key)
                    frozen.__dict__['__file__'] = source
                elif field == 'file_value':
                    frozen.__dict__['__file__'] = source
    finally:
        builtin.__module__ = previous_module
        owner.close()



def test_guard_worker_admission_does_not_hash_code_constants():
    import _thread
    import threading

    if guard_unavailable():
        return
    calls = []
    class Constant:
        def __hash__(self):
            calls.append('hash')
            return 0
        def __eq__(self, other):
            calls.append('eq')
            return False

    ran = threading.Event()
    namespace = {'__name__': 'external_worker_witness', '__builtins__': {},
                 'starter': _thread.start_new_thread, 'target': ran.set}
    exec('def create():\n starter(target, ())\n', namespace)
    function = namespace['create']
    function.__code__ = function.__code__.replace(co_consts=(*function.__code__.co_consts, Constant()))
    with pytest.raises(AssertionError, match='unwrapped raw worker'):
        with execution_guard():
            function()
    assert not ran.is_set()
    assert calls == []
