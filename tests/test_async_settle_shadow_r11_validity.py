"""Round-10 signed witnesses plus expiry, delivery-order and lookback axes."""
import copy
import datetime as dt
import hashlib
import json
from dataclasses import replace

import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
from tests.test_async_settle_shadow_accounting import Database, synthetic_window
from tests.test_async_settle_shadow_r7_retry_original import add_retry
from trusted_router.async_settle_shadow_binding import LIFETIME, verify_binding
from trusted_router.async_settle_shadow_compare import Booking, compare
from trusted_router.async_settle_shadow_evidence import (
    COUNTER,
    SAMPLE,
    retry_classification,
    sample,
)
from trusted_router.detached_jws import canonical
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_models import generation_id_for_authorization


def signed_observation(received_us, *, phase='refund', charge=0):
    """Reviewer's independent auth-prior/res-prior/nonce-prior signed envelope."""
    issued = received_us // 1000000
    value = copy.deepcopy(FIXTURE)
    claims = verify_binding(value['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
    claims.update(iat=issued, exp=issued+LIFETIME, authorization_id='auth-prior',
        generation_id=generation_id_for_authorization('auth-prior'),
        reservation_id='res-prior', invocation_nonce='nonce-prior')
    value['billing_shadow_binding'] = signer().sign(claims, issued)
    value['terminal'].update(terminal_kind=phase, charge_micro=charge, authorization_id='auth-prior',
        generation_id=generation_id_for_authorization('auth-prior'), invocation_nonce='nonce-prior')
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    ctx = context(attempted_kind=phase, booking=Booking(0 if phase == 'refund' else 2,
        'refunded' if phase == 'refund' else 'settled', True))
    ctx = replace(ctx, received_at=issued, body=ctx.body.model_copy(update={'authorization_id':'auth-prior'}),
        authorization=replace(ctx.authorization, id='auth-prior', credit_reservation_id='res-prior',
            invocation_nonce='nonce-prior', created_at=dt.datetime.fromtimestamp(issued, dt.UTC).isoformat()))
    compared = compare(wire(value), ctx, [signer().trusted])
    original = sample(ctx, compared, observed_us=received_us,
        router_us=1, comparator_us=1, booking_us=1,
        instance='00000000-0000-0000-0000-000000000001', revision='a'*40)
    return value, ctx, original


def append_sample(rows, body):
    rows.append(dict(kind=SAMPLE, id=body['authorization_day']+'/'+body['authorization_id'], body=body))


@pytest.mark.parametrize('offset_us', [-1000000, -500000, 0, 500000, 999999, 1000000])
def test_signed_expiry_report_boundary(offset_us):
    rows, days, proof = synthetic_window()
    counter_row = add_retry(rows, 'refund', day_index=0)
    start = counter_row['body']['started_at_us']
    value, ctx, original = signed_observation(start - LIFETIME*1000000 + offset_us)
    assert original['classification'] == 'exact'
    append_sample(rows, original)
    validate_counter(counter_row['id'], counter_row['body'], partitions=True)
    replay = compare(wire(value), replace(ctx, received_at=start//1000000), [signer().trusted])
    expired = offset_us < 1000000
    assert replay.classification == ('unevaluable' if expired else 'exact')
    assert replay.reasons == ({'proof_expired'} if expired else set())
    result = report(rows, days, proof)
    assert result['status'] == ('BLOCKED' if expired else 'PASS')
    assert bool(result['gaps']) is expired


@pytest.mark.parametrize('offset_us', [-1, 0, 1])
def test_validator_expiry_boundary(offset_us):
    from trusted_router.async_settle_shadow_evidence import original_can_back_retry, retry_identity

    value, ctx, original = signed_observation(NOW*1000000 + 500000)
    claims = verify_binding(value['billing_shadow_binding'], [signer().trusted], NOW)
    received_us = claims.exp*1000000 + offset_us
    # The production observer passes int(received_at) to the comparator.
    replay = compare(wire(value), replace(ctx, received_at=received_us//1000000), [signer().trusted])
    identity = retry_identity(original)
    valid = original_can_back_retry(identity, original['observed_at_us'], identity, received_us, received_us)
    assert valid is (offset_us < 0)
    assert valid is (replay.classification == 'exact')
    assert replay.reasons == (set() if valid else {'proof_expired'})


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('receipts', [(NOW, NOW+1), (NOW-1, NOW+LIFETIME-2), (NOW, NOW)])
def test_same_signed_payload_delayed_observation(reverse, receipts, shadow_deadline_clock):
    observations = []
    for received in receipts:
        ctx = replace(context(), received_at=received)
        compared = compare(wire(), ctx, [signer().trusted])
        assert compared.classification == 'exact'
        observations.append(sample(ctx, compared, observed_us=received*1000000,
            router_us=1, comparator_us=1, booking_us=1,
            instance='00000000-0000-0000-0000-000000000001', revision='a'*40))
    assert observations[0]['payload_hash'] == observations[1]['payload_hash']
    if reverse:
        observations.reverse()
    db = Database()
    store = EvidenceStore(db)
    identity = observations[0]['authorization_day']+'/'+observations[0]['authorization_id']
    assert store.insert_sample(identity, observations[0], shadow_deadline_clock.monotonic()+1) == 'inserted'
    assert retry_classification(*observations) == 'duplicate'
    try:
        result = store.insert_sample(identity, observations[1], shadow_deadline_clock.monotonic()+1)
    except ValueError as exc:
        result = str(exc)
    assert result == 'duplicate', 'delivery order must not expire two valid identical observations'
    assert json.loads(db.rows[SAMPLE, identity]) == observations[0]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('offset_us', [-500000, 0, 500000, 1000000])
def test_pair_expiry_is_symmetric(reverse, offset_us):
    _, _, original = signed_observation(NOW*1000000+offset_us)
    retry = copy.deepcopy(original)
    retry['observed_at_us'] = (NOW+LIFETIME)*1000000
    pair = [original, retry]
    if reverse:
        pair.reverse()
    assert retry_classification(*pair) == ('duplicate' if offset_us >= 1000000 else 'proof_expired')


@pytest.mark.parametrize('day_offset', [-2, -1, 0, 8])
@pytest.mark.parametrize('source', ['sample', 'counter'])
@pytest.mark.parametrize('reverse', [False, True])
def test_lookback_mismatch_requires_resolution(day_offset, source, reverse):
    rows, days, proof = synthetic_window()
    received_us = (1791244800 + day_offset*86400 + 1)*1000000
    _, _, mismatch = signed_observation(received_us, phase='settle', charge=999)
    assert mismatch['classification'] == 'evaluator_disagreement'
    assert (mismatch['python_micro'], mismatch['legacy_frozen_micro'], mismatch['booked_micro'], mismatch['go_micro']) == (2, 2, 2, 999)
    if source == 'sample':
        append_sample(rows, mismatch)
    else:
        row = copy.deepcopy(next(r for r in rows if r['kind'] == COUNTER))
        day = mismatch['authorization_day']
        row['body'].update(instance='00000000-0000-0000-0000-000000000002',
            started_at_us=received_us-1000000, flushed_at_us=received_us+1000000,
            last_mismatch_at_us=received_us)
        row['id'] = day+'/'+row['body']['instance']
        rows.append(row)
    if reverse:
        rows.reverse()
    output = report(rows, days, proof)
    assert output['status'] == 'BLOCKED'
    assert output['continuous_seconds'] == 0
    assert output['resets'] == [dict(at_us=received_us, revision='a'*40,
        reason='evaluator_disagreement' if source == 'sample' else 'counter_mismatch')]
