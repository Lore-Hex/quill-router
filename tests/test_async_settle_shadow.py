"""Adversarial dormant shadow contract; every guard includes a failing witness."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_async_settle_ticket import authorization, endpoint, runtime
from tests.test_authorize_hold_time import _ast_sha256
from trusted_router import billing_snapshot as b
from trusted_router.async_settle_shadow_binding import (
    FIXTURE_SHA256,
    PURPOSE,
    ShadowSigner,
    verify_binding,
)
from trusted_router.async_settle_shadow_compare import Booking, Context, compare, legacy_oracle
from trusted_router.async_settle_shadow_evidence import (
    Counters,
    dimensions,
    sample,
    validate_sample,
)
from trusted_router.async_settle_shadow_projection import project
from trusted_router.async_settle_shadow_wire import Rejection, parse_header, snapshot_from_object
from trusted_router.async_settle_ticket import TicketSigner, verify_lookup_ticket, verify_ticket
from trusted_router.config import Settings
from trusted_router.detached_jws import TrustedKey, b64encode, canonical
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.services.async_settle_shadow import Runtime

FIXTURE_PATH = Path(__file__).parent/'fixtures/async_settlement/shadow_v1.json'
FIXTURE = json.loads(FIXTURE_PATH.read_bytes())
NOW = 1791244801


def signer():
    private = Ed25519PrivateKey.from_private_bytes(bytes([1])*32)
    return ShadowSigner(TicketSigner(private, TrustedKey('shadow-v1-fixture', 'async-settle-ticket',
        b64encode(private.public_key().public_bytes_raw()), 'router-fixture', 'router-settlement')))


def wire(value=None):
    return (b64encode(canonical(FIXTURE if value is None else value)),)


def context(**changes):
    auth = authorization()
    auth.created_at = '2026-10-06T00:00:00+00:00'
    auth.candidate_endpoint_ids = [endpoint().id]
    auth.endpoint_id = endpoint().id
    body = GatewaySettleRequest(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1,
                               selected_endpoint=endpoint().id, route_type='chat.completions')
    snapshot = snapshot_from_object(FIXTURE['billing_snapshot'])
    base = Context(auth, body, 'settle', endpoint().id, 'us-central1', 1, NOW, Booking(2, 'settled', True),
                   lambda: snapshot, True, 'catalog_at_authorize_time')
    return replace(base, **changes)


def resign(value):
    claims = verify_binding(FIXTURE['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
    claims['snapshot_hash'] = b.canonical_hash(snapshot_from_object(value['billing_snapshot']))
    value['billing_shadow_binding'] = signer().sign(claims, NOW)
    if value['terminal']:
        value['terminal']['snapshot_hash'] = claims['snapshot_hash']
        value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    return value


def test_literal_and_separate_purpose():
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert len(canonical(FIXTURE)) == 2576 and len(wire()[0]) == 3435
    claims = verify_binding(FIXTURE['billing_shadow_binding'], [signer().trusted], NOW)
    assert claims.exp - claims.iat == 172800 and claims.async_eligible is False
    assert signer().sign(claims.model_dump(), NOW) == FIXTURE['billing_shadow_binding']
    assert signer().trusted.purpose == PURPOSE
    for verifier in (lambda: verify_ticket(FIXTURE['billing_shadow_binding'], [signer().trusted], claims.model_dump(), NOW),
                     lambda: verify_lookup_ticket(FIXTURE['billing_shadow_binding'], [signer().trusted], NOW)):
        with pytest.raises(ValueError):
            verifier()
    assert b.canonical_hash(parse_header(wire()).terminal) == FIXTURE['payload_hash']
    assert compare(wire(), context(), [signer().trusted]).classification == 'exact'


@pytest.mark.parametrize('raw', ['', ' ', 'ws-v1, ws-v1', 'a,b'])
def test_setting_immutable(raw):
    config = Settings(environment='test', async_settle_shadow_workspaces=raw)
    assert config.async_settle_shadow_workspace_ids == frozenset(v.strip() for v in raw.split(',') if v.strip())
    with pytest.raises(AttributeError):
        config.async_settle_shadow_workspace_ids.add('other')


@pytest.mark.parametrize('raw', ['*', 'ws a', 'a'*65, ','.join(f'w{i}' for i in range(33)), 'é'])
def test_setting_invalid(raw):
    with pytest.raises(ValueError):
        Settings(environment='test', async_settle_shadow_workspaces=raw)


@pytest.mark.parametrize(('headers', 'reason'), [
    (('a', 'b'), 'header_duplicate'), (('a'*12289,), 'header_size'),
    (('abc=',), 'base64'), (('a',), 'base64'),
    ((b64encode(b'\xef\xbb\xbf{}'),), 'json_encoding'),
    ((b64encode(b'{"v":1,"v":1}'),), 'json_duplicate'),
    ((b64encode(b'[]'),), 'json_shape'),
    ((b64encode(b'{"v":1.0}'),), 'integer'),
    ((b64encode(b'{"v":NaN}'),), 'integer'),
    ((b64encode(b'{"v":999999999999999999999999999}'),), 'integer'),
    ((b64encode(b'{"v":"\\ud800"}'),), 'json_encoding'),
    ((b64encode(b'['*17+b']'*17),), 'json_shape'),
    ((b64encode(b' '*8193),), 'header_size'),
])
def test_parser_refusals(headers, reason):
    with pytest.raises(Rejection, match=reason):
        parse_header(headers)


@pytest.mark.parametrize('field', ['v', 'handoff_prepare_us'])
@pytest.mark.parametrize('bad', [True, '1', 1.5, -1])
def test_integer_strict(field, bad):
    value = copy.deepcopy(FIXTURE)
    value[field] = bad
    with pytest.raises(Rejection):
        parse_header(wire(value))


@pytest.mark.parametrize('field', ['authorization_id', 'workspace_id', 'key_id', 'invocation_nonce', 'journal_region', 'epoch', 'route_type', 'streamed'])
def test_foreign_identity(field):
    value = copy.deepcopy(FIXTURE)
    claims = verify_binding(value['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
    claims[field] = not claims[field] if field == 'streamed' else 2 if field == 'epoch' else 'responses' if field == 'route_type' else 'other'
    if field == 'authorization_id':
        from trusted_router.storage_models import generation_id_for_authorization
        claims['generation_id'] = generation_id_for_authorization(claims[field])
    value['billing_shadow_binding'] = signer().sign(claims, NOW)
    assert compare(wire(value), context(), [signer().trusted]).classification == 'identity'


def test_signed_hash_not_self_hash():
    value = copy.deepcopy(FIXTURE)
    value['billing_snapshot']['candidates'][0]['rates']['input_micro_per_million'] += 1
    value['terminal']['snapshot_hash'] = b.canonical_hash(snapshot_from_object(value['billing_snapshot']))
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    assert compare(wire(value), context(), [signer().trusted]).classification == 'hash'
    value = copy.deepcopy(FIXTURE)
    value['payload_hash'] = '0'*64
    assert compare(wire(value), context(), [signer().trusted]).classification == 'hash'


def test_hash_only_success_and_corrected_failure():
    value = copy.deepcopy(FIXTURE)
    del value['billing_snapshot']
    success = compare(wire(value), context(), [signer().trusted])
    assert (success.classification, success.s0_reconstruction, success.legacy_frozen_micro) == ('exact', 'verified', 2)
    for rebuild in (None, lambda: project((endpoint(prompt_price_microdollars_per_million_tokens=1500000),), context().authorization.created_at)):
        result = compare(wire(value), context(rebuild=rebuild, booking=Booking(3, 'settled', True)), [signer().trusted])
        assert result.classification == 'unevaluable' and result.reasons == {'snapshot_reconstruction_failed'}
        assert result.python_micro is result.legacy_frozen_micro is result.booked_minus_frozen is None
        assert result.payload_hash == value['payload_hash']


@pytest.mark.parametrize('booked', [2, 3, 4])
def test_catalog_explanation(booked):
    rebuilt = project((endpoint(prompt_price_microdollars_per_million_tokens=1500000),), context().authorization.created_at)
    result = compare(wire(), context(rebuild=lambda: rebuilt, booking=Booking(booked, 'settled', True)), [signer().trusted])
    assert result.classification == {2:'exact', 3:'explained-by-catalog-change', 4:'evaluator_disagreement'}[booked]
    assert (result.legacy_frozen_micro, result.python_micro, result.go_micro, result.rebuilt_micro) == (2, 2, 2, 3)
    assert result.booked_minus_frozen == booked-2


def test_missing_or_corrupt_independent_oracle(monkeypatch):
    import trusted_router.async_settle_shadow_compare as module
    rebuilt = project((endpoint(prompt_price_microdollars_per_million_tokens=1500000),), context().authorization.created_at)
    ctx = context(rebuild=lambda: rebuilt, booking=Booking(3, 'settled', True))
    monkeypatch.setattr(module, 'legacy_oracle', lambda *args: (_ for _ in ()).throw(ValueError()))
    assert compare(wire(), ctx, [signer().trusted]).classification == 'requires_review'
    monkeypatch.setattr(module, 'legacy_oracle', lambda *args: (1, FIXTURE['terminal']['usage']))
    assert compare(wire(), ctx, [signer().trusted]).classification == 'evaluator_disagreement'


@pytest.mark.parametrize('attempted', ['settle', 'refund'])
@pytest.mark.parametrize('winner', ['settled', 'refunded'])
def test_refund_and_winner_polarity(attempted, winner):
    value = copy.deepcopy(FIXTURE)
    value['terminal'].update(terminal_kind=attempted, charge_micro=2 if attempted == 'settle' else 0)
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    result = compare(wire(value), context(attempted_kind=attempted, booking=Booking(2 if winner == 'settled' else 0, winner, True)), [signer().trusted])
    if (attempted, winner) in {('settle','settled'), ('refund','refunded')}:
        assert result.classification == 'exact'
        assert result.python_micro == result.go_micro == result.booked_micro == (2 if attempted == 'settle' else 0)
    else:
        assert result.classification == 'requires_review' and result.reasons == {'winner_polarity'}
        assert result.booked_minus_frozen is result.booked_minus_rebuilt is None


def test_refund_cannot_hide_go_charge():
    value = copy.deepcopy(FIXTURE)
    value['terminal']['terminal_kind'] = 'refund'
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    result = compare(wire(value), context(attempted_kind='refund', booking=Booking(0, 'refunded', True)), [signer().trusted])
    assert (result.classification, result.python_micro, result.go_micro) == ('evaluator_disagreement', 0, 2)


def test_usage_missing_and_aliases():
    ctx = context()
    ctx.body.input_tokens = 999
    assert compare(wire(), ctx, [signer().trusted]).classification == 'exact'
    ctx.body.actual_input_tokens = None
    assert compare(wire(), ctx, [signer().trusted]).classification == 'normalization'
    ctx.body.input_tokens = None
    assert compare(wire(), ctx, [signer().trusted]).reasons == {'usage_missing'}


def test_rate_bucket_before_work(monkeypatch):
    from trusted_router.services import async_settle_shadow as module
    now = [10.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    rt = Runtime(Settings(environment='test', async_settle_shadow_workspaces='ws-v1'), runtime())
    assert [rt.admit(1) for _ in range(13)] == [None]*10+['rate_limit']*3
    now[0] += .5
    assert rt.admit(1) is None and rt.admit(1) == 'rate_limit'
    rt.executor.shutdown()


def test_dimensions_without_samples_and_overflow():
    counters = Counters('us-central1', 'a'*40, clock=lambda: NOW)
    for dims in (dimensions('openai','responses',True), dimensions('anthropic','chat.completions',False)):
        counters.reason(dims, 'settle', 'rate_limit')
    _, row = counters.snapshot()[0]
    assert row['drops'] == [
        dict(phase='settle',adapter='openai',route_type='responses',streamed=True,reason='rate_limit',count=1),
        dict(phase='settle',adapter='anthropic',route_type='chat.completions',streamed=False,reason='rate_limit',count=1)]
    assert row['first_gap_at_us'] == NOW*1000000
    assert len(canonical(row)) <= 65536


def test_sample_exact_schema_and_no_secrets():
    ctx = context()
    result = compare(wire(), ctx, [signer().trusted])
    row = sample(ctx, result, observed_us=NOW*1000000, router_us=1, comparator_us=1,
                 booking_us=1, instance='00000000-0000-0000-0000-000000000001', revision='a'*40)
    validate_sample(row, '2026-10-06/auth-v1')
    assert row['legacy_frozen_micro'] == 2 and row['timing']['evidence_write_us'] is None
    assert 'billing_shadow_binding' not in canonical(row).decode()
    assert 'ws-v1' not in canonical(row).decode()
    row['booked_minus_frozen'] = 100
    with pytest.raises(ValueError, match='delta'):
        validate_sample(row, '2026-10-06/auth-v1')


def test_frozen_source_and_ast_pins():
    manifest = json.loads((Path(__file__).parent/'fakes/shadow_main_manifest.json').read_bytes())
    for row in manifest['sources'].values():
        raw = Path(row['fixture']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row['sha256']
        assert _ast_sha256(ast.parse(raw)) == row['ast_sha256']


def test_independent_mapping_fields():
    candidate = snapshot_from_object(FIXTURE['billing_snapshot']).candidates[0]
    amount, usage = legacy_oracle(candidate, context().body, 'settle')
    assert amount == 2 and usage == FIXTURE['terminal']['usage']
    changed = candidate.model_copy(update={'rates': candidate.rates.model_copy(update={'input_micro_per_million':1500000})})
    assert legacy_oracle(changed, context().body, 'settle')[0] == 3


def test_authorize_shadow_signing_no_ticket_api(monkeypatch):
    config = Settings(environment='test', async_settle_shadow_workspaces='ws-v1')
    rt = Runtime(config, runtime())
    rt.signer = signer()
    monkeypatch.setattr(TicketSigner, 'sign', lambda *args: pytest.fail('ticket capability minted'))
    additions = {'billing_snapshot':FIXTURE['billing_snapshot'], 'billing_snapshot_hash':FIXTURE['terminal']['snapshot_hash']}
    rt.authorize(authorization(), additions, replay=False, header=True, route='chat.completions', streamed=False, endpoints=[endpoint()])
    assert 'billing_shadow_binding' in additions and 'settlement_ticket' not in additions
    for ws in ('', 'nonmember'):
        config = Settings(environment='test', async_settle_shadow_workspaces=ws)
        rt.settings = config
        additions = {}
        before = copy.deepcopy(rt.counters.days)
        rt.authorize(authorization(), additions, replay=False, header=True, route='chat.completions', streamed=False, endpoints=[])
        assert additions == {} and rt.counters.days == before
    rt.executor.shutdown()


def test_go_charge_999_is_diagnostic():
    value = copy.deepcopy(FIXTURE)
    value['terminal']['charge_micro'] = 999
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    result = compare(wire(value),context(),[signer().trusted])
    assert (result.classification,result.booked_micro,result.python_micro,result.go_micro) == ('evaluator_disagreement',2,2,999)


def test_two_evaluators_corrupt_rounding(monkeypatch):
    import trusted_router.async_settle_shadow_compare as module
    original = module.b.evaluate
    def corrupt(*args):
        correct = original(*args)
        return correct.model_copy(update={'charge_micro':1}) if args[0].candidates[0].rates.input_micro_per_million == 500000 else correct
    monkeypatch.setattr(module.b,'evaluate',corrupt)
    value = copy.deepcopy(FIXTURE)
    value['terminal']['charge_micro'] = 1
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    rebuilt = project((endpoint(prompt_price_microdollars_per_million_tokens=1000000,
                                 completion_price_microdollars_per_million_tokens=1000000),),context().authorization.created_at)
    result = compare(wire(value),context(rebuild=lambda:rebuilt),[signer().trusted])
    assert result.python_micro == result.go_micro == 1 and result.legacy_frozen_micro == 2
    assert result.classification == 'evaluator_disagreement'


def test_optimized_projection_matches_builder():
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, effective_endpoint
    for e in MODEL_ENDPOINTS.values():
        if e.provider not in {'openai','anthropic'} or e.usage_type != 'Credits' or not MODELS[e.model_id].supports_chat:
            continue
        at = context().authorization.created_at
        try:
            expected = b.build_snapshot([effective_endpoint(e,at=at)], b.Eligibility())
        except ValueError:
            with pytest.raises(ValueError):
                project((e,),at)
        else:
            assert b.canonical_bytes(project((e,),at)) == b.canonical_bytes(expected)


def test_transport_measured_shapes():
    from scripts.async_settle.shadow_benchmark import cases
    measured = list(cases())
    assert [(n,sizes['snapshot']) for n,_,_,sizes in measured] == [(1,1041),(4,3563),(43,28821)]
    assert measured[1][3] == dict(snapshot=3563,decoded=5492,encoded=7323,transmitted_decoded=5492,transmitted_encoded=7323)
    assert 'billing_snapshot' not in measured[2][1]
    for _,value,ctx,_ in measured:
        result = compare(wire(value),ctx,[signer().trusted])
        assert result.classification == 'exact'
    value = json.loads((Path(__file__).parent/'fixtures/async_settlement/request_v1.json').read_bytes())
    assert len(canonical(value['billing_snapshot'])) == 645


def test_invalid_signature():
    value = copy.deepcopy(FIXTURE)
    value['billing_shadow_binding'] = '.'.join(value['billing_shadow_binding'].split('.')[:2])+'.'+b64encode(bytes(64))
    result = compare(wire(value),context(),[signer().trusted])
    assert result.classification == 'hash' and result.reasons == {'proof_signature'}


def test_hash_only_literals_and_named_projection_lengths():
    from trusted_router.catalog import MODEL_ENDPOINTS
    root = FIXTURE_PATH.parent
    only = json.loads((root/'shadow_hash_only_v1.json').read_bytes())
    corrected = json.loads((root/'shadow_hash_only_corrected_v1.json').read_bytes())
    assert set(only) == set(FIXTURE)-{'billing_snapshot'}
    assert compare(wire(only),context(),[signer().trusted]).s0_reconstruction == 'verified'
    rebuilt = snapshot_from_object(corrected['rebuilt_snapshot'])
    result = compare(wire(corrected['envelope']),context(rebuild=lambda:rebuilt),[signer().trusted])
    assert result.classification == corrected['classification'] and result.reasons == {corrected['reason']}
    models = ('openai/gpt-6-astra','openai/gpt-5.6-sol','openai/gpt-6.1-sol','openai/gpt-5.5')
    endpoints = tuple(next(e for e in MODEL_ENDPOINTS.values() if e.model_id == model and e.provider == 'openai' and e.usage_type == 'Credits') for model in models)
    assert [len(b.canonical_bytes(project(endpoints[:n],context().authorization.created_at))) for n in range(1,5)] == [1041,1884,2726,3563]


from tests.test_billing_snapshot import CASES  # noqa: E402


@pytest.mark.parametrize('case',[case for case in CASES if case['expected_exclusion'] is None],ids=lambda case:case['name'])
def test_independent_legacy_arithmetic_matrix(case):
    snapshot = snapshot_from_object(case['snapshot'])
    candidate = next(c for c in snapshot.candidates if c.endpoint_id == case['selected_endpoint'])
    raw = case['raw_usage']
    body = GatewaySettleRequest(authorization_id='auth-v1',actual_input_tokens=raw['input_tokens'],
        actual_output_tokens=raw['output_tokens'],cache_read_input_tokens=raw.get('cache_read_tokens',0),
        cache_creation_input_tokens=raw.get('cache_creation_tokens',0),reasoning_tokens=raw.get('reasoning_tokens',0))
    amount,usage = legacy_oracle(candidate,body,'settle')
    assert amount == case['expected_charge_micro'] and usage == case['expected_normalized_usage']
    assert legacy_oracle(candidate,body,'refund') == (0,usage)


def test_oracle_dependency_separation():
    import inspect
    source = ast.parse(inspect.getsource(legacy_oracle))
    calls = {ast.unparse(node.func) for node in ast.walk(source) if isinstance(node,ast.Call)}
    assert 'endpoint_cost_microdollars_from_candidate' in calls and 'normalized_prompt_accounting' in calls
    assert calls.isdisjoint({'b.evaluate','b.checked','b.validate_envelope'})


def test_no_public_openapi_change():
    from tests.test_settle_outbox_drain import _client
    off = _client(Settings(environment='test')).app.openapi()
    on = _client(Settings(environment='test',async_settle_shadow_workspaces='ws-v1')).app.openapi()
    assert on == off


def test_stage_d_document_precedes_corrected_catalog():
    changed = replace(endpoint(), prompt_price_microdollars_per_million_tokens=1500000)
    document = {'candidates': copy.deepcopy(FIXTURE['billing_snapshot']['candidates'])}
    ctx = context(rebuild=lambda: project((changed,), context().authorization.created_at, document), price_source='stage_d_document')
    result = compare(wire(), ctx, [signer().trusted])
    assert (result.classification, result.python_micro, result.rebuilt_micro, result.booked_micro) == ('exact',2,2,2)
    assert result.rebuilt_snapshot_hash == result.snapshot_hash


@pytest.mark.parametrize('rebuild_available',[False,True])
def test_unavailable_or_unproven_booking_view_cannot_explain(rebuild_available):
    changed = replace(endpoint(), prompt_price_microdollars_per_million_tokens=1500000)
    ctx = context(booking=Booking(3,'settled',True),rebuild_matches_booking_view=False,
        rebuild=(lambda:project((changed,),context().authorization.created_at)) if rebuild_available else None)
    result = compare(wire(),ctx,[signer().trusted])
    assert (result.classification,result.reasons) == ('unevaluable',{'rebuild_unavailable'})
    assert (result.legacy_frozen_micro,result.python_micro,result.go_micro,result.booked_micro) == (2,2,2,3)


@pytest.mark.parametrize('booked',[1,3])
def test_one_micro_unexplained_delta_is_not_tolerated(booked):
    result = compare(wire(),context(booking=Booking(booked,'settled',True)),[signer().trusted])
    assert (result.classification,result.booked_minus_frozen,result.booked_minus_rebuilt) == ('evaluator_disagreement',booked-2,booked-2)


def test_projection_cache_keys_include_time_catalog_and_document(monkeypatch):
    from dataclasses import replace

    from trusted_router import async_settle_shadow_projection as projection
    projection.clear_caches()
    source = endpoint()
    calls = []
    def effective(value, *, at):
        calls.append((value, at))
        return replace(value, completion_price_microdollars_per_million_tokens=1000000 if at.endswith('00Z') else 2000000)
    monkeypatch.setattr(projection, 'effective_endpoint', effective)
    try:
        early = project((source,), '2026-10-06T00:00:00Z')
        assert project((source,), '2026-10-06T00:00:00Z') is early
        later = project((source,), '2026-10-06T00:00:01Z')
        changed = project((replace(source, prompt_price_microdollars_per_million_tokens=3000000),), '2026-10-06T00:00:00Z')
        assert len(calls) == 3
        assert len({projection.snapshot_material(v)[1] for v in (early, later, changed)}) == 3
        document = {'candidates': [early.candidates[0].model_dump(mode='json')]}
        one = project((source,), '2026-10-06T00:00:00Z', document)
        document['candidates'][0]['rates']['input_micro_per_million'] += 1
        two = project((source,), '2026-10-06T00:00:00Z', document)
        assert projection.snapshot_material(one)[1] != projection.snapshot_material(two)[1]
        for value in (early, later, changed, one, two):
            assert projection.snapshot_material(value) == (b.canonical_bytes(value), b.canonical_hash(value))
    finally:
        projection.clear_caches()


@pytest.mark.parametrize('size', [6144, 6145])
def test_inline_snapshot_boundary(size):
    # Review reproduction: structurally valid candidates, with only ID length
    # varied. The decoded envelope still fits its independent 8192-byte cap.
    value = copy.deepcopy(FIXTURE)
    template = value['billing_snapshot']['candidates'][0]
    candidates = []
    for index in range(13):
        item = copy.deepcopy(template)
        item['endpoint_id'] = f'openai/e{index:02d}'
        candidates.append(item)
    value['billing_snapshot']['candidates'] = candidates
    remaining = size - len(canonical(value['billing_snapshot']))
    for item in candidates:
        extra = min(remaining, 121-len(item['endpoint_id']))
        item['endpoint_id'] += 'x'*extra
        remaining -= extra
    assert remaining == 0
    snapshot = snapshot_from_object(value['billing_snapshot'])
    assert len(b.canonical_bytes(snapshot)) == size
    assert len(canonical(value)) == size + 1931
    rejected = None
    try:
        parsed = parse_header(wire(value))
    except Rejection as error:
        rejected = str(error)
    if size == 6144:
        assert rejected is None and parsed.snapshot == snapshot
    else:
        assert rejected == 'header_size'
        del value['billing_snapshot']
        assert parse_header(wire(value)).snapshot is None
