from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.fakes.spanner import _FakeTransaction, make_fake_store
from tests.test_async_settle_ticket import runtime, settings
from tests.test_billing_snapshot import CASES
from tests.test_settle_outbox_drain import (
    _client,
    _make_key,
    _seed_credit,
    _typed_credit,
    _typed_key,
)
from tests.test_settle_outbox_drain import (
    _outbox as _legacy_outbox,
)
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_ticket import verify_lookup_ticket, verify_ticket
from trusted_router.routes.settlements import status
from trusted_router.services import async_settle_handler as handler
from trusted_router.services.async_settle import snapshot_projection
from trusted_router.services.settle_outbox_drain import drain_settle_outbox
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_async_settle import ReservationNotOpen, enqueue

ROOT = Path(__file__).parent / 'fixtures/async_settlement'
WIRE = json.loads((ROOT / 'request_v1.json').read_text())
NOW = 1791244801


def _outbox(store):
    outbox = _legacy_outbox(store)
    outbox._async_fence = store.trust_settings.async_settle_protection
    return outbox


@pytest.fixture
def env(monkeypatch):
    store, db = make_fake_store(request_record_write_mode="typed",
                                operational_analytics_outbox_enabled=True,
                                generation_records_enabled=True)
    configure_store(store)
    rt = runtime()
    cfg = settings(settle_outbox_enabled=True)
    store.trust_settings = cfg
    store.settle_outbox._async_fence = True
    monkeypatch.setattr(handler.time, 'time', lambda: NOW)
    try:
        yield store, db, rt, cfg
    finally:
        configure_store(InMemoryStore())


def prepare(env, case=None, kind='settle'):
    store, db, rt, cfg = env
    snapshot = billing.parse_snapshot(json.dumps(case['snapshot'] if case else WIRE['billing_snapshot']))
    first = snapshot.candidates[0]
    _seed_credit(store, 'ws-v1', 10**15)
    key = _make_key(store, 'ws-v1', limit=10**15)
    _, auth = store.authorize_gateway_typed(
        workspace_id='ws-v1', key_hash=key.hash, estimate=case.get('reservation_estimate_micro', 1) if case else 3,
        has_credit_candidate=True, reservation_usage_type='Credits', model_id=first.model_id,
        provider=first.provider, requested_model_id=first.model_id,
        candidate_model_ids=[c.model_id for c in snapshot.candidates], region='us-central1',
        endpoint_id=first.endpoint_id, candidate_endpoint_ids=[c.endpoint_id for c in snapshot.candidates],
        idempotency_key=None, idempotency_fingerprint=None, expires_at='2099-01-01T00:00:00Z',
    )
    assert auth is not None
    # Authorize primitive does not create an invocation nonce itself.
    auth.invocation_nonce = 'nonce-v1'
    context = billing.Eligibility(**(case['context'] if case else {}))
    projection = snapshot_projection(authorization=auth, snapshot=snapshot, requested=context,
                                    runtime=rt, settings=cfg, now=NOW)
    body = copy.deepcopy(WIRE)
    body.update(billing_snapshot=snapshot.model_dump(mode='json'), settlement_ticket=projection['settlement_ticket'],
                raw_usage=copy.deepcopy(case['raw_usage'] if case else WIRE['raw_usage']), observed=context.model_dump())
    body['terminal'].update(authorization_id=auth.id, generation_id=projection.get('generation_id',
        __import__('trusted_router.storage_models', fromlist=['generation_id_for_authorization']).generation_id_for_authorization(auth.id)),
        key_id=key.hash, snapshot_hash=projection['billing_snapshot_hash'],
        selected_endpoint=case['selected_endpoint'] if case else first.endpoint_id,
        route_type=context.route_type, streamed=context.streamed, terminal_kind=kind,
        usage=copy.deepcopy(case['expected_normalized_usage'] if case else WIRE['terminal']['usage']),
        charge_micro=(case['expected_charge_micro'] if case else 2) if kind == 'settle' else 0)
    return body, auth, key


def call(env, body, **kwargs):
    return handler.handle(json.dumps(body).encode(), kind=body['terminal']['terminal_kind'],
                          runtime=env[2], settings=env[3], started=time.monotonic(), **kwargs)


def row_for(env, body):
    value = handler.parse(json.dumps(body).encode())
    claims = handler.verify(value, env[2], body['terminal']['terminal_kind'], NOW)
    return handler.intent(value, claims, handler.price(value))


def test_literal_wires():
    value = handler.parse(json.dumps(WIRE).encode())
    row = handler.intent(value, handler.verify(value, runtime(), 'settle', NOW), handler.price(value))
    assert json.loads(handler.response(row).body) == json.loads((ROOT/'pending_v1.json').read_text())
    for reason, response in json.loads((ROOT/'sync_required_v1.json').read_text()).items():
        assert json.loads(handler.sync_required(reason).body) == response


@pytest.mark.parametrize('bad', [b'\xef\xbb\xbf{}', b'{"x":1,"x":2}', b'[]', b'{}',
                                json.dumps(WIRE).replace('"input_tokens": 1', '"input_tokens": true').encode(),
                                json.dumps(WIRE).replace('"input_tokens": 1', '"input_tokens": 1.0').encode(),
                                json.dumps(WIRE).replace('"input_tokens": 1', '"input_tokens": 9223372036854775808').encode(),
                                json.dumps(WIRE).replace('"input_tokens": 1', '"input_tokens": 1,"input_tokens": 1').encode()])
def test_strict_parse(bad):
    with pytest.raises(HTTPException) as exc:
        handler.parse(bad)
    assert exc.value.status_code == 400


