"""Reviewed resolution history survives missing raw mismatch exports."""
import pytest

from scripts.async_settle.shadow_report import report
from tests.test_async_settle_shadow_r11_validity import append_sample, signed_observation
from tests.test_async_settle_shadow_r13_rollback import (
    DAY,
    START,
    A,
    B,
    C,
    add_mismatch,
    daily_window,
    rollback,
    seal,
)
from trusted_router.async_settle_shadow_evidence import CONTROL, COUNTER, SAMPLE


def resolution_window(*, missing_a=False, missing_b=True, target=B):
    rows, days, proof = daily_window()
    retained = len(rows)
    add_mismatch(rows, proof)
    if missing_a:
        del rows[retained:]
    at = START-DAY+2_000000
    proof['resolved_mismatches'].append(dict(at_us=at, revision=B, fixed_revision=C,
        serving_since_us=START+2*DAY, artifact_sha256='e'*64))
    if not missing_b:
        _, _, bad = signed_observation(at, phase='settle', charge=999, authorization_id='auth-b')
        bad['deployment']['router_revision'] = B
        append_sample(rows, bad)
    rollback(rows, 2, revision=C)
    # Exact reviewer witness: B -> C -> B, with no historical B row.
    if target is not None:
        for row in rows:
            body = row['body']
            if row['kind'] == SAMPLE and body['observed_at_us'] >= START+4*DAY:
                body['deployment']['router_revision'] = target
            elif row['kind'] == COUNTER and body['started_at_us'] >= START+4*DAY:
                body['router_revision'] = target
            elif row['kind'] == CONTROL and body['admission_disabled_from_us'] >= START+4*DAY:
                body['router_revisions'] = [target]
    seal(rows, proof)
    return rows, days, proof


def test_reviewer_resolution_link_witness():
    rows, days, proof = resolution_window()
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert result['continuous_seconds'] == 0 and result['clean_window_start_us'] is None
    assert dict(at_us=1791590400000000, revision=B, reason='revision_rollback') in result['resets']
    assert result['gaps'] == [f'resolution:{START-DAY+2_000000}:{B}:missing_support']


@pytest.mark.parametrize('missing_a', [False, True])
@pytest.mark.parametrize('missing_b', [False, True])
@pytest.mark.parametrize('target', [None, A, B])
@pytest.mark.parametrize('reverse', [False, True])
def test_chained_resolution_missing_support_matrix(missing_a, missing_b, target, reverse):
    rows, days, proof = resolution_window(missing_a=missing_a, missing_b=missing_b, target=target)
    if reverse:
        rows.reverse()
        proof['resolved_mismatches'].reverse()
        seal(rows, proof)
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert result['continuous_seconds'] == (518399 if target is None and not missing_a and not missing_b else 0)
    assert set(result['gaps']) == {
        f'resolution:{at}:{revision}:missing_support'
        for missing, at, revision in ((missing_a, START-DAY+1000000, A), (missing_b, START-DAY+2000000, B))
        if missing}
    rollbacks = {(reset['at_us'], reset['revision']) for reset in result['resets'] if reset['reason'] == 'revision_rollback'}
    assert rollbacks == ({(START+4*DAY, target)} if target else set())
    histories = {(reset['at_us'], reset['revision']): reset for reset in result['resets']}
    assert histories[START-DAY+2000000, B].get('invalidated_at_us') == (START+4*DAY if target else None)


def test_missing_resolution_support_alone_blocks_clean_eight_days():
    rows, days, proof = daily_window()
    retained = len(rows)
    at, _ = add_mismatch(rows, proof)
    del rows[retained:]
    seal(rows, proof)
    result = report(rows, days, proof)
    assert result['gaps'] == [f'resolution:{at}:{A}:missing_support']
    assert result['status'] == 'BLOCKED' and result['continuous_seconds'] == 0
    assert result['completeness'] == 'unknown'


@pytest.mark.parametrize('reverse', [False, True])
def test_ambiguous_resolution_cannot_skip_another_history(reverse):
    rows, days, proof = daily_window()
    at, _ = add_mismatch(rows, proof)
    proof['resolved_mismatches'].append(dict(at_us=at, revision=A, fixed_revision=C,
        serving_since_us=START+4*DAY, artifact_sha256='e'*64))
    if reverse:
        proof['resolved_mismatches'].reverse()
    seal(rows, proof)
    with pytest.raises(ValueError, match='duplicate reset resolution'):
        report(rows, days, proof)
