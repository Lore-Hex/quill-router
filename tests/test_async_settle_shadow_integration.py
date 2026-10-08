# ruff: noqa: F811 - imported pytest fixture
from __future__ import annotations

import copy
import json

import pytest

from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_async_settle_oracle import endpoint_from_candidate
from tests.test_settle_outbox_drain import _client, _typed_credit, _typed_key
from trusted_router.catalog_data import Model
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.services.async_settle_shadow import Runtime


@pytest.mark.parametrize('kind',['settle','refund'])
@pytest.mark.parametrize('optin',['','nonmember','ws-v1'])
@pytest.mark.parametrize('failure',['none','submit','capture'])
def test_actual_entry_no_interference(env,monkeypatch,kind,optin,failure):
    body,auth,key = prepare(env,kind=kind)
    store,db,rt,cfg = env
    repair = json.loads(row_for(env,body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = Settings(environment='test',async_settle_shadow_workspaces=optin).async_settle_shadow_workspace_ids
    endpoints = {c['endpoint_id']:endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS,endpoint.model_id,Model(id=endpoint.model_id,name='shadow',provider=endpoint.provider,context_length=1000000,prepaid_available=True))
    monkeypatch.setattr(gateway,'endpoint_for_id',endpoints.get)
    client = _client(cfg)
    shadow = Runtime(cfg,rt)
    client.app.state.async_settle_shadow = shadow
    calls = []
    booked_at_submit = []
    original = shadow.submit
    def submit(*args):
        capture = args[0]
        if capture.authorization:
            calls.append(capture.kind)
            booked_at_submit.append(db.gateway_authorizations[auth.id]['settled'])
            if failure == 'submit':
                raise RuntimeError('injected')
        return original(*args)
    monkeypatch.setattr(shadow,'submit',submit)
    if failure == 'capture':
        from trusted_router.services import async_settle_shadow
        monkeypatch.setattr(async_settle_shadow,'capture_prices',lambda *args:(_ for _ in ()).throw(RuntimeError('injected')))
    before = copy.deepcopy(db.typed)
    result = client.post('/v1/internal/gateway/'+kind,json=repair,headers={'X-TR-Settlement-Shadow':'!'*12289})
    assert result.status_code == 200, result.text
    assert result.json()['data']['cost_microdollars'] == (2 if kind == 'settle' else 0)
    assert _typed_credit(db,'ws-v1')['total_usage'] == (2 if kind == 'settle' else 0)
    assert _typed_key(db,key.hash)['usage'] == (2 if kind == 'settle' else 0)
    assert calls == ([kind] if optin == 'ws-v1' else [])
    assert booked_at_submit == ([True] if optin == 'ws-v1' else [])
    assert before != db.typed  # Hold is released even for zero-charge refunds.
    shadow.executor.shutdown()


@pytest.mark.parametrize('go_amount',[2,999])
@pytest.mark.parametrize('injection',['none','parser','signature','evaluator','storage','queue'])
def test_shadow_failure_after_real_money_commit(env,monkeypatch,go_amount,injection):
    import hashlib
    from dataclasses import asdict

    from tests.test_async_settle_shadow import NOW, signer, wire
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.async_settle_shadow_evidence import validate_sample
    from trusted_router.detached_jws import canonical
    from trusted_router.storage_models import generation_id_for_authorization

    body,auth,key = prepare(env)
    store,db,rt,cfg = env
    repair = json.loads(row_for(env,body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg.release = 'a'*40
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
    # Persist the real server nonce as authorize does (prepare's PR-C helper
    # only attaches its nonce to the returned detached authorization).
    payload = json.loads(db.gateway_authorizations[auth.id]['payload'])
    payload['invocation_nonce'] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]['payload'] = json.dumps(payload)
    endpoints = {c['endpoint_id']:endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS,endpoint.model_id,Model(id=endpoint.model_id,name='shadow',provider=endpoint.provider,context_length=1000000,prepaid_available=True))
    monkeypatch.setattr(gateway,'endpoint_for_id',endpoints.get)
    from tests.test_async_settle_shadow import FIXTURE
    envelope = copy.deepcopy(FIXTURE)
    envelope['billing_snapshot'] = body['billing_snapshot']
    envelope['terminal'] = copy.deepcopy(body['terminal'])
    envelope['terminal']['charge_micro'] = go_amount
    claims = dict(authorization_id=auth.id,generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id,key_id=auth.key_hash,invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id,billing_authority='local',journal_region=rt.region,
        epoch=rt.epoch,route_type='chat.completions',streamed=False,settle_origin='typed',snapshot_version=1,
        snapshot_hash=body['terminal']['snapshot_hash'],async_eligible=False,iss='router-fixture',aud='router-shadow',iat=NOW,exp=NOW+172800)
    envelope['billing_shadow_binding'] = signer().sign(claims,NOW)
    envelope['payload_hash'] = hashlib.sha256(canonical(envelope['terminal'])).hexdigest()
    evidence = []
    evidence_commit_counts = []
    booking_observations = []
    sample_commit_counts = []
    commits_before = db.commits
    class DetachedStore:
        def booking(self,identity,deadline):
            booking_observations.append((db.commits, db.gateway_authorizations[identity]['settled']))
            record = db.gateway_authorizations[identity]
            return Booking(record['finalized_cost_microdollars'],record['finalization_outcome'],True)
        def reserve(self,day,deadline):
            evidence_commit_counts.append(db.commits)
            return 100
        def insert_sample(self,identity,row,deadline):
            sample_commit_counts.append(db.commits)
            validate_sample(row,identity)
            if injection == 'storage':
                raise RuntimeError('injected')
            evidence.append(row)
            return 'inserted'
        def flush(self,*args):
            pass
    shadow = Runtime(cfg,rt,DetachedStore())
    shadow.signer = signer()
    client = _client(cfg)
    client.app.state.async_settle_shadow = shadow
    def fail(*args,**kwargs):
        raise RuntimeError('injected')
    if injection == 'queue':
        monkeypatch.setattr(shadow,'submit',fail)
    elif injection in {'parser','signature','evaluator'}:
        import trusted_router.async_settle_shadow_compare as comparator
        if injection == 'parser':
            monkeypatch.setattr(comparator,'parse_header',fail)
        elif injection == 'signature':
            monkeypatch.setattr(comparator,'verify_binding',fail)
        else:
            monkeypatch.setattr(comparator.b,'evaluate',fail)
    before_auth = asdict(auth)
    try:
        result = client.post('/v1/internal/gateway/settle',json=repair,headers={'X-TR-Settlement-Shadow':wire(envelope)[0]})
    except RuntimeError as error:
        result = error
    finally:
        shadow.executor.shutdown()
    assert not isinstance(result, RuntimeError), 'shadow changed the money response'
    assert all(count > commits_before and settled for count, settled in booking_observations)
    assert all(count > commits_before for count in sample_commit_counts)
    assert all(count > commits_before for count in evidence_commit_counts), 'evidence preceded money outcome'
    assert result.status_code == 200 and result.json()['data']['cost_microdollars'] == 2
    assert _typed_credit(db,'ws-v1')['total_usage'] == _typed_key(db,key.hash)['usage'] == 2
    assert db.reservations[auth.credit_reservation_id]['actual_micro'] == 2
    assert auth.workspace_id == before_auth['workspace_id']
    if injection == 'none':
        assert [(row['classification'],row['booked_micro'],row['go_micro']) for row in evidence] == [
            ('exact' if go_amount == 2 else 'evaluator_disagreement',2,go_amount)]
    else:
        assert evidence == []
    counters = shadow.counters.snapshot()
    for _, counter in counters:
        assert counter['comparison_attempts'] == sum(counter[name] for name in (
            'samples_inserted', 'duplicate_samples', 'conflicting_samples', 'comparison_dropped'))
    shadow.executor.shutdown()


def test_shadow_submission_failure_preserves_real_legacy_error(env, monkeypatch):
    from fastapi import HTTPException
    body, auth, _ = prepare(env)
    _, db, rt, cfg = env
    repair = json.loads(row_for(env, body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
    client = _client(cfg)
    shadow = Runtime(cfg, rt)
    client.app.state.async_settle_shadow = shadow
    calls = []
    def broken_submit(*args):
        calls.append(args[0].authorization.id)
        raise RuntimeError('shadow failure must be isolated')
    def legacy_error(*args, **kwargs):
        raise HTTPException(409, detail='original legacy failure', headers={'Retry-After':'2'})
    monkeypatch.setattr(shadow, 'submit', broken_submit)
    monkeypatch.setattr(gateway, '_select_authorized_endpoint', legacy_error)
    before = copy.deepcopy(db.typed)
    try:
        response = client.post('/v1/internal/gateway/settle', json=repair)
    except RuntimeError as error:
        response = error
    assert not isinstance(response, RuntimeError), 'shadow replaced the legacy error'
    assert (response.status_code, response.json(), response.headers.get('Retry-After')) == (409, {'error': {'code':409,'message':'original legacy failure','type':'http_error','source':'router'}}, '2')
    assert calls == [auth.id]
    assert db.typed == before
    shadow.executor.shutdown()


@pytest.mark.parametrize('failure',['gate','legacy'])
def test_failed_legacy_attempt_is_counted_without_lost_background_queue(env,monkeypatch,failure):
    from fastapi import HTTPException
    body, _, _ = prepare(env)
    _, db, rt, cfg = env
    repair = json.loads(row_for(env,body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
    client = _client(cfg)
    shadow = Runtime(cfg,rt)
    client.app.state.async_settle_shadow = shadow
    if failure == 'gate':
        monkeypatch.setattr(type(gateway._SETTLE_ADMISSION),'try_acquire',lambda *a,**k:False)
    else:
        def fail(*args,**kwargs):
            raise HTTPException(409,detail='original failure')
        monkeypatch.setattr(gateway,'_select_authorized_endpoint',fail)
    before = copy.deepcopy(db.typed)
    response = client.post('/v1/internal/gateway/settle',json=repair)
    assert response.status_code == (503 if failure=='gate' else 409)
    assert db.typed == before and (shadow.pending,shadow.queued_bytes) == (0,0)
    counter = shadow.counters.snapshot()[0][1]
    assert counter['booking_unknown'] == 1
    assert counter['exclusions'] == [dict(phase='settle',adapter='openai',route_type='chat.completions',streamed=None,reason='booking_unknown',count=1)]
    shadow.executor.shutdown()


def test_reviewer_hash_only_replay(env,monkeypatch):
    go_amount = 2
    import hashlib
    from dataclasses import asdict

    from tests.test_async_settle_shadow import NOW, signer, wire
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.async_settle_shadow_evidence import validate_sample
    from trusted_router.detached_jws import canonical
    from trusted_router.storage_models import generation_id_for_authorization

    body,auth,key = prepare(env)
    store,db,rt,cfg = env
    repair = json.loads(row_for(env,body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg.release = 'a'*40
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
    # Persist the real server nonce as authorize does (prepare's PR-C helper
    # only attaches its nonce to the returned detached authorization).
    payload = json.loads(db.gateway_authorizations[auth.id]['payload'])
    payload['invocation_nonce'] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]['payload'] = json.dumps(payload)
    endpoints = {c['endpoint_id']:endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS,endpoint.model_id,Model(id=endpoint.model_id,name='shadow',provider=endpoint.provider,context_length=1000000,prepaid_available=True))
    monkeypatch.setattr(gateway,'endpoint_for_id',endpoints.get)
    from tests.test_async_settle_shadow import FIXTURE
    envelope = copy.deepcopy(FIXTURE)
    envelope['billing_snapshot'] = body['billing_snapshot']
    envelope['terminal'] = copy.deepcopy(body['terminal'])
    envelope['terminal']['charge_micro'] = go_amount
    from trusted_router.async_settle_shadow_projection import project
    from trusted_router.billing_snapshot import canonical_hash
    snapshot = project(tuple(endpoints.values()), auth.created_at)
    envelope['billing_snapshot'] = snapshot.model_dump(mode='json')
    body['terminal']['snapshot_hash'] = canonical_hash(snapshot)
    envelope['terminal']['snapshot_hash'] = canonical_hash(snapshot)
    claims = dict(authorization_id=auth.id,generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id,key_id=auth.key_hash,invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id,billing_authority='local',journal_region=rt.region,
        epoch=rt.epoch,route_type='chat.completions',streamed=False,settle_origin='typed',snapshot_version=1,
        snapshot_hash=body['terminal']['snapshot_hash'],async_eligible=False,iss='router-fixture',aud='router-shadow',iat=NOW,exp=NOW+172800)
    envelope['billing_shadow_binding'] = signer().sign(claims,NOW)
    envelope['payload_hash'] = hashlib.sha256(canonical(envelope['terminal'])).hexdigest()
    del envelope['billing_snapshot']
    from tests.test_async_settle_shadow_accounting import Database
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
    evidence_store = EvidenceStore(Database())
    evidence = []
    persistence = []
    evidence_commit_counts = []
    booking_observations = []
    sample_commit_counts = []
    commits_before = db.commits
    class DetachedStore:
        def booking(self,identity,deadline):
            booking_observations.append((db.commits, db.gateway_authorizations[identity]['settled']))
            record = db.gateway_authorizations[identity]
            return Booking(record['finalized_cost_microdollars'],record['finalization_outcome'],True)
        def reserve(self,day,deadline):
            evidence_commit_counts.append(db.commits)
            return 100
        def insert_sample(self,identity,row,deadline):
            sample_commit_counts.append(db.commits)
            validate_sample(row,identity)
            evidence.append(row)
            outcome = evidence_store.insert_sample(identity,row,deadline)
            persistence.append(outcome)
            return outcome
        def flush(self,*args):
            pass
    shadow = Runtime(cfg,rt,DetachedStore())
    shadow.signer = signer()
    client = _client(cfg)
    client.app.state.async_settle_shadow = shadow
    before_auth = asdict(auth)
    try:
        first = client.post('/v1/internal/gateway/settle',json=repair,headers={'X-TR-Settlement-Shadow':wire(envelope)[0]})
        result = client.post('/v1/internal/gateway/settle',json=repair,headers={'X-TR-Settlement-Shadow':wire(envelope)[0]})
    except RuntimeError as error:
        result = error
    finally:
        shadow.executor.shutdown()
    assert not isinstance(result, RuntimeError), 'shadow changed the money response'
    assert all(count > commits_before and settled for count, settled in booking_observations)
    assert all(count > commits_before for count in sample_commit_counts)
    assert all(count > commits_before for count in evidence_commit_counts), 'evidence preceded money outcome'
    assert result.status_code == 200 and result.json()['data']['cost_microdollars'] == 2
    assert _typed_credit(db,'ws-v1')['total_usage'] == _typed_key(db,key.hash)['usage'] == 2
    assert db.reservations[auth.credit_reservation_id]['actual_micro'] == 2
    assert auth.workspace_id == before_auth['workspace_id']
    assert first.status_code == 200 and first.json()['data']['cost_microdollars'] == 2
    assert [(row['classification'],row['booked_micro'],row['go_micro']) for row in evidence] == [
        ('exact',2,2), ('exact',2,2)]
    assert persistence == ['inserted', 'duplicate']
    counter = shadow.counters.snapshot()[0][1]
    assert counter['samples_inserted'] == counter['duplicate_samples'] == 1
    assert counter['conflicting_samples'] == counter['comparison_dropped'] == 0
    assert counter['last_mismatch_at_us'] is None
    shadow.executor.shutdown()
