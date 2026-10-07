"""Actual HTTP bytes (including timing) and SQL traces against pinned main routes."""
# ruff: noqa: F811, S102 - imported fixtures and pinned-source execution
from __future__ import annotations

import ast
import copy
import datetime as dt
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import pytest

from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_async_settle_shadow import FIXTURE, NOW, signer, wire
from tests.test_async_settle_ticket import runtime, settings
from tests.test_billing_snapshot import endpoint_from_candidate
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401
)
from tests.test_settle_outbox_drain import _client
from trusted_router import acquisition, gateway_timing, storage_gcp_authorize
from trusted_router.catalog_data import Model
from trusted_router.detached_jws import canonical
from trusted_router.routes.internal import gateway
from trusted_router.services.async_settle_shadow import Runtime
from trusted_router.storage_models import generation_id_for_authorization


def frozen_routes():
    path = Path(__file__).parent / 'fakes/shadow_main_routes_internal_gateway.py.txt'
    tree = ast.parse(path.read_text())
    namespace = dict(vars(gateway))
    # All entry, registration and money functions come from the pinned source;
    # the shared injected catalog/clock/backend ports stay identical in both legs.
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def pin_clocks(monkeypatch):
    from trusted_router import storage_gcp_counter_dml, storage_gcp_settle_outbox, storage_models
    fixed = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    original = dt.datetime
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, original)
    class Clock(original, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(dt, 'datetime', Clock)
    for module in (storage_gcp_counter_dml, storage_gcp_settle_outbox):
        monkeypatch.setattr(module, 'datetime', Clock)
    for module in (storage_models, storage_gcp_authorize):
        monkeypatch.setattr(module, 'utcnow', lambda: fixed)
    monkeypatch.setattr(time, 'time', lambda: NOW)
    monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=42))
    monkeypatch.setattr(gateway, 'perf_counter', lambda: 1.)
    monkeypatch.setattr(gateway_timing, 'perf_counter', lambda: 1.)


