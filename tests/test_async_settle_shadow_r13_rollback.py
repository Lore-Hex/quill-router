"""Round-12 daily signed rollback witness and fleet-wide resolution matrix."""
import copy
import datetime as dt
import hashlib
from dataclasses import replace

import pytest

from scripts.async_settle.shadow_report import report
from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
from tests.test_async_settle_shadow_accounting import synthetic_window
from tests.test_async_settle_shadow_r11_validity import append_sample, signed_observation
from trusted_router.async_settle_shadow_binding import LIFETIME, verify_binding
from trusted_router.async_settle_shadow_compare import compare
from trusted_router.async_settle_shadow_evidence import CONTROL, COUNTER, SAMPLE, sample
from trusted_router.detached_jws import canonical
from trusted_router.storage_models import generation_id_for_authorization

START = 1791244800_000000
DAY = 86400_000000
A, B, C = ('a'*40, 'b'*40, 'c'*40)


def seal(rows, proof):
    for row in rows:
        if row['kind'] == CONTROL:
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()


def daily_window():
    """Reviewer's eight independently signed exact terminals and balanced counters."""
    rows, days, proof = synthetic_window()
    rows = [row for row in rows if row['kind'] != SAMPLE]
    template = copy.deepcopy(rows[0]['body'])
    for n, _day in enumerate(days):
        issued = START//1000000 + n*86400
        auth = f'auth-day-{n}'
        value = copy.deepcopy(FIXTURE)
        claims = verify_binding(value['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
        claims.update(iat=issued, exp=issued+LIFETIME, authorization_id=auth,
            generation_id=generation_id_for_authorization(auth))
        value['billing_shadow_binding'] = signer().sign(claims, issued)
        value['terminal'].update(authorization_id=auth, generation_id=generation_id_for_authorization(auth))
        value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
        ctx = context()
        ctx = replace(ctx, received_at=issued+1, body=ctx.body.model_copy(update={'authorization_id': auth}),
            authorization=replace(ctx.authorization, id=auth, created_at=dt.datetime.fromtimestamp(issued, dt.UTC).isoformat()))
        compared = compare(wire(value), ctx, [signer().trusted])
        assert compared.classification == 'exact'
        counter = rows[n*2]['body']
        for field in ('counts', 'terminal_counts', 'comparison_attempts', 'samples_inserted', 'admission_observer'):
            counter[field] = copy.deepcopy(template[field])
        counter['router_revision'] = B
        rows[n*2+1]['body']['router_revisions'] = [B]
        observed = sample(ctx, compared, observed_us=(issued+1)*1000000,
            router_us=1, comparator_us=1, booking_us=1, instance=counter['instance'], revision=B,
            admission=dict(prediction='yes', reason='eligible', tier=2, pending_micro=0, cap_micro=25000000,
                workspace_age_us=1000, health_age_us=1000, health_p95_us=0))
        append_sample(rows, observed)
    return rows, days, proof


def add_mismatch(rows, proof, *, source='sample', offset=-1, resolution='valid'):
    at = START + offset*DAY + 1000000
    _, _, bad = signed_observation(at, phase='settle', charge=999)
    if source == 'sample':
        append_sample(rows, bad)
    else:
        counter = copy.deepcopy(rows[0])
        counter['body'].update(instance='00000000-0000-0000-0000-000000000002', router_revision=A,
            started_at_us=at-1000000, flushed_at_us=at+1000000, last_mismatch_at_us=at)
        counter['id'] = bad['authorization_day']+'/'+counter['body']['instance']
        rows.append(counter)
    since = START + max(0, offset+1)*DAY
    if resolution != 'absent':
        proof['resolved_mismatches'] = [dict(at_us=at, revision=C if resolution == 'wrong' else A,
            fixed_revision=A if resolution == 'same' else B, serving_since_us=since, artifact_sha256='d'*64)]
    return at, since


def rollback(rows, day_index, *, source='all', region='same', revision=A):
    """A-only evidence may contradict otherwise complete B evidence; never discard it."""
    start = START + day_index*DAY
    for row in rows:
        body = row['body']
        if row['kind'] == SAMPLE and body['observed_at_us'] >= start and body['deployment']['router_revision'] == B:
            if source in ('all', 'sample'):
                body['deployment']['router_revision'] = revision
                if region == 'other':
                    body['deployment']['region'] = 'europe-west1'
        elif row['kind'] == COUNTER and body['started_at_us'] >= start:
            if source in ('all', 'counter'):
                body['router_revision'] = revision
                if region == 'other':
                    body['region'] = 'europe-west1'
        elif row['kind'] == CONTROL and body['admission_disabled_from_us'] >= start:
            if source in ('all', 'manifest'):
                body['router_revisions'] = [revision]
    return start + (1000000 if source == 'sample' else 0)


@pytest.mark.parametrize('day_index', [1, 4])
@pytest.mark.parametrize('source', ['all', 'sample', 'counter', 'manifest'])
@pytest.mark.parametrize('region', ['same', 'other'])
@pytest.mark.parametrize('reverse', [False, True])
def test_rollback_invalidates_resolution(day_index, source, region, reverse):
    rows, days, proof = daily_window()
    at, _ = add_mismatch(rows, proof)
    instant = rollback(rows, day_index, source=source, region=region)
    seal(rows, proof)
    if reverse:
        rows.reverse()
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert result['continuous_seconds'] == 0 and result['clean_window_start_us'] is None
    assert result['resets'] == [dict(at_us=at, revision=A, reason='evaluator_disagreement',
        invalidated_at_us=instant, resolved_at_us=START, fixed_revision=B),
        dict(at_us=instant, revision=A, reason='revision_rollback')]
    assert result['distinct_samples'] == 8


@pytest.mark.parametrize('offset', [-3, -1, 0, 8])
@pytest.mark.parametrize('source', ['sample', 'counter'])
@pytest.mark.parametrize('resolution', ['absent', 'same', 'wrong', 'valid'])
@pytest.mark.parametrize('rollback_day', [None, 1, 4])
def test_reset_resolution_rollback_matrix(offset, source, resolution, rollback_day):
    rows, days, proof = daily_window()
    _, since = add_mismatch(rows, proof, source=source, offset=offset, resolution=resolution)
    if rollback_day is not None:
        instant = rollback(rows, max(0, offset+1)+rollback_day)
    seal(rows, proof)
    if resolution == 'same':
        with pytest.raises(ValueError, match='reset resolution schema'):
            report(rows, days, proof)
        return
    result = report(rows, days, proof)
    clean = resolution == 'valid' and offset < 0 and rollback_day is None
    assert result['status'] == ('PASS' if clean else 'BLOCKED')
    rollbacks = [reset for reset in result['resets'] if reset['reason'] == 'revision_rollback']
    expected = resolution == 'valid' and offset < 8 and rollback_day is not None
    assert rollbacks == ([dict(at_us=instant, revision=A, reason='revision_rollback')] if expected else [])
    assert result['continuous_seconds'] == (691199 if clean else 604799 if resolution == 'valid' and offset == 0 and rollback_day is None else 0)
    assert since > START + offset*DAY + 1000000


@pytest.mark.parametrize('source', ['sample', 'counter', 'manifest'])
@pytest.mark.parametrize('requested_start', [0, 3])
def test_rollback_outside_requested_days_is_retained(source, requested_start):
    rows, days, proof = daily_window()
    add_mismatch(rows, proof)
    instant = rollback(rows, 1, source=source)
    seal(rows, proof)
    # Lookback-only rollback, or evidence after the requested window.
    selected = days[requested_start:] if requested_start else days[:1]
    result = report(rows, selected, proof)
    assert result['status'] == 'BLOCKED' and result['continuous_seconds'] == 0
    assert result['resets'][-1] == dict(at_us=instant, revision=A, reason='revision_rollback')


def test_reviewed_successor_and_predecessor_order():
    rows, days, proof = daily_window()
    at, _ = add_mismatch(rows, proof)
    # Reviewed predecessor C -> A, then A -> B. Hexical SHA order means nothing.
    proof['resolved_mismatches'].append(dict(at_us=at-DAY, revision=C, fixed_revision=A,
        serving_since_us=at-1000000, artifact_sha256='e'*64))
    instant = rollback(rows, 4, revision=C)
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['resets'][-1] == dict(at_us=instant, revision=C, reason='revision_rollback')
    assert result['status'] == 'BLOCKED' and result['continuous_seconds'] == 0


def test_unknown_revision_cannot_prove_fix_retention():
    rows, days, proof = daily_window()
    add_mismatch(rows, proof)
    instant = rollback(rows, 4, revision=C)
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['resets'][-1] == dict(at_us=instant, revision=C, reason='revision_rollback')
    assert result['status'] == 'BLOCKED'


def test_rollback_confined_to_one_region_with_complete_roster():
    rows, days, proof = daily_window()
    add_mismatch(rows, proof)
    for n, day in enumerate(days):
        # A second, idle but fully accounted writer stays on B until day four.
        idle_rows, _, _ = synthetic_window()
        idle = copy.deepcopy(idle_rows[n*2])
        if n == 0:
            empty = idle_rows[2]['body']
            for field in ('counts', 'terminal_counts', 'comparison_attempts', 'samples_inserted', 'admission_observer'):
                idle['body'][field] = copy.deepcopy(empty[field])
        boot = '00000000-0000-0000-0000-000000000003'
        revision = A if n >= 4 else B
        idle['id'] = day+'/'+boot
        idle['body'].update(instance=boot, region='europe-west1', router_revision=revision)
        rows.append(idle)
        manifest = rows[n*2+1]['body']
        manifest['router_revisions'] = sorted({B, revision})
        manifest['instance_boot_ids'] = sorted([*manifest['instance_boot_ids'], boot])
        proof['instance_boot_ids_by_day'][day] = manifest['instance_boot_ids']
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['gaps'] == []
    assert result['status'] == 'BLOCKED' and result['continuous_seconds'] == 0
    assert result['resets'][-1] == dict(at_us=START+4*DAY, revision=A, reason='revision_rollback')


@pytest.mark.parametrize('reviewed', [False, True])
def test_rollback_requires_new_reviewed_resolution(reviewed):
    rows, days, proof = daily_window()
    add_mismatch(rows, proof)
    instant = rollback(rows, 1)
    # Restore B after one day on A, retaining the durable rollback history.
    for row in rows:
        body = row['body']
        if row['kind'] == SAMPLE and body['observed_at_us'] >= START+2*DAY:
            body['deployment']['router_revision'] = B
        elif row['kind'] == COUNTER and body['started_at_us'] >= START+2*DAY:
            body['router_revision'] = B
        elif row['kind'] == CONTROL and body['admission_disabled_from_us'] >= START+2*DAY:
            body['router_revisions'] = [B]
    if reviewed:
        proof['resolved_mismatches'].append(dict(at_us=instant, revision=A, fixed_revision=B,
            serving_since_us=START+2*DAY, artifact_sha256='e'*64))
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert result['continuous_seconds'] == (518399 if reviewed else 0)
    assert result['clean_window_start_us'] == (START+2*DAY+1000000 if reviewed else None)


@pytest.mark.parametrize('source', ['sample', 'counter'])
def test_rollback_on_fix_day_uses_first_evidence_instant(source):
    rows, days, proof = daily_window()
    add_mismatch(rows, proof)
    instant = rollback(rows, 0, source=source)
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['resets'][-1] == dict(at_us=instant, revision=A, reason='revision_rollback')
    assert result['status'] == 'BLOCKED'


def test_reviewed_successor_retains_fix():
    rows, days, proof = daily_window()
    at, _ = add_mismatch(rows, proof)
    # A separate reviewed B -> C fix establishes C as a successor, without
    # relying on lexical SHA order or first-seen row order.
    proof['resolved_mismatches'].append(dict(at_us=at+1000000, revision=B, fixed_revision=C,
        serving_since_us=START+4*DAY, artifact_sha256='e'*64))
    rollback(rows, 4, revision=C)
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['status'] == 'PASS' and result['continuous_seconds'] == 691199
    assert all(reset['reason'] != 'revision_rollback' for reset in result['resets'])


def test_cyclic_revision_order_cannot_relabel_bad_build_as_successor():
    rows, days, proof = daily_window()
    at, _ = add_mismatch(rows, proof)
    proof['resolved_mismatches'].append(dict(at_us=at+1000000, revision=B, fixed_revision=A,
        serving_since_us=START+4*DAY, artifact_sha256='e'*64))
    rollback(rows, 4)
    seal(rows, proof)
    with pytest.raises(ValueError, match='cyclic reset revision order'):
        report(rows, days, proof)
