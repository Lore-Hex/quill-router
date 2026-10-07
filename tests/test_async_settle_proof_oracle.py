"""f83bbaac route → finalize → insert → drain → retention differential.

Frozen files compile under their own paths, never under live coverage paths.
"""
# ruff: noqa: F811, F401, S102 - shared fixtures and pinned executable oracle
from __future__ import annotations

import copy
import datetime as dt
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
    settings = module('config').Settings(**cfg.model_dump())
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
                with execution_guard() as setup_seen:
                    store, db, settings, client = frozen_environment(patch, cfg, body)
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
            with execution_guard() if frozen else nullcontext(set()) as seen:
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
                with execution_guard() as setup_seen:
                    _, db, _, client = frozen_environment(patch, cfg, body)
                inventory(setup_seen)
                restore(db, initial)
            else:
                db = env[1]
                client = _client(cfg)
            client.app.state.async_settle = env[2]
            with execution_guard() if frozen else nullcontext(set()) as seen:
                reply = client.post('/v1/internal/gateway/'+kind, json=body,
                                    headers={'X-TR-Settlement-Mode': 'async-v1'})
                client.close()
                results.append((reply.status_code, reply.content, save(db)))
            if frozen:
                inventory(seen)
    assert results[0] == results[1]
    assert results[1][0] == 200 and b'"reason":"disabled"' in results[1][1]
    assert results[1][2] == initial