def transcripts(monkeypatch, store, db, cfg, path, payload, headers, signer_present=False):
    pin_clocks(monkeypatch)
    frozen = frozen_routes()
    saved = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
    outputs = []
    for old in (True, False):
        for key, value in saved.items():
            setattr(db, key, copy.deepcopy(value))
        store._credit_shard_counts.invalidate(payload.get('workspace_id', 'ws-rpc'))
        with monkeypatch.context() as patch:
            patch.setattr(acquisition, '_usage_check_after', OrderedDict())
            patch.setattr(storage_gcp_authorize, '_OUTBOX_AVAILABILITY_CACHE', {})
            patch.setattr(gateway, '_BROADCAST_EMPTY_CACHE', OrderedDict())
            frozen['_BROADCAST_EMPTY_CACHE'] = OrderedDict()
            if old:
                patch.setattr(gateway, 'register', frozen['register'])
            client = _client(cfg)
            if signer_present:
                client.app.state.async_settle = runtime()
            # Keep a runtime installed even with the set empty: the HTTP gate
            # must prevent every observer, read and comparator invocation.
            shadow = Runtime(cfg, runtime())
            shadow.signer = signer()
            def unexpected(*args, **kwargs):
                pytest.fail('flag-off HTTP request reached shadow')
            shadow.submit = shadow.authorize = unexpected
            client.app.state.async_settle_shadow = shadow
            raw_calls = []
            def record(cls, method, raw_calls=raw_calls):
                original = getattr(cls, method)
                def wrapped(self, *args, **kwargs):
                    raw_calls.append((cls.__name__, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
                    return original(self, *args, **kwargs)
                patch.setattr(cls, method, wrapped)
            for method in ('execute_update', 'execute_sql', 'batch_update'):
                record(_FakeTransaction, method)
            record(_FakeSnapshot, 'execute_sql')
            responses = []
            # Exercise a real fresh request and durable idempotent replay.
            for _ in range(2):
                response = client.post(path, json=payload, headers=headers)
                assert response.status_code == 200, response.text
                responses.append((response.status_code, list(response.headers.multi_items()), response.content))
            state = {k: copy.deepcopy(v) for k, v in vars(db).items() if isinstance(v, (dict, list, set, int))}
            outputs.append((responses, raw_calls, state))
            shadow.executor.shutdown()
            client.close()
    assert outputs[0] == outputs[1]
    return outputs[1]


@pytest.mark.usefixtures('fixed_operation_catalog')
@pytest.mark.parametrize('header', ['none', 'exact', 'duplicate', 'bad'])
@pytest.mark.parametrize('eligible', [True, False])
@pytest.mark.parametrize('admission', [False, True])
@pytest.mark.parametrize('optin', ['', 'nonmember'])
@pytest.mark.parametrize('signer_present', [False, True])
def test_flag_off_authorize_http_identity(monkeypatch, header, eligible, admission, optin, signer_present):
    store, db, key = _seed_typed_gateway_store()
    cfg = settings(async_settle_enabled=admission, async_settle_shadow_workspaces=optin)
    body = _body(key.hash)
    body.route_type = 'chat.completions' if eligible else None
    body.invocation_nonce = 'http-identity'
    headers = {'none': [], 'exact': [('X-TR-Settlement-Mode', 'async-v1')],
        'duplicate': [('X-TR-Settlement-Mode', 'async-v1')]*2,
        'bad': [('X-TR-Settlement-Mode', '!')]}[header]
    transcripts(monkeypatch, store, db, cfg, '/v1/internal/gateway/authorize', body.model_dump(), headers, signer_present)


@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('header', ['none', 'valid', 'invalid', 'oversize', 'duplicate'])
@pytest.mark.parametrize('admission', [False, True])
@pytest.mark.parametrize('optin', ['', 'nonmember'])
@pytest.mark.parametrize('signer_present', [False, True])
def test_flag_off_terminal_http_identity(env, monkeypatch, kind, header, admission, optin, signer_present):
    body, auth, _ = prepare(env, kind=kind)
    store, db, rt, cfg = env
    cfg.async_settle_enabled = admission
    cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = frozenset({optin}) if optin else frozenset()
    repair = json.loads(row_for(env, body).settle_body)
    stored = json.loads(db.gateway_authorizations[auth.id]['payload'])
    stored['invocation_nonce'] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]['payload'] = json.dumps(stored)
    endpoints = {c['endpoint_id']: endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(id=endpoint.model_id,
            name='http oracle', provider=endpoint.provider, context_length=1000000, prepaid_available=True))
    monkeypatch.setattr(gateway, 'endpoint_for_id', endpoints.get)
    envelope = copy.deepcopy(FIXTURE)
    envelope['billing_snapshot'], envelope['terminal'] = body['billing_snapshot'], body['terminal']
    claims = dict(authorization_id=auth.id, generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id, key_id=auth.key_hash, invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id, billing_authority='local', journal_region=rt.region,
        epoch=rt.epoch, route_type='chat.completions', streamed=False, settle_origin='typed', snapshot_version=1,
        snapshot_hash=body['terminal']['snapshot_hash'], async_eligible=False, iss='router-fixture',
        aud='router-shadow', iat=NOW, exp=NOW+172800)
    envelope['billing_shadow_binding'] = signer().sign(claims, NOW)
    envelope['payload_hash'] = hashlib.sha256(canonical(envelope['terminal'])).hexdigest()
    if header == 'valid':
        from trusted_router.async_settle_shadow_compare import Booking, Context, compare
        from trusted_router.schemas import GatewaySettleRequest
        ctx = Context(auth, GatewaySettleRequest(**repair), kind, body['terminal']['selected_endpoint'],
            rt.region, rt.epoch, NOW, Booking(2 if kind == 'settle' else 0,
                                            'settled' if kind == 'settle' else 'refunded', True))
        assert compare(wire(envelope), ctx, [signer().trusted]).classification == 'exact'
    headers = [] if header == 'none' else [('X-TR-Settlement-Shadow', {
        'valid': wire(envelope)[0], 'duplicate': wire(envelope)[0], 'invalid': '!', 'oversize': '!'*12289}[header])]
    if header == 'duplicate':
        headers *= 2
    outputs = transcripts(monkeypatch, store, db, cfg, '/v1/internal/gateway/'+kind, repair, headers, signer_present)
    assert json.loads(outputs[0][0][2])['data']['cost_microdollars'] == (2 if kind == 'settle' else 0)
