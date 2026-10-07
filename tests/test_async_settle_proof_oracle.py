"""f83bbaac route → finalize → insert → drain → retention differential.

Frozen files compile under their own paths, never under live coverage paths.
"""
# ruff: noqa: F811, F401, S102 - shared fixtures and pinned executable oracle
from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes.spanner import _FakeBatch, _FakeSnapshot, _FakeTransaction
from tests.test_async_settle_handler import SUPPORTED, env, prepare
from tests.test_async_settle_proof import catalog, legacy_body, restore, save
from tests.test_authorize_hold_time import _ast_sha256
from tests.test_settle_outbox_drain import _client
from trusted_router import (
    acquisition,
    storage_codec,
    storage_gcp,
    storage_gcp_analytics_outbox,
    storage_gcp_authorize,
    storage_gcp_batch_dml,
    storage_gcp_codec,
    storage_gcp_counter_dml,
    storage_gcp_generation_records,
    storage_gcp_generations,
    storage_gcp_operational_analytics_outbox,
    storage_gcp_request_records,
    storage_gcp_settle_outbox,
    storage_gcp_trust,
    storage_models,
    storage_operational_analytics,
)
from trusted_router.routes import settlements
from trusted_router.routes.internal import gateway
from trusted_router.services import async_settle_handler, settle_outbox_apply, settle_outbox_drain

FROZEN = Path(__file__).parent/'fakes'
MODULES = dict(benchmark_model=storage_models, codec=storage_codec,
               gcp_codec=storage_gcp_codec,
               batch=storage_gcp_batch_dml,
               generation=storage_gcp_generation_records,
               requests=storage_gcp_request_records,
               activity_payload=storage_operational_analytics,
               activity=storage_gcp_operational_analytics_outbox,
               benchmark=storage_gcp_analytics_outbox,
               generations=storage_gcp_generations,
               counter=storage_gcp_counter_dml, trust=storage_gcp_trust, outbox=storage_gcp_settle_outbox,
               finalize=storage_gcp_authorize, apply=settle_outbox_apply,
               drain=settle_outbox_drain, handler=async_settle_handler, gateway=gateway, route=settlements, store=storage_gcp)
PINS = {'gateway': 'f4421795f19f8e3c4172016c1831214a0e6ff0f47080ad7167785dc6e478de91', 'route': '069adcb553d856424807e7638db6c20acf9499fe0de6d6de6efe2596de7b91d2', 'finalize': '2c215187a3c740d3e011d19d34bc0ba5ec381b9a3789abf6709c9cfec44e69b2', 'store': 'a3b82c02176a190ee8037ed355160a4f94cbd2b78ec2badba07ce151ca20a497', 'drain': 'dcd00080d137ddc6ff8fac9abd7274c38b1289d28c73171c167037f72c11aa79', 'apply': 'f531dc96b0a5e11c6b8e1d01c46b45bfd9c0a5cc0b549b4892f02cb8c366ca6b', 'handler': 'a62623d28e6c1ee446d705aa6d582c8bee33e9f6964d33f722b520ce97b528e5', 'outbox': '5528e2426fc0aa84a3f895b24418dc8807eeb394522b76a76a346a1cd49997ab', 'counter': 'd903c43f21ad67464238a4af0b415d0963a5aced79218f1911866edb2b58eda2'}
PINS.update({'trust': '4a35210bbe0173c81deb39d732dbd4d4715d7ae291ca8d76ca0199108bd1987c', 'benchmark_model': '14df737dbf22d6f2ea46362ee5bb65823db6ca08e6d1b5817f05af3513e94362', 'codec': '30691fb408355e3b73ac589ca4b41b5fe5208570bbcec72add5c3f5baded8561', 'gcp_codec': '1dc9bc29b7d9eb1f178439443e6bf0b409412837c99ddeca608731111a7c2d23', 'batch': '44d340db7a39a50ad0288057a642ee9b447f4a17a2ab0145d48732793342bf18', 'generation': 'b47b1d08c122e0d1ae533957fa5c6e012f6a71691f6e3ce09ee7f0b99c4c0f72', 'requests': '42234f8e8cc6923c1d5d9c5d12490504ada45d7b11510fabda1aed9ea1a11217', 'activity_payload': '3f283b95812ec9e04588bbcfdddfbc36c6b10f630b5735e55e66b9d78cce3cf9', 'activity': '2a11d38692afe901d8fac36b75536c423b441c449b4bedb2678fe4d565e52e7b', 'benchmark': '4b5e1e4cf8975910e4adcc4418a3456919267a70a1fc87754969af0ffe666faf', 'generations': '95cd2efe388bebdcf3071436c64e4bcdbfcc0cfad299cc175b6b788c514baf1a'})