def test_enqueue_batch_no_money(env, monkeypatch):
    body, auth, _ = prepare(env)
    db = env[1]
    before = copy.deepcopy(db.typed)
    batches = []
    original = _FakeTransaction.batch_update

    def record(self, statements, *args, **kwargs):
        result = original(self, statements, *args, **kwargs)
        batches.append((statements, result[1]))
        return result

    monkeypatch.setattr(_FakeTransaction, 'batch_update', record)
    response = call(env, body)
    assert response.status_code == 202
    assert db.typed == before
    assert len(batches) == 1
    statements, counts = batches[0]
    assert counts == [1, 0, 0, 1]
    assert [s[0].split()[2 if s[0].startswith('INSERT') else 1] for s in statements] == [
        'tr_settle_outbox', 'tr_gateway_authorization', 'tr_reservation', 'tr_reservation']
    row = db.settle_outbox[(auth.id, 'settle')]
    assert row['next_attempt_at'] == row['created_at']
    assert row['workspace_id'] == 'ws-v1' and row['async_version'] == 1
    assert row['attempts'] == 0 and row['terminal_at'] is None and row['lease_owner'] is None
    assert not db.reservations[auth.credit_reservation_id]['settled']
    assert db.last_timeout_secs <= .5
    assert db.transaction_tags[-1] == 'tr_async_settle_enqueue'


@pytest.mark.parametrize('change,code,reason', [
    ('valid', 202, None), ('signature', 401, None), ('hash', 400, None),
    ('charge', 409, None), ('identity', 401, None), ('route', 401, None),
    ('expired', 200, 'ticket_expired'), ('cohort', 200, 'unsupported_cohort'),
    ('health', 200, 'drain_unhealthy'), ('settled', 200, 'reservation_not_open'),
    ('missing', 200, 'reservation_not_open'), ('ineligible', 200, 'not_eligible'),
])
def test_handler_matrix(env, change, code, reason):
    body, auth, _ = prepare(env)
    if change == 'signature':
        body['settlement_ticket'] = 'invalid'
    if change == 'hash':
        body['billing_snapshot']['candidates'][0]['rates']['input_micro_per_million'] += 1
    if change == 'charge':
        body['terminal']['charge_micro'] += 1
    if change == 'identity':
        body['terminal']['workspace_id'] = 'other'
    if change == 'route':
        body['terminal']['streamed'] = True
    if change == 'cohort':
        body['observed']['tool_cost'] = True
    if change == 'health':
        env[2].admission.health = None
    if change == 'settled':
        env[1].reservations[auth.credit_reservation_id]['settled'] = True
    if change == 'missing':
        del env[1].reservations[auth.credit_reservation_id]
    if change in {'expired', 'ineligible'}:
        claims = verify_lookup_ticket(body['settlement_ticket'], [env[2].signer.trusted], NOW).model_dump()
        if change == 'expired':
            claims.update(iat=NOW-400, exp=NOW-100)  # 300 s lifetime, expired 100 s before NOW
        else:
            claims['async_eligible'] = False
        body['settlement_ticket'] = env[2].signer.sign(claims, claims['iat'])
    if code >= 400:
        with pytest.raises(HTTPException) as exc:
            call(env, body)
        assert exc.value.status_code == code
        if change == 'charge':
            assert exc.value.detail == json.loads((ROOT/'amount_conflict_v1.json').read_text())
    else:
        result = call(env, body)
        assert result.status_code == code
        if reason:
            assert json.loads(result.body)['data']['reason'] == reason
    if code != 202:
        assert not env[1].settle_outbox


def test_duplicate_conflict_expired_and_immutable(env):
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    original = copy.deepcopy(env[1].settle_outbox)
    assert json.loads(call(env, body).body)['data']['acceptance']['status'] == 'duplicate'
    changed = copy.deepcopy(body)
    changed['raw_usage']['input_tokens'] = 3
    changed['terminal']['usage'].update(uncached_input_tokens=3, total_prompt_tokens=3)
    changed['terminal']['charge_micro'] = 3
    with pytest.raises(HTTPException) as exc:
        call(env, changed)
    assert exc.value.status_code == 409 and env[1].settle_outbox == original
    claims = verify_lookup_ticket(body['settlement_ticket'], [env[2].signer.trusted], NOW).model_dump()
    claims.update(iat=NOW-400, exp=NOW-100)  # 300 s lifetime, expired 100 s before NOW
    body['settlement_ticket'] = env[2].signer.sign(claims, claims['iat'])
    with pytest.raises(ValueError):
        verify_ticket(body['settlement_ticket'], [env[2].signer.trusted], claims, NOW)
    assert call(env, body).status_code == 202
    row = row_for(env, body)
    row.actual_cost_micro = 999
    _outbox(env[0]).enqueue(row)
    assert env[1].settle_outbox[(auth.id, 'settle')]['actual_cost_micro'] == 2


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_drain_and_status_ownership(env, kind):
    body, auth, key = prepare(env, kind=kind)
    assert call(env, body).status_code == 202
    principal = SimpleNamespace(workspace=SimpleNamespace(id='ws-v1'), api_key=key)
    sid = auth.id + '.' + kind
    assert status(sid, principal)['data']['trusted_router_settlement']['settlement_status'] == 'pending'
    for ws, owner in [('other', key), ('ws-v1', SimpleNamespace(hash='other'))]:
        with pytest.raises(HTTPException) as exc:
            status(sid, SimpleNamespace(workspace=SimpleNamespace(id=ws), api_key=owner))
        assert exc.value.status_code == 404
    result = drain_settle_outbox(10)
    assert result['outcomes'] == {'settled_now': 1}
    view = status(sid, principal)['data']['trusted_router_settlement']
    assert view['settlement_status'] == ('settled' if kind == 'settle' else 'refunded')
    assert view['cost_microdollars'] == (2 if kind == 'settle' else 0)
    assert view['poll_after_ms'] is None
    assert call(env, body).status_code == 200
    row = _outbox(env[0]).get(auth.id, kind)
    assert row.payload_hash and row.snapshot_hash and row.settle_body is None


