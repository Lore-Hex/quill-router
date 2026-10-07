"""f83bbaac route → finalize → insert → drain → retention differential.

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
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
from types import FunctionType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.fakes.frozen_package import PINS, execution_guard, fake_store, module
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


def test_f83bbaac_provenance():
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
    directory = os.environ.get('F83BBAAC_INVENTORY_DIR')
    if directory:
        path = Path(directory) / f'{os.getpid()}.json'
        previous = {tuple(row) for row in json.loads(path.read_text())} if path.exists() else set()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(previous | seen), indent=2) + '\n')


def frozen_runtime():
    # Run the same fixture factories with snapshot classes and literal key inputs.
    # In particular, never attach the live Runtime (or its admission callback).
    from tests import test_async_settle_ticket as fixture

    namespace = dict(fixture.runtime.__globals__)
    for name in ('Admission', 'AdmissionCache', 'DrainHealth', 'Runtime',
                 'TicketSigner', 'TrustedKey'):
        value = namespace[name]
        namespace[name] = getattr(module(value.__module__.removeprefix('trusted_router.')), name)
    namespace['signer'] = FunctionType(fixture.signer.__code__, namespace)
    return FunctionType(fixture.runtime.__code__, namespace)()


def test_guard_rejects_live_callback_even_after_return(env):
    _, auth, _ = prepare(env)
    with pytest.raises(AssertionError, match='trusted_router.storage_models:GatewayAuthorization.record_finalization'):
        with execution_guard():
            auth.record_finalization(success=True, actual_microdollars=2,
                                     selected_usage_type='Credits', generation=None)


@pytest.mark.parametrize('target', ['native', 'partner', 'unlisted', 'generated', 'worker'])
def test_guard_has_no_omitted_function_or_module_exemption(target):
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
    from trusted_router.storage_errors import transient_store_error_types

    transient_store_error_types()  # warm the C cache; no Python body on a hit
    with monkeypatch.context() as patch:
        patch.setattr(module('storage_errors'), 'transient_store_error_types', transient_store_error_types)
        with pytest.raises(AssertionError, match='live reference.*trusted_router.storage_errors:transient_store_error_types'):
            with execution_guard():
                module('storage_errors').transient_store_error_types()


@pytest.mark.parametrize('case', SUPPORTED, ids=lambda c: c['name'])
@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('mode', ['no_header_off', 'header_off', 'no_header_protected'])
@pytest.mark.parametrize('commit_path', ['inline', 'repair'])
def test_f83bbaac_complete_entry(env, monkeypatch, case, kind, mode, commit_path):
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
            with execution_guard(store, db, client) if frozen else nullcontext(set()) as seen:
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


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_f83bbaac_protected_header_rejection(env, monkeypatch, kind):
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
            with execution_guard(db, client) if frozen else nullcontext(set()) as seen:
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
    'external_callable_class', 'bounded_cache_result',
])
def test_guard_reviewer_references(monkeypatch, bridge):
    import dataclasses
    import functools

    from pydantic import create_model, field_validator

    from tests.fakes import spanner
    from trusted_router import storage_errors

    frozen = module('storage_errors')
    live = storage_errors.transient_store_error_types
    live()  # The attack must start with a genuinely warm C cache.
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
    elif bridge in {'cache_result', 'bounded_cache_result'}:
        @functools.lru_cache(maxsize=1 if bridge == 'bounded_cache_result' else None)
        def root():
            return importlib.import_module('trusted_router.storage_errors').transient_store_error_types
        root()  # Live callable is held in C cache state, not a closure/default.
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
    import functools
    from types import MappingProxyType

    from trusted_router.storage_errors import transient_store_error_types

    # A separate wrapper is essential: clearing the live module's own cache
    # would mask a traversal gap by turning the attack into a profiled cold call.
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
    import _thread
    import sys
    import threading
    import time

    namespace, name = api.split('.')
    owner = _thread if namespace == '_thread' else threading
    if not hasattr(owner, name):
        pytest.skip(f'{api} unavailable on Python {sys.version_info[:2]}')
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
    import _thread
    import threading

    start = _thread.start_new_thread
    ran = threading.Event()
    with pytest.raises(AssertionError, match='unwrapped raw worker'):
        with execution_guard():
            start(ran.set, ())
    assert not ran.is_set()


@pytest.mark.parametrize('kind', [
    'mapping_key', 'mapping_value', 'sequence', 'set', 'inherited_private_slot',
    'shadowed_slot', 'wrapped_descriptor', 'cached_property', 'getstate',
])
def test_reference_walk_python_state(kind):
    from collections.abc import Mapping, Sequence, Set
    from functools import cached_property

    from tests.fakes.frozen_package import _references

    marker = object()
    # Keep protocol-only values in external function globals, which the walker
    # deliberately cannot follow. A closure/instance dict would let a broken
    # container/state traversal pass by reaching the marker through another edge.
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
    assert any(value is marker for value in _references([root], namespaces=()))


@pytest.mark.parametrize('root_kind', ['namespace', 'module', 'logger'])
def test_guard_explicit_harness_root(root_kind):
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
    with pytest.raises(AssertionError, match='live callable'):
        with execution_guard():
            importlib.import_module('trusted_router.storage_errors').is_transient_store_error(ValueError())


def test_guard_preexisting_worker_cost_bridge(monkeypatch):
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

    forbidden = ('tests', 'frozen_f83bbaac')
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
         "assert not any(n == 'frozen_f83bbaac' or n.startswith('frozen_f83bbaac.') for n in sys.modules)"],
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
    import functools
    from typing import Annotated

    from tests.fakes.frozen_package import _references
    from trusted_router.routes.internal.lightning import Credit

    # This exact shared stdlib cache caused a second-case false positive after
    # the live HTTP app registered its Credit response schema.
    module('billing_snapshot')
    Annotated[Credit, 'f1-review-typing-cache']
    cache = next(value for value in _references([Annotated], namespaces=())
                 if isinstance(value, functools._lru_cache_wrapper)
                 and value.__qualname__ == 'Annotated._class_getitem_inner')
    assert cache.cache_info().currsize
    with execution_guard():
        assert cache.cache_info().currsize == 0