def test_f83bbaac_provenance():
    assert set(PINS) == set(MODULES)
    for name, digest in PINS.items():
        assert _ast_sha256(ast.parse((FROZEN/f'async_proof_{name}_main.txt').read_text())) == digest


def install_frozen(patch, store):
    replacements = {}
    for name, module in MODULES.items():
        path = FROZEN/f'async_proof_{name}_main.txt'
        source = path.read_text()
        # Selected store/gateway functions inherit globals rather than imports.
        # Rebind their aliases too; otherwise a frozen body can call live SQL.
        namespace = {key: replacements.get(id(value), value)
                     for key, value in vars(module).items()}
        exec(compile(source, str(path), 'exec'), namespace)
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                target = storage_gcp.SpannerStore if name == 'store' else module
                original = getattr(target, node.name)
                frozen = namespace[node.name]
                replacements[id(original)] = frozen
                # The fixture constructed these composed stores before freezing.
                # Patch their class descriptors as well as future constructors.
                if name in {'activity', 'benchmark', 'generations', 'benchmark_model'} and isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            patch.setattr(original, member.name, vars(frozen)[member.name])
                patch.setattr(target, node.name, frozen)

    # Feature stores retain bound IO callbacks from fixture construction.
    # Replacing a class method alone cannot update an already-bound callback.
    io = store.generation_store._io
    frozen_io = replace(io, **{
        name: getattr(store, '_' + name) for name in (
            'read_entity', 'read_entity_tx', 'write_entity', 'write_entity_tx',
            'write_entity_batch', 'list_entities', 'delete_entities', 'delete_entities_tx')
    })
    for feature in vars(store).values():
        if getattr(feature, '_io', None) is io:
            patch.setattr(feature, '_io', frozen_io)


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
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(storage_models, 'utcnow', lambda: clock)
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
        restore(db, initial)
        with monkeypatch.context() as patch:
            if frozen:
                install_frozen(patch, env[0])
            # Full modules have their own imports; pin clocks after installation.
            patch.setattr(storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            patch.setattr(acquisition, '_usage_check_after', OrderedDict())
            store.settle_outbox = storage_gcp_settle_outbox.SpannerSettleOutbox(
                db, store._param_types, async_fence=cfg.async_settle_protection)
            client = _client(cfg)
            trace = []
            def record(cls, method, trace=trace, frozen=frozen):
                original = getattr(cls, method)
                def invoke(self, *args, **kwargs):
                    if frozen:
                        frame = inspect.currentframe().f_back
                        while frame is not None:
                            filename = frame.f_code.co_filename
                            if filename.startswith(str(FROZEN)) and filename.endswith('_main.txt'):
                                break
                            assert '/src/trusted_router/' not in filename, (
                                'live SQL/mutation producer on frozen path', filename,
                                frame.f_code.co_name)
                            frame = frame.f_back
                        assert frame is not None, 'missing frozen SQL/mutation producer'
                        del frame
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
            drained = settle_outbox_drain.drain_settle_outbox(10, settings=cfg)
            outputs.append((value, stage, drained, save(db), trace,
                            [getattr(db, n)-v for n, v in zip(rpc, before, strict=True)]))
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
                install_frozen(patch, env[0])
            client = _client(cfg)
            client.app.state.async_settle = env[2]
            reply = client.post('/v1/internal/gateway/'+kind, json=body,
                                headers={'X-TR-Settlement-Mode': 'async-v1'})
            results.append((reply.status_code, reply.content, save(env[1])))
    assert results[0] == results[1]
    assert results[1][0] == 200 and b'"reason":"disabled"' in results[1][1]
    assert results[1][2] == initial