@pytest.mark.parametrize('header', [None, 'async-v1', 'sync'])
@pytest.mark.parametrize('enabled', [False, True])
def test_dispatch_before_legacy_parser(env, header, enabled):
    body, _, _ = prepare(env)
    cfg = settings(settle_outbox_enabled=True)
    env[0].trust_settings = cfg
    cfg.async_settle_enabled = enabled
    cfg.async_settle_protection = enabled
    client = _client(cfg)
    client.app.state.async_settle = env[2]
    result = client.post('/v1/internal/gateway/settle', content=json.dumps(body),
                         headers={'X-TR-Settlement-Mode': header} if header else {})
    expected = (202 if header == 'async-v1' else 200) if header and enabled else 400
    assert result.status_code == expected, result.text


@pytest.mark.parametrize('admission', [False, True], ids=['rollback', 'both-on'])
@pytest.mark.parametrize('mode,accepted', [('sync', True), ('sync', False), ('async-v1', False)],
                         ids=['sync-accepted', 'sync-fresh', 'async-fresh'])
def test_snapshot_dispatch_after_admission_rollback(env, monkeypatch, admission, mode, accepted):
    from trusted_router.services import settle_outbox_apply

    body, auth, key = prepare(env)  # Issue the ticket while both flags are on.
    store, db, rt, cfg = env
    client = _client(cfg)
    client.app.state.async_settle = rt
    if accepted:
        result = client.post('/v1/internal/gateway/settle', json=body,
                             headers={'X-TR-Settlement-Mode': 'async-v1'})
        assert result.status_code == 202, result.text
    cfg.async_settle_enabled = admission
    assert cfg.async_settle_protection
    # Recovery must retain the signed price even after catalog removal.
    monkeypatch.setattr(settle_outbox_apply, 'endpoint_for_id', lambda _: None)
    before = copy.deepcopy((db.typed, db.gateway_authorizations, db.reservations, db.settle_outbox))
    result = client.post('/v1/internal/gateway/settle', json=body,
                         headers={'X-TR-Settlement-Mode': mode})
    if mode == 'async-v1':
        if admission:
            assert result.status_code == 202, result.text
            assert result.json()['data']['acceptance']['status'] == 'accepted'
            assert db.settle_outbox[(auth.id, 'settle')]['actual_cost_micro'] == 2
            assert (db.typed, db.gateway_authorizations, db.reservations) == before[:3]
        else:
            assert result.status_code == 200, result.text
            assert result.json() == json.loads((ROOT/'sync_required_v1.json').read_text())['disabled']
            assert (db.typed, db.gateway_authorizations, db.reservations, db.settle_outbox) == before
        return
    assert result.status_code == 200, result.text
    view = result.json()['data']['trusted_router_settlement']
    assert view['settlement_status'] == 'settled' and view['cost_microdollars'] == 2
    assert _typed_credit(db, 'ws-v1')['total_usage'] == 2
    assert _typed_key(db, key.hash)['usage'] == 2
    assert db.reservations[auth.credit_reservation_id]['actual_micro'] == 2
    settled = store.get_gateway_authorization(auth.id)
    assert store.get_generation(settled.finalized_generation_id).total_cost_microdollars == 2
    if accepted:
        assert db.settle_outbox[(auth.id, 'settle')]['status'] == 'done'
    else:
        assert not db.settle_outbox


SUPPORTED = [c for c in CASES if c['expected_exclusion'] is None]


@pytest.mark.parametrize('case', SUPPORTED, ids=lambda c: c['name'])
@pytest.mark.parametrize('finalizer', ['drain', 'sync'])
def test_billing_matrix_enqueue_drain_and_sync_fallback(env, case, monkeypatch, finalizer):
    body, auth, key = prepare(env, case)
    assert call(env, body).status_code == 202
    # Frozen apply must survive catalog removal or arbitrary new prices.
    from trusted_router.services import settle_outbox_apply
    monkeypatch.setattr(settle_outbox_apply, 'endpoint_for_id', lambda _: None)
    assert call(env, body).status_code == 202
    if finalizer == 'drain':
        assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    assert call(env, body, synchronous=True).status_code == 200
    assert drain_settle_outbox(10)['outcomes'] == {}
    expected = case['expected_charge_micro']
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == expected
    assert _typed_key(env[1], key.hash)['usage'] == expected
    assert env[1].reservations[auth.credit_reservation_id]['actual_micro'] == expected
    settled = env[0].get_gateway_authorization(auth.id)
    generation = env[0].get_generation(settled.finalized_generation_id)
    assert generation.total_cost_microdollars == expected
    assert generation.tokens_prompt == case['expected_normalized_usage']['total_prompt_tokens']
    assert call(env, body).status_code == 200


def test_admission_miss_rolls_back(env):
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    env[1].reservations[auth.credit_reservation_id]['settled'] = True
    before = copy.deepcopy(env[1].gateway_authorizations)
    with pytest.raises(ReservationNotOpen):
        enqueue(_outbox(env[0]), row, time.monotonic()+.5)
    assert not env[1].settle_outbox
    assert before == env[1].gateway_authorizations


