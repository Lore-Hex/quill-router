"""Round-9 reviewer witnesses: signed dimensions and binding validity."""
import copy
import datetime as dt
import json

import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow_accounting import synthetic_window
from tests.test_async_settle_shadow_r7_retry_original import add_retry
from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE


@pytest.mark.parametrize('damage', ['excess_duplicates', 'retired_region'])
def test_required_impossible_retry_counts(damage):
    rows, days, proof = synthetic_window()
    counter = add_retry(rows, 'settle')
    if damage == 'excess_duplicates':
        counter['body']['duplicate_samples'] += 1
        counter['body']['terminal_counts'][0]['duplicate_samples'] += 1
    else:
        original = copy.deepcopy(next(r for r in rows if r['kind'] == SAMPLE))
        original['id'] = '2026-09-01/retired-original'
        original['body'].update(authorization_id='retired-original', authorization_day='2026-09-01',
            authorize_at_us=int(dt.datetime(2026,9,1,tzinfo=dt.UTC).timestamp()*1e6),
            observed_at_us=int(dt.datetime(2026,10,5,tzinfo=dt.UTC).timestamp()*1e6))
        original['body']['deployment']['region'] = 'other-region'
        counter['body']['region'] = 'other-region'
        rows.append(original)
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'

def test_duplicate_cannot_change_signed_stream_dimension():
    rows, days, proof = synthetic_window()
    counter_row = next(r for r in rows if r['kind'] == COUNTER)
    before = copy.deepcopy(counter_row['body'])
    add_retry(rows, 'settle', day_index=0)
    counter = counter_row['body']
    src = next(b for b in counter['counts'] if (b['adapter'],b['route_type'],b['streamed']) == ('openai','chat.completions',False))
    old = next(b for b in before['counts'] if (b['adapter'],b['route_type'],b['streamed']) == ('openai','chat.completions',False))
    dst = next(b for b in counter['counts'] if (b['adapter'],b['route_type'],b['streamed']) == ('openai','chat.completions',True))
    for k in src:
        if k not in ('adapter','route_type','streamed'):
            dst[k] += src[k] - old[k]
            src[k] = old[k]
    phase = counter['terminal_counts'][0]
    prior = before['terminal_counts'][0]
    extra = copy.deepcopy(phase)
    for k in phase:
        if k not in ('adapter','route_type','streamed','phase'):
            extra[k] -= prior[k]
            phase[k] = prior[k]
    extra['streamed'] = True
    counter['terminal_counts'].append(extra)
    validate_counter(counter_row['id'],counter,partitions=True)
    assert all(r['body']['streamed'] is False for r in rows if r['kind'] == SAMPLE)
    # Produce both valid signed terminal observations and ask the real adapter.
    import hashlib
    import time
    from dataclasses import replace

    from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
    from tests.test_async_settle_shadow_accounting import Database
    from trusted_router.async_settle_shadow_binding import verify_binding
    from trusted_router.async_settle_shadow_compare import compare
    from trusted_router.async_settle_shadow_evidence import sample
    from trusted_router.detached_jws import canonical
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
    value = copy.deepcopy(FIXTURE)
    claims = verify_binding(value['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
    claims['streamed'] = True
    value['billing_shadow_binding'] = signer().sign(claims, NOW)
    value['terminal']['streamed'] = True
    value['observed']['streamed'] = True
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    ctx = context()
    ctx = replace(ctx, body=ctx.body.model_copy(update={'streamed':True}))
    comparison = compare(wire(value),ctx,[signer().trusted])
    assert comparison.classification == 'exact'
    original = next(r['body'] for r in rows if r['kind'] == SAMPLE)
    retry = sample(ctx,comparison,observed_us=NOW*1000000,router_us=1,comparator_us=1,
        booking_us=1,instance=original['deployment']['instance'],revision='a'*40)
    assert retry['streamed'] is True and original['streamed'] is False
    assert retry['payload_hash'] != original['payload_hash']
    db = Database()
    identity = original['authorization_day']+'/'+original['authorization_id']
    db.rows[SAMPLE,identity] = json.dumps(original)
    actual = EvidenceStore(db).insert_sample(identity,retry,time.monotonic()+1)
    assert actual == 'conflict'
    result = report(rows,days,proof)
    assert result['status'] == 'BLOCKED', 'A verified identical terminal cannot switch its signed streamed field'

def test_duplicate_cannot_outlive_binding():
    rows, days, proof = synthetic_window()
    counter_row = add_retry(rows, 'settle')
    validate_counter(counter_row['id'],counter_row['body'],partitions=True)
    original = next(r['body'] for r in rows if r['kind'] == SAMPLE)
    # Even the latest possible issuance before the original observation has
    # expired before this last-day writer starts; expiry is exactly 48 hours.
    assert original['observed_at_us'] + 172800_000000 < counter_row['body']['started_at_us']
    from dataclasses import replace

    from tests.test_async_settle_shadow import NOW, context, signer, wire
    from trusted_router.async_settle_shadow_compare import compare
    compared = compare(wire(),replace(context(),received_at=NOW+7*86400),[signer().trusted])
    assert compared.reasons == {'proof_expired'} and not compared.binding_verified
    result = report(rows,days,proof)
    assert result['status'] == 'BLOCKED', 'No retained binding could be valid for this duplicate'


@pytest.mark.parametrize('phase', ['settle', 'refund'])
@pytest.mark.parametrize('offset,expected', [(-1, False), (0, True), (1, True)])
def test_report_binding_validity_lower_boundary(phase, offset, expected):
    from tests.test_async_settle_shadow_r8_retry_class import observation

    rows, days, proof = synthetic_window()
    # Keep the positive seed in the other phase, so only our lookback original
    # can back the retry. It was observed outside the requested metric window.
    if phase == 'settle':
        for row in rows:
            if row['kind'] == SAMPLE:
                row['body']['booking'].update(attempted_kind='refund', outcome='refunded')
            elif row['kind'] == COUNTER:
                for bucket in row['body']['counts']:
                    bucket['refund_attempts'], bucket['settle_attempts'] = bucket['settle_attempts'], 0
                for bucket in row['body']['terminal_counts']:
                    bucket['phase'] = 'refund'
    counter = add_retry(rows, phase, day_index=0)['body']
    original = observation(phase)
    observed = counter['started_at_us'] - 172800_000000 + offset
    day = dt.datetime.fromtimestamp(observed / 1000000, dt.UTC).date().isoformat()
    original.update(authorization_id='boundary', authorization_day=day,
                    authorize_at_us=observed, observed_at_us=observed)
    rows.append(dict(kind=SAMPLE, id=day + '/boundary', body=original))
    result = report(rows, days, proof)
    assert result['status'] == ('PASS' if expected else 'BLOCKED')
    assert bool(result['gaps']) is not expected


@pytest.mark.parametrize('offset,expected', [(-172800_000001, False),
    (-172800_000000, True), (-172799_999999, True), (0, True), (1, True), (2, False)])
def test_shared_original_window_boundaries(offset, expected):
    from trusted_router.async_settle_shadow_evidence import RetryIdentity, original_can_back_retry

    identity = RetryIdentity('settle', 'us-central1', '2026-10-06', 'openai', 'chat.completions', False)
    start = 200000_000000
    assert original_can_back_retry(identity, start + offset, identity, start, start + 1) is expected
