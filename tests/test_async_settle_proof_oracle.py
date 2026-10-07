"""f83bbaac route → finalize → insert → drain → retention differential.

Frozen files compile under their own paths, never under live coverage paths.
"""
# ruff: noqa: F811, F401, S102 - shared fixtures and pinned executable oracle
from __future__ import annotations

import ast
import copy
import datetime as dt
from collections import OrderedDict
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
    storage_gcp,
    storage_gcp_authorize,
    storage_gcp_counter_dml,
    storage_gcp_settle_outbox,
    storage_models,
)
from trusted_router.routes import settlements
from trusted_router.routes.internal import gateway
from trusted_router.services import async_settle_handler, settle_outbox_apply, settle_outbox_drain

FROZEN = Path(__file__).parent/'fakes'
MODULES = dict(counter=storage_gcp_counter_dml, outbox=storage_gcp_settle_outbox,
               finalize=storage_gcp_authorize, apply=settle_outbox_apply,
               drain=settle_outbox_drain, handler=async_settle_handler, gateway=gateway, route=settlements, store=storage_gcp)
PINS = {'gateway': '943097a42bd04a3b239510ac31419b56f2ac01ae99ea933f1c56cc34a2154e0f', 'route': '069adcb553d856424807e7638db6c20acf9499fe0de6d6de6efe2596de7b91d2', 'finalize': '2c215187a3c740d3e011d19d34bc0ba5ec381b9a3789abf6709c9cfec44e69b2', 'store': '6e95ebc7182d03b5296ff978ec952c524c1fb404695ce60e6e8a19bd271e44d8', 'drain': 'dcd00080d137ddc6ff8fac9abd7274c38b1289d28c73171c167037f72c11aa79', 'apply': 'f531dc96b0a5e11c6b8e1d01c46b45bfd9c0a5cc0b549b4892f02cb8c366ca6b', 'handler': 'a62623d28e6c1ee446d705aa6d582c8bee33e9f6964d33f722b520ce97b528e5', 'outbox': '5528e2426fc0aa84a3f895b24418dc8807eeb394522b76a76a346a1cd49997ab', 'counter': 'd903c43f21ad67464238a4af0b415d0963a5aced79218f1911866edb2b58eda2'}


def test_f83bbaac_provenance():
    assert set(PINS) == set(MODULES)
    for name, digest in PINS.items():
        assert _ast_sha256(ast.parse((FROZEN/f'async_proof_{name}_main.txt').read_text())) == digest


def install_frozen(patch):
    for name, module in MODULES.items():
        path = FROZEN/f'async_proof_{name}_main.txt'
        source = path.read_text()
        namespace = dict(vars(module))
        exec(compile(source, str(path), 'exec'), namespace)
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                patch.setattr(storage_gcp.SpannerStore if name == 'store' else module,
                              node.name, namespace[node.name])


@pytest.mark.parametrize('case', SUPPORTED, ids=lambda c: c['name'])
@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('mode', ['no_header_off', 'header_off', 'no_header_protected'])
@pytest.mark.parametrize('commit_path', ['inline', 'repair'])
def test_f83bbaac_complete_entry(env, monkeypatch, case, kind, mode, commit_path):
    store, db, _, cfg = env
    body, auth, _ = prepare(env, case, kind=kind)
    catalog(monkeypatch, body)
    cfg.async_settle_enabled = False
    cfg.async_settle_protection = mode == 'no_header_protected'
    clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(storage_models, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_gcp_authorize, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_gcp_settle_outbox, '_iso_now', lambda: clock.isoformat().replace('+00:00', 'Z'))
    monkeypatch.setattr(storage_gcp_settle_outbox.uuid, 'uuid4', lambda: SimpleNamespace(hex='oracle-owner'))
    initial = save(db)
    outputs = []
    for frozen in (True, False):
        clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
        restore(db, initial)
        with monkeypatch.context() as patch:
            if frozen:
                install_frozen(patch)
            # Full modules have their own imports; pin clocks after installation.
            patch.setattr(storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            patch.setattr(acquisition, '_usage_check_after', OrderedDict())
            store.settle_outbox = storage_gcp_settle_outbox.SpannerSettleOutbox(
                db, store._param_types, async_fence=cfg.async_settle_protection)
            client = _client(cfg)
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
    cfg.async_settle_enabled = False
    initial = save(env[1])
    results = []
    for frozen in (True, False):
        with monkeypatch.context() as patch:
            if frozen:
                install_frozen(patch)
            client = _client(cfg)
            client.app.state.async_settle = env[2]
            reply = client.post('/v1/internal/gateway/'+kind, json=body,
                                headers={'X-TR-Settlement-Mode': 'async-v1'})
            results.append((reply.status_code, reply.content, save(env[1])))
    assert results[0] == results[1]
    assert results[1][0] == 200 and b'"reason":"disabled"' in results[1][1]
    assert results[1][2] == initial