@pytest.mark.parametrize('winner', ['reaper', 'sync'])
@pytest.mark.parametrize('boundary', range(6))
def test_interleaving_sweep(env, monkeypatch, winner, boundary):
    """Competing commit before callback, after each statement, and before commit.

    The fake validates reservation versions at commit. A loser retries its
    complete batch, then the atomic still-open count rolls the INSERT back.
    """
    from trusted_router.storage_gcp_authorize import settle_atomic
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    db = env[1]
    original_update = _FakeTransaction.execute_update
    original_commit = db._try_commit
    fired = False
    count = 0

    def compete():
        nonlocal fired
        if fired:
            return
        fired = True
        result = settle_atomic(db, env[0]._param_types, reservation_id=auth.credit_reservation_id,
                               actual_micro=7 if winner == 'sync' else 0,
                               settled_usage_type='Credits', success=winner == 'sync',
                               guard_outbox=winner == 'reaper', outbox_available=True)
        assert result['outcome'] == 'settled'

    def update(self, sql, *args, **kwargs):
        nonlocal count
        result = original_update(self, sql, *args, **kwargs)
        if not fired:
            count += 1
            if count == boundary:
                compete()
        return result

    def commit(tx):
        if boundary == 5:
            compete()
        return original_commit(tx)

    monkeypatch.setattr(_FakeTransaction, 'execute_update', update)
    monkeypatch.setattr(db, '_try_commit', commit)
    if boundary == 0:
        compete()
    with pytest.raises(ReservationNotOpen):
        enqueue(_outbox(env[0]), row, time.monotonic()+.5)
    assert fired and not db.settle_outbox
    assert db.reservations[auth.credit_reservation_id]['actual_micro'] == (7 if winner == 'sync' else 0)
    assert _typed_credit(db, 'ws-v1')['total_usage'] == (7 if winner == 'sync' else 0)


def test_enqueue_wins_reaper_and_legacy_fence(env):
    from trusted_router.storage_gcp_authorize import settle_atomic
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    for reaper in [False, True]:
        result = settle_atomic(env[1], env[0]._param_types, reservation_id=auth.credit_reservation_id,
                               actual_micro=900, settled_usage_type='Credits', success=not reaper,
                               guard_outbox=reaper, outbox_available=True, async_fence=True)
        assert result['outcome'] != 'settled'
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 2


