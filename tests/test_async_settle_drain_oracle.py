"""Frozen main ecb794597f32b0775594f8944f40f935b7f07278 vs dormant PR D.

Execute frozen drain + entire frozen outbox helpers on the real fake, including
actual applies. Compare response bytes, durable state, SQL bytes/params/types,
mutations and RPC counts. No replacement expected billing algorithm.
"""
# ruff: noqa: S102, F811, F401 - pinned source execution and shared fixtures
from __future__ import annotations

import ast
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
from tests.test_async_settle_handler import env, prepare, row_for
from tests.test_authorize_hold_time import _ast_sha256
from trusted_router import storage_gcp_settle_outbox as storage
from trusted_router.config import Settings
from trusted_router.services import async_settle
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services.settle_outbox_apply import ApplyOutcome

FROZEN = Path(__file__).parent/'fakes'
PINS = {'async_drain_async_settle_main.txt': 'b8a500732af75dde9b61b4086eb7d980f9852be59d67063237bf136f7d3a311d', 'async_drain_settle_outbox_drain_main.txt': 'bf9fefe601706f67dbf8bc5187efa1fd1e26198f33b19bf055ca03ccf952a43d', 'async_drain_storage_gcp_settle_outbox_main.txt': '16bbdc5919662fd3a38fe1159200848e2faed542ac2e4ad83cb76aef26cce529'}


def test_frozen_drain_ast_pins():
    for file, pin in PINS.items():
        assert _ast_sha256(ast.parse((FROZEN/file).read_text())) == pin


def namespace(name, module):
    result = dict(vars(module))
    path = FROZEN/f'async_drain_{name}_main.txt'
    exec(compile(path.read_text(), str(path), 'exec'), result)
    return result


@pytest.mark.parametrize('scenario', ['empty', 'apply', 'error', 'park', 'dead', 'budget', 'clamp'])
def test_flag_off_frozen_drain_sql_state_response(env, monkeypatch, scenario):
    store, db, _, cfg = env
    body, auth, _ = prepare(env)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    store.settle_outbox._async_fence = False
    clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(storage, '_iso_now', lambda: clock.isoformat().replace('+00:00', 'Z'))
    monkeypatch.setattr(storage.uuid, 'uuid4', lambda: SimpleNamespace(hex='fixed-oracle-owner'))
    from trusted_router import storage_gcp_authorize, storage_models
    monkeypatch.setattr(storage_models, 'utcnow', lambda: clock)
    monkeypatch.setattr(storage_gcp_authorize, 'utcnow', lambda: clock)
    if scenario != 'empty':
        row = row_for(env, body)
        row.async_version = None
        storage.SpannerSettleOutbox(db, store._param_types).enqueue(row)
    old_storage = namespace('storage_gcp_settle_outbox', storage)
    old_drain = namespace('settle_outbox_drain', drain)
    old_drain['SpannerSettleOutbox'] = old_storage['SpannerSettleOutbox']
    names = ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
             'generation_records', 'operational_analytics_outbox', 'analytics_outbox', 'reservation_idemp')
    saved = {n: copy.deepcopy(getattr(db, n)) for n in names}
    outputs = []
    for frozen in (True, False):
        for n, value in saved.items():
            setattr(db, n, copy.deepcopy(value))
        with monkeypatch.context() as patch:
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
            ticks = iter([0., 241.] if scenario == 'budget' else [0.] * 1000)
            monotonic = lambda ticks=ticks: next(ticks)  # noqa: E731
            patch.setattr(drain, '_monotonic', monotonic)
            old_drain['_monotonic'] = monotonic
            if scenario in ('error', 'park', 'dead'):
                outcome = dict(error=ApplyOutcome.ERROR, park=ApplyOutcome.PARK_TYPED_UNAVAILABLE,
                               dead=ApplyOutcome.INVALID_ROW)[scenario]
                apply = lambda row, outcome=outcome: outcome  # noqa: E731
                patch.setattr(drain, 'apply_frozen_settle', apply)
                old_drain['apply_frozen_settle'] = apply
            rpc = ('snapshot_execute_sql_calls', 'transaction_execute_sql_calls',
                   'transaction_execute_update_calls', 'transaction_batch_update_calls', 'commits')
            before = [getattr(db, n) for n in rpc]
            function = old_drain['drain_settle_outbox'] if frozen else drain.drain_settle_outbox
            result = function(999 if scenario == 'clamp' else 100,
                              **({} if frozen else {'settings': Settings(environment='test')}))
            outputs.append((json.dumps(result, separators=(',', ':')),
                {n: copy.deepcopy(getattr(db, n)) for n in names}, trace,
                [getattr(db, n)-v for n, v in zip(rpc, before, strict=True)]))
    assert outputs[0] == outputs[1]
    assert not any('settle_drain_control' in str(call) for call in outputs[1][2])
    if scenario == 'apply':
        assert outputs[1][0].find('settled_now') >= 0
        assert db.settle_outbox[(auth.id, 'settle')]['status'] == 'done'


def test_flag_off_runtime_frozen_no_health_rpc(env, monkeypatch):
    store, db, _, _ = env
    frozen = namespace('async_settle', async_settle)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('flag off issued a health read')
    from trusted_router import storage_gcp_async_admission as adapter
    monkeypatch.setattr(adapter, 'read_health', forbidden)
    monkeypatch.setattr(adapter, 'read_admission', lambda *args: async_settle.Admission(0, 2))
    for function in (frozen['load_runtime'], async_settle.load_runtime):
        rt = function(Settings(environment='test'), store)
        assert rt.signer is None
        assert rt.admission is not None and not rt.admission.eligible('ws', 0)
    assert calls == [] and db.snapshot_execute_sql_calls == 0