def test_budget_includes_admission_and_retries(env, monkeypatch):
    body, _, _ = prepare(env)
    clock = [50.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    admission = env[2].admission.eligible

    def slow(*args):
        result = admission(*args)
        clock[0] += .51
        return result

    monkeypatch.setattr(env[2].admission, 'eligible', slow)
    with pytest.raises(HTTPException) as exc:
        handler.handle(json.dumps(body).encode(), kind='settle', runtime=env[2],
                       settings=env[3], started=50.0)
    assert exc.value.status_code == 503
    assert not env[1].settle_outbox


def test_unknown_commit_retry_same_identity(env, monkeypatch):
    from google.api_core.exceptions import DeadlineExceeded
    body, auth, _ = prepare(env)
    run = env[1].run_in_transaction
    calls = []

    def lost(fn, **kwargs):
        calls.append(kwargs)
        result = run(fn, **kwargs)
        if len(calls) == 1:
            raise DeadlineExceeded('lost commit reply')
        return result

    monkeypatch.setattr(env[1], 'run_in_transaction', lost)
    result = call(env, body)
    assert result.status_code == 202
    assert len(calls) == 2
    assert list(env[1].settle_outbox) == [(auth.id, 'settle')]
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0


def test_expired_snapshot_sync_fallback_without_pending_acceptance(env):
    body, auth, _ = prepare(env)
    claims = verify_lookup_ticket(body['settlement_ticket'], [env[2].signer.trusted], NOW).model_dump()
    claims.update(iat=NOW-400, exp=NOW-100)  # 300 s lifetime, expired 100 s before NOW
    body['settlement_ticket'] = env[2].signer.sign(claims, claims['iat'])
    assert call(env, body, synchronous=True).status_code == 200
    assert not env[1].settle_outbox
    assert env[1].reservations[auth.credit_reservation_id]['actual_micro'] == 2


@pytest.mark.parametrize('winner', ['reaper', 'sync'])
def test_concurrent_enqueue_vs_terminal(env, winner, monkeypatch):
    monkeypatch.setattr(time, 'monotonic', lambda: 10.0)
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from trusted_router.storage_gcp_authorize import settle_atomic
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    env[1]._ready_barrier = threading.Barrier(2)

    def accept():
        try:
            enqueue(_outbox(env[0]), row, time.monotonic()+.5)
            return True
        except ReservationNotOpen:
            return False

    def compete():
        return settle_atomic(env[1], env[0]._param_types, reservation_id=auth.credit_reservation_id,
                             actual_micro=9, settled_usage_type='Credits', success=winner == 'sync',
                             guard_outbox=winner == 'reaper', outbox_available=True)

    with ThreadPoolExecutor(2) as pool:
        accepting, terminal = pool.submit(accept), pool.submit(compete)
        accepted, result = accepting.result(), terminal.result()
    assert accepted == bool(env[1].settle_outbox)
    assert accepted == (result['outcome'] != 'settled')
    assert not accepted or not env[1].reservations[auth.credit_reservation_id]['settled']
    env[1]._ready_barrier = None


def test_mark_park_never_rewrites_async_metadata(env):
    body, auth, _ = prepare(env)
    call(env, body)
    outbox = _outbox(env[0])
    frozen = outbox.get(auth.id, 'settle')
    claimed = outbox.claim(limit=1)[0]
    keys = ('actual_cost_micro', 'payload_hash', 'snapshot_hash', 'workspace_id', 'settle_body')
    assert not outbox.park(auth.id, 'settle', lease_owner='stale', retry_after_seconds=1, note='test')
    assert outbox.mark(auth.id, 'settle', done=True, lease_owner='stale') is None
    assert outbox.park(auth.id, 'settle', lease_owner=claimed.lease_owner, retry_after_seconds=1, note='test')
    after = outbox.get(auth.id, 'settle')
    assert all(getattr(frozen, k) == getattr(after, k) for k in keys)
    assert outbox.mark(auth.id, 'settle', done=False, force_dead=True) == 'dead'
    assert handler.settlement(outbox.get(auth.id, 'settle'))['settlement_status'] == 'pending'


def test_header_authentication_unchanged(env):
    body, _, _ = prepare(env)
    cfg = settings(settle_outbox_enabled=True, internal_gateway_token='secret')  # noqa: S106 - test credential
    client = _client(cfg)
    client.app.state.async_settle = env[2]
    result = client.post('/v1/internal/gateway/settle', json=body,
                         headers={'X-TR-Settlement-Mode': 'async-v1'})
    assert result.status_code == 401
    assert not env[1].settle_outbox


def test_late_commit_does_not_silently_extend_handoff(env, monkeypatch):
    body, auth, _ = prepare(env)
    clock = [10.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    run = env[1].run_in_transaction

    def late(*args, **kwargs):
        result = run(*args, **kwargs)
        clock[0] += .51
        return result

    monkeypatch.setattr(env[1], 'run_in_transaction', late)
    with pytest.raises(HTTPException) as exc:
        handler.handle(json.dumps(body).encode(), kind='settle', runtime=env[2],
                       settings=env[3], started=10.0)
    assert exc.value.status_code == 503
    assert list(env[1].settle_outbox) == [(auth.id, 'settle')]


@pytest.mark.parametrize('admission', [True, False])
@pytest.mark.parametrize('outbox_enabled', [True, False])
@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_legacy_retry_preserves_accepted_amount(env, monkeypatch, outbox_enabled, kind, admission):
    from tests.test_billing_snapshot import endpoint_from_candidate
    from trusted_router.catalog_data import Model
    from trusted_router.routes.internal import gateway
    from trusted_router.schemas import GatewaySettleRequest

    body, auth, _ = prepare(env)
    call(env, body)
    endpoint = endpoint_from_candidate(body['billing_snapshot']['candidates'][0])
    monkeypatch.setattr(gateway, 'endpoint_for_id', lambda _: endpoint)
    monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(
        id=endpoint.model_id, name='retry', provider=endpoint.provider,
        context_length=1_000_000, prepaid_available=True))
    cfg = settings(settle_outbox_enabled=outbox_enabled)
    cfg.async_settle_enabled = admission
    env[0].trust_settings = cfg
    # Refresh must remain immutable even before the legacy claim fence runs.
    changed = row_for(env, body)
    changed.actual_cost_micro = 100
    _outbox(env[0]).enqueue(changed)
    assert env[1].settle_outbox[(auth.id, 'settle')]['actual_cost_micro'] == 2
    result = gateway._settle_gateway_authorization(
        GatewaySettleRequest(authorization_id=auth.id, selected_endpoint=endpoint.id,
                             actual_input_tokens=100, actual_output_tokens=100),
        success=kind == 'settle', settings=cfg)
    assert result['data']['cost_microdollars'] == 2
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 2
    assert env[1].settle_outbox[(auth.id, 'settle')]['actual_cost_micro'] == 2


def test_literal_duplicate_and_conflict(env, monkeypatch):
    value = handler.parse(json.dumps(WIRE).encode())
    row = handler.intent(value, handler.verify(value, runtime(), 'settle', NOW), 2)
    monkeypatch.setattr(type(env[0]), 'get_gateway_authorization', lambda self, _: None)
    assert json.loads(handler.response(row, duplicate=True).body) == json.loads((ROOT/'duplicate_v1.json').read_text())
    body, _, _ = prepare(env)
    call(env, body)
    body['terminal']['charge_micro'] = 3
    body['raw_usage']['input_tokens'] = 3
    body['terminal']['usage'].update(uncached_input_tokens=3, total_prompt_tokens=3)
    with pytest.raises(HTTPException) as exc:
        call(env, body)
    assert exc.value.detail == json.loads((ROOT/'conflict_v1.json').read_text())


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_literal_status_and_timestamps(env, monkeypatch, kind):
    from trusted_router import storage_gcp_async_settle, storage_gcp_settle_outbox
    stamp = '2026-10-06T00:00:00Z'
    monkeypatch.setattr(storage_gcp_async_settle, '_iso_now', lambda: stamp)
    monkeypatch.setattr(storage_gcp_settle_outbox, '_iso_now', lambda: stamp)
    body, auth, key = prepare(env, kind=kind)
    call(env, body)
    principal = SimpleNamespace(workspace=SimpleNamespace(id='ws-v1'), api_key=key)
    for completed in (False, True):
        if completed:
            drain_settle_outbox(10)
        view = status(auth.id+'.'+kind, principal)
        literal = json.loads(json.dumps(view).replace(auth.id, 'auth-v1'))
        state = ('settled' if kind == 'settle' else 'refunded') if completed else 'pending'
        if state == 'pending' and kind == 'refund':
            assert literal['data']['trusted_router_settlement']['cost_microdollars'] == 0
        else:
            assert literal == json.loads((ROOT/f'status_{state}_v1.json').read_text())


@pytest.mark.parametrize('denial,reason', [('cap', 'cap_exceeded'), ('tier', 'not_eligible'),
                                         ('read', 'admission_stale'), ('stale', 'admission_stale')])
def test_admission_reason_matrix(env, denial, reason):
    from trusted_router.services.async_settle import Admission, DrainHealth
    body, _, _ = prepare(env)
    cache = env[2].admission
    if denial == 'cap':
        cache.entries['ws-v1'] = (10, Admission(100_000_001, 2))
    elif denial == 'tier':
        cache.entries['ws-v1'] = (10, Admission(0, 1))
    elif denial == 'read':
        cache.entries['ws-v1'] = (10, None)
    else:
        cache.health = DrainHealth(0, 0)
    result = json.loads(call(env, body).body)
    assert result['data']['reason'] == reason
    assert not env[1].settle_outbox


def test_sibling_refund_reports_charge_winner(env):
    body, auth, key = prepare(env)
    call(env, body)
    refund = copy.deepcopy(body)
    refund['terminal'].update(terminal_kind='refund', charge_micro=0)
    call(env, refund)
    drain_settle_outbox(10)
    view = status(auth.id+'.refund', SimpleNamespace(
        workspace=SimpleNamespace(id='ws-v1'), api_key=key))['data']['trusted_router_settlement']
    assert view['settlement_status'] == 'settled'
    assert view['cost_microdollars'] == 2 and view['review_required'] is True
    assert env[1].settle_outbox[(auth.id, 'refund')]['actual_cost_micro'] == 0


def test_sync_fallback_does_not_impersonate_worker_lease(env):
    body, auth, _ = prepare(env)
    call(env, body)
    outbox = _outbox(env[0])
    claimed = outbox.claim(limit=1)[0]
    assert call(env, body, synchronous=True).status_code == 200
    row = outbox.get(auth.id, 'settle')
    assert row.lease_owner == claimed.lease_owner and row.status == 'pending'
    assert outbox.mark(auth.id, 'settle', done=True, lease_owner=claimed.lease_owner) == 'done'


@pytest.fixture(scope='module')
def matrix_client():
    return _client(settings(settle_outbox_enabled=True))


def matrix_cases():
    from itertools import product
    return list(product((False, True), repeat=6))


@pytest.mark.parametrize('header,flag,ticket,hash_ok,cohort,healthy', matrix_cases())
@pytest.mark.parametrize('reservation', ['open', 'settled', 'missing'])
def test_full_handler_matrix(env, matrix_client, header, flag, ticket, hash_ok, cohort, healthy, reservation):
    body, auth, _ = prepare(env)
    env[3].async_settle_enabled = flag
    matrix_client.app.state.settings = env[3]
    matrix_client.app.state.async_settle = env[2]
    if not ticket:
        body['settlement_ticket'] = 'invalid'
    if not hash_ok:
        body['billing_snapshot']['candidates'][0]['rates']['input_micro_per_million'] += 1
    if not cohort:
        body['observed']['tool_cost'] = True
    if not healthy:
        env[2].admission.health = None
    if reservation == 'settled':
        env[1].reservations[auth.credit_reservation_id]['settled'] = True
    if reservation == 'missing':
        del env[1].reservations[auth.credit_reservation_id]
    expected = (400 if not header else 401 if not ticket else 400 if not hash_ok
                else 200 if not (flag and cohort and healthy and reservation == 'open') else 202)
    result = matrix_client.post('/v1/internal/gateway/settle', json=body,
                                headers={'X-TR-Settlement-Mode': 'async-v1'} if header else {})
    assert result.status_code == expected, result.text
    if header and ticket and hash_ok and cohort and not flag:
        assert result.json() == json.loads((ROOT/'sync_required_v1.json').read_text())['disabled']
    assert bool(env[1].settle_outbox) == (expected == 202)


def test_retry_timeout_is_remaining_handoff_budget(env, monkeypatch):
    from google.api_core.exceptions import DeadlineExceeded
    body, _, _ = prepare(env)
    clock = [10.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    run = env[1].run_in_transaction
    timeouts = []

    def attempts(fn, **kwargs):
        timeouts.append(kwargs['timeout_secs'])
        clock[0] += .3
        if len(timeouts) == 1:
            raise DeadlineExceeded('lost transport before commit')
        return run(fn, **kwargs)

    monkeypatch.setattr(env[1], 'run_in_transaction', attempts)
    with pytest.raises(HTTPException) as exc:
        call(env, body)
    assert exc.value.status_code == 503
    assert len(timeouts) == 2 and timeouts[0] <= .5 and timeouts[1] < .21
    assert not env[1].settle_outbox


def test_accepted_amount_survives_evaluator_disagreement(env, monkeypatch):
    body, _, _ = prepare(env)
    call(env, body)

    def disagree(*args, **kwargs):
        raise ValueError('later evaluator disagreement')

    monkeypatch.setattr(billing, 'evaluate', disagree)
    result = call(env, body)
    assert result.status_code == 202
    assert json.loads(result.body)['data']['trusted_router_settlement']['cost_microdollars'] == 2
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    assert call(env, body).status_code == 200


@pytest.mark.parametrize('changed_observed', [False, True])
def test_sync_reconciles_accepted_intent_before_evaluation(env, monkeypatch, changed_observed):
    body, _, _ = prepare(env)
    assert call(env, body).status_code == 202
    if changed_observed:
        body['observed']['tool_cost'] = True

    def forbidden(*args, **kwargs):
        raise AssertionError('Accepted synchronous fallback must not evaluate again')

    monkeypatch.setattr(billing, 'evaluate', forbidden)
    assert call(env, body, synchronous=True).status_code == 200
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 2


def test_accepted_retry_with_changed_cohort_returns_existing(env):
    body, _, _ = prepare(env)
    assert call(env, body).status_code == 202
    body['observed']['tool_cost'] = True
    result = call(env, body)
    assert result.status_code == 202
    data = json.loads(result.body)['data']
    assert data['acceptance']['status'] == 'duplicate'
    assert data['trusted_router_settlement']['cost_microdollars'] == 2


@pytest.mark.parametrize('enabled', [False, True])
def test_claim_sql_protection_pin(enabled):
    from google.cloud.spanner_v1 import param_types as pt

    from trusted_router.storage_gcp_counter_dml import claim_reservation_statement

    sql, params, types = claim_reservation_statement(
        pt, 'rid', actual_micro=2, settled_usage_type='Credits',
        defer_retention=True, async_fence=enabled,
    )
    expected = ('UPDATE tr_reservation SET settled=true, actual_micro=@actual, '
                'settled_usage_type=@sut, terminal_at=@terminal_at '
                'WHERE reservation_id=@rid AND settled=false')
    expected_params = dict(rid='rid', actual=2, sut='Credits', terminal_at=None)
    expected_types = dict(rid=pt.STRING, actual=pt.INT64, sut=pt.STRING, terminal_at=pt.TIMESTAMP)
    if enabled:
        expected += (
            ' AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox a '
            'WHERE a.authorization_id = tr_reservation.authorization_id '
            "AND a.status IN ('pending', 'dead') "
            'AND a.async_version=1 AND (@async_hash IS NULL OR '
            '(a.intent_kind=@async_kind AND '
            '(a.payload_hash!=@async_hash OR a.actual_cost_micro!=@actual))))'
        )
        expected_params.update(async_hash=None, async_kind=None)
        expected_types.update(async_hash=pt.STRING, async_kind=pt.STRING)
    assert (sql, params, types) == (expected, expected_params, expected_types)


@pytest.mark.parametrize('async_version', [None, 1])
def test_operator_approved_release(env, async_version):
    from datetime import UTC, datetime, timedelta

    body, auth, _ = prepare(env)
    call(env, body)
    now = datetime.now(UTC)
    env[1].reservations[auth.credit_reservation_id]['expires_at'] = now - timedelta(seconds=1)
    row = env[1].settle_outbox[(auth.id, 'settle')]
    row.update(status='release_approved', async_version=async_version)
    result = env[0].reap_expired_reservations_result(now=now, limit=10)
    assert result.count == 1
    assert _typed_credit(env[1], 'ws-v1')['reserved'] == 0
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0


def test_expired_handoff_attempts_one_rollback(monkeypatch):
    from trusted_router import storage_gcp_io as io

    clock = [10.0]
    calls = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    def rollback():
        calls.append(io.remaining_rpc_budget(2))

    with io.spanner_rpc_deadline(10.5):
        clock[0] = 10.501
        io._rollback_discarded_transaction(SimpleNamespace(rollback=rollback))
    assert len(calls) == 1 and calls[0] == pytest.approx(.05)
    assert io._SPANNER_RPC_DEADLINE.get() is None
    assert not io._STRICT_RPC_DEADLINE.get()


@pytest.mark.parametrize('elapsed', [.499, .501])
def test_commit_handoff_boundary(env, monkeypatch, elapsed):
    body, auth, _ = prepare(env)
    clock = [10.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    run = env[1].run_in_transaction

    def commit(*args, **kwargs):
        result = run(*args, **kwargs)
        clock[0] = 10 + elapsed
        return result

    monkeypatch.setattr(env[1], 'run_in_transaction', commit)
    if elapsed < .5:
        assert call(env, body).status_code == 202
    else:
        with pytest.raises(HTTPException) as exc:
            call(env, body)
        assert exc.value.status_code == 503
    assert list(env[1].settle_outbox) == [(auth.id, 'settle')]


def test_lookup_ticket_rejects_other_audience(env):
    from dataclasses import replace

    from trusted_router.async_settle_ticket import TYP
    from trusted_router.detached_jws import b64encode, canonical

    body, _, _ = prepare(env)
    signer = env[2].signer
    claims = verify_lookup_ticket(body['settlement_ticket'], [signer.trusted], NOW).model_dump()
    claims['aud'] = 'another-purpose'
    material = b64encode(canonical(dict(alg='EdDSA', kid=signer.trusted.kid, typ=TYP))) + '.' + b64encode(canonical(claims))
    token = material + '.' + b64encode(signer.private.sign(material.encode('ascii')))
    with pytest.raises(ValueError, match='ticket lookup validity'):
        verify_lookup_ticket(token, [replace(signer.trusted, aud='another-purpose')], NOW)


@pytest.mark.parametrize('admission,protection', [(False, False), (False, True), (True, False)])
def test_runtime_admission_fails_closed(env, admission, protection):
    body, _, _ = prepare(env)
    env[3].async_settle_enabled = admission
    env[3].async_settle_protection = protection
    result = call(env, body)
    assert json.loads(result.body)['data']['reason'] == 'disabled'
    assert not env[1].settle_outbox
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    response = client.post('/v1/internal/gateway/settle', json=body,
                           headers={'X-TR-Settlement-Mode': 'async-v1'})
    assert response.status_code == (200 if protection else 400)
    if protection:
        assert response.json() == json.loads((ROOT/'sync_required_v1.json').read_text())['disabled']
    assert not env[1].settle_outbox


@pytest.mark.parametrize('reply_lost', [False, True])
def test_late_batch_cleanup_chain_has_one_budget(env, monkeypatch, reply_lost):
    """Real retry wrapper -> bounded rollback -> enqueue's outer disposer.

    The runner models SDK GoogleAPICallError handling: it discards the handle
    without its generic-exception rollback. A lost reply leaves rolled_back false.
    """
    from google.api_core.exceptions import DeadlineExceeded
    from google.rpc.status_pb2 import Status

    from trusted_router import storage_gcp_io as io

    body, _, _ = prepare(env)
    row = row_for(env, body)
    clock = [10.0]
    calls = []
    commits = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    def batch_update(statements):
        clock[0] = 10.501
        return Status(code=0), [1, 0, 0, 1]

    def rollback():
        start = clock[0]
        budget = io.remaining_rpc_budget(2)
        clock[0] += budget
        calls.append((start, clock[0]))
        if reply_lost:
            raise DeadlineExceeded('rollback reply lost')
        tx.rolled_back = True

    tx = SimpleNamespace(batch_update=batch_update, rollback=rollback,
                         committed=None, rolled_back=False)

    def sdk_runner(callback, **kwargs):
        callback(tx)
        commits.append(clock[0])

    monkeypatch.setattr(env[1], 'run_in_transaction', sdk_runner)
    with pytest.raises(DeadlineExceeded, match='before commit'):
        enqueue(_outbox(env[0]), row, 10.5)
    assert not commits
    assert len(calls) == 1, calls
    assert not env[1].settle_outbox
    assert calls[0] == pytest.approx((10.501, 10.551))
    assert clock[0] - 10.501 <= .055
    assert tx.rolled_back is (not reply_lost)
    assert io._SPANNER_RPC_DEADLINE.get() is None
    assert not io._STRICT_RPC_DEADLINE.get()


@pytest.mark.parametrize('allowlist,expected', [('', 202), ('ws-v1', 202), ('ws-v2', 200)])
def test_pilot_workspace_settle(env, allowlist, expected):
    body, _, _ = prepare(env)  # Ticket precedes pilot policy change.
    cfg = env[3]
    cfg.async_settle_pilot_workspaces = allowlist
    cfg.parse_pilot_workspaces()
    result = call(env, body)
    assert result.status_code == expected
    assert bool(env[1].settle_outbox) is (expected == 202)
    if expected == 200:
        assert json.loads(result.body)['data']['reason'] == 'not_eligible'


@pytest.mark.parametrize('tier', [2, 3])
@pytest.mark.parametrize('amount', [5000000, 5000001])
def test_pilot_settle_cap_override(env, tier, amount):
    from trusted_router.services.async_settle import Admission
    body, _, _ = prepare(env)
    env[3].async_settle_pilot_cap_micro = 5000000
    env[2].admission.entries.clear()
    env[2].admission.read = lambda _: Admission(amount, tier)
    response = call(env, body)
    assert response.status_code == (202 if amount <= 5000000 else 200)
    assert bool(env[1].settle_outbox) is (amount <= 5000000)


@pytest.mark.parametrize('flip', ['admission', 'allowlist', 'fleet_health'])
def test_pilot_rollback_drains_truthfully(env, flip):
    body, auth, key = prepare(env)
    assert call(env, body).status_code == 202
    principal = SimpleNamespace(workspace=SimpleNamespace(id='ws-v1'), api_key=key)
    sid = auth.id + '.settle'
    assert status(sid, principal)['data']['trusted_router_settlement']['settlement_status'] == 'pending'
    if flip == 'admission':
        env[3].async_settle_enabled = False
    elif flip == 'allowlist':
        env[3].async_settle_pilot_workspaces = 'ws-v2'
        env[3].parse_pilot_workspaces()
    else:
        env[2].admission.health = None
    assert call(env, body).status_code == 202
    assert status(sid, principal)['data']['trusted_router_settlement']['settlement_status'] == 'pending'
    assert env[3].async_settle_protection
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    terminal = status(sid, principal)['data']['trusted_router_settlement']
    assert terminal['settlement_status'] == 'settled' and terminal['cost_microdollars'] == 2
    assert call(env, body).status_code == 200
    env[3].async_settle_enabled = False
    env[3].async_settle_protection = False
    assert status(sid, principal)['data']['trusted_router_settlement']['settlement_status'] == 'settled'


def test_pilot_durable_fleet_health_flip(env):
    from tests.test_async_settle_drain import healthy
    from trusted_router.services.async_settle import Admission, AdmissionCache
    from trusted_router.storage_gcp_async_admission import (
        HEALTH_ID,
        HEALTH_KIND,
        publish_health,
        read_health,
    )
    body, auth, key = prepare(env)
    publish_health(env[1])
    env[1].rows[(HEALTH_KIND, HEALTH_ID)].body = json.dumps(healthy())
    clock = [40.]
    cache = AdmissionCache(lambda _: Admission(0, 2), clock=lambda: clock[0],
                           wall_clock=lambda: 100., health_read=lambda: read_health(env[1]))
    env[2].admission = cache
    assert call(env, body).status_code == 202
    env[1].rows[(HEALTH_KIND, HEALTH_ID)].body = json.dumps(healthy(complete=False))
    clock[0] += 1
    assert not cache.eligible('ws-v1', 5000000)
    assert call(env, body).status_code == 202
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    principal = SimpleNamespace(workspace=SimpleNamespace(id='ws-v1'), api_key=key)
    assert status(auth.id + '.settle', principal)['data']['trusted_router_settlement']['settlement_status'] == 'settled'
