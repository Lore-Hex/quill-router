from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from tests.test_async_settle_ticket import authorization, endpoint, runtime, settings
from trusted_router.billing_snapshot import Eligibility
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    DrainHealth,
    authorize_additions,
    projection,
)
from trusted_router.storage_gcp_async_admission import admission_statement, read_admission

MATRIX = list(itertools.product([False, True], [1, 2, 3], [False, True], [False, True],
                               ['fresh', 'stale', 'missing', 'slow']))


@pytest.mark.parametrize('enabled,tier,cohort,under,health', MATRIX)
def test_eligibility_matrix(enabled, tier, cohort, under, health):
    calls = []
    rt = runtime()
    cap = {1: 100000, 2: 25000000, 3: 100000000}[tier]
    rt.admission = AdmissionCache(lambda ws: calls.append(ws) or Admission(cap + int(not under), tier),
                                  clock=lambda: 10.0)
    rt.admission.health = {'fresh': DrainHealth(10, 5), 'stale': DrainHealth(5, 0),
                           'missing': None, 'slow': DrainHealth(10, 5.001)}[health]
    config = settings()
    config.async_settle_enabled = enabled
    result = projection(authorization=authorization(), endpoints=[endpoint()],
                        requested=Eligibility(typed=cohort), runtime=rt, settings=config)
    assert result['async_eligible'] is (enabled and cohort and under and health == 'fresh')
    assert len(calls) == int(enabled and cohort)
    if not cohort:
        assert result == {'async_eligible': False}


@pytest.mark.parametrize('tier', [2, 3])
@pytest.mark.parametrize('amount,expected', [(4999999, True), (5000000, True), (5000001, False)])
def test_pilot_cap(tier, amount, expected):
    rt = runtime()
    rt.admission.read = lambda ws: Admission(amount, tier)
    assert rt.admission.eligible('ws', 5000000) is expected


def test_cache_freshness_failed_reads_concurrency_and_slow_reads():
    now = [0.0]
    calls = []
    def read(ws):
        calls.append(ws)
        return Admission(0, 2)
    cache = AdmissionCache(read, clock=lambda: now[0])
    cache.health = DrainHealth(0, 0)
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(lambda _: cache.eligible('ws', 0), range(64)))
    assert calls == ['ws']
    now[0] = 4.999
    assert cache.eligible('ws', 0)
    assert calls == ['ws']
    def failed(ws):
        calls.append(ws)
        raise RuntimeError('read failure')
    cache.read = failed
    now[0] = 5
    cache.health = DrainHealth(5, 0)
    assert not cache.eligible('ws', 0)
    assert not cache.eligible('ws', 0)
    assert calls == ['ws', 'ws']
    now[0] = 10
    def slow(ws):
        now[0] += 5
        cache.health = DrainHealth(now[0], 0)
        return Admission(0, 2)
    cache.read = slow
    assert not cache.eligible('ws', 0)


@pytest.mark.parametrize('value', [None, Admission(-1, 2), Admission(True, 2), Admission(0, True),
                                  Admission(0, 0), Admission(0, 4)])
def test_missing_or_malformed_facts(value):
    rt = runtime()
    rt.admission.read = lambda ws: value
    assert not rt.admission.eligible('ws', 0)


EXCLUSIONS = {
    'typed': False, 'usage_type': 'BYOK', 'authority': 'deferred_home', 'route_type': 'decide',
    'service_tier': 'priority', 'app_markup': 1, 'custom_markup': 1, 'receipt_fee': 1,
    'request_fee': 1, 'custom_model': True, 'user_model': True, 'tool_cost': True,
    'search_cost': True, 'image_cost': True, 'video_cost': True, 'partner': True,
    'liberty': True, 'native_batch': True, 'fusion': True, 'polyphemus': True, 'private_tier_basis': True,
}


@pytest.mark.parametrize('field,value', EXCLUSIONS.items())
def test_each_contract_exclusion(field, value):
    assert projection(authorization=authorization(), endpoints=[endpoint()],
                      requested=Eligibility(**{field: value}), runtime=runtime(),
                      settings=settings()) == {'async_eligible': False}


@pytest.mark.parametrize('change', [dict(usage_type='Payouts'), dict(request_price_microdollars=1),
                                    dict(prompt_price_microdollars_per_million_tokens=-1),
                                    dict(prompt_price_microdollars_per_million_tokens=1 << 63),
                                    dict(provider='not a slug'), dict(provider=''), dict(model_id='not a model')])
def test_candidate_exclusions(change):
    assert projection(authorization=authorization(), endpoints=[endpoint(**change)],
                      requested=Eligibility(), runtime=runtime(), settings=settings()) == {'async_eligible': False}


@pytest.mark.parametrize('change,convention', [
    (dict(provider='google'), 'includes_cache'), (dict(model_id='custom/model'), 'includes_cache'),
    # The two resellers relaying Anthropic-native usage for an anthropic/
    # model are cache-exclusive, as the anthropic slug is; the same resellers
    # serving another publisher's model are not.
    (dict(provider='amazon-bedrock', model_id='anthropic/billing-v1'), 'excludes_cache'),
    (dict(provider='google-vertex', model_id='anthropic/billing-v1'), 'excludes_cache'),
    (dict(provider='amazon-bedrock', model_id='openai/billing-v1'), 'includes_cache'),
    (dict(provider='together', model_id='anthropic/billing-v1'), 'includes_cache'),
    (dict(provider='anthropic', model_id='anthropic/billing-v1'), 'excludes_cache'),
    (dict(provider='anthropic'), 'excludes_cache')])
def test_any_provider_and_any_publisher_is_a_candidate(change, convention):
    """The snapshot takes every provider's Credits endpoint, whatever the
    model's publisher; the prompt convention follows the provider slug and
    the model id (provider_contracts.provider_input_excludes_cache).
    (A custom or user-provided model is excluded by the authorization's own
    facts, never by the candidate's model id: tests/test_async_settle_all_providers.py.)"""
    result = projection(authorization=authorization(), endpoints=[endpoint(**change)],
                        requested=Eligibility(), runtime=runtime(), settings=settings())
    assert result['async_eligible'] is True
    candidate, = result['billing_snapshot']['candidates']
    expected = endpoint(**change)
    assert (candidate['provider'], candidate['model_id']) == (expected.provider, expected.model_id)
    assert candidate['prompt_convention'] == convention


@pytest.mark.parametrize('authority', ['spend_lease', 'regional_lease', 'federated', 'deferred_home'])
def test_each_nonlocal_reservation_excluded(authority):
    auth = authorization()
    auth.settlement = authority
    assert projection(authorization=auth, endpoints=[endpoint()], requested=Eligibility(),
                      runtime=runtime(), settings=settings()) == {'async_eligible': False}


def request(rt, optin=True):
    return Request({'type': 'http', 'method': 'POST', 'path': '/',
                    'headers': [(b'x-tr-settlement-mode', b'async-v1')] if optin else [],
                    'app': SimpleNamespace(state=SimpleNamespace(async_settle=rt))})


def body(**changes):
    return GatewayAuthorizeRequest(api_key_hash='key-v1', model='openai/billing-v1',
                                   route_type='chat.completions', **changes)


@pytest.mark.parametrize('change', ['receipt_zero_fee', 'unknown', 'tools', 'missing_route', 'federated',
                                   'legacy', 'replay', 'lease', 'missing_nonce', 'missing_reservation', 'image'])
def test_route_fact_exclusions(change):
    auth = authorization()
    b = body()
    if change == 'receipt_zero_fee':
        b.inference_receipt = True
    if change in {'unknown', 'lease'}:
        b = body(**{'unknown_feature' if change == 'unknown' else 'spend_lease_id': 'x'})
    if change == 'tools':
        b.requested_parameters = ['tools']
    if change == 'image':
        b.input_modalities = ['text', 'image']
    if change == 'missing_route':
        b.route_type = None
    if change == 'missing_nonce':
        auth.invocation_nonce = None
    if change == 'missing_reservation':
        auth.credit_reservation_id = None
    assert authorize_additions(request=request(runtime()), body=b, authorization=auth,
                              endpoints=[endpoint()], settings=settings(), typed=change != 'legacy',
                              federated=change == 'federated', replay=change == 'replay',
                              effective_at=None) == {'async_eligible': False}


def test_no_optin_no_metadata_or_admission():
    rt = runtime()
    rt.admission.read = lambda ws: pytest.fail('unexpected read')
    assert authorize_additions(request=request(rt, False), body=body(), authorization=authorization(),
                              endpoints=[endpoint()], settings=settings(), typed=True,
                              federated=False, replay=False, effective_at=None) == {}


def test_admission_read_budget_and_bounded_sum():
    calls = []
    def execute(sql, **kwargs):
        calls.append((sql, kwargs))
        return [[2, 500, 2]]
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=execute)))
    assert read_admission(db, 'ws') == Admission(500, 2)
    assert len(calls) == 1
    sql, kwargs = calls[0]
    assert "workspace_id=@ws AND status IN ('pending', 'dead') LIMIT 1001" in sql
    assert 'FORCE_INDEX=tr_settle_outbox_workspace_status' in sql
    assert "leased_until" not in sql
    assert 0 < kwargs['timeout'] <= 0.5 and kwargs['retry'] is None
    assert kwargs['params'] == {'ws': 'ws'}
    assert sql == admission_statement('ws')[0]


@pytest.mark.parametrize('rows', [[], [[1001, 0, 2]], [[1, None, 2]], [[0, None, None]], [[1, -1, 2]]])
def test_admission_read_failure_is_ineligible(rows):
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=lambda *a, **kw: rows)))
    cache = AdmissionCache(lambda ws: read_admission(db, ws), clock=lambda: 0)
    cache.health = DrainHealth(0, 0)
    assert not cache.eligible('ws', 0)


def test_confirmed_empty_workspace_is_zero():
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=lambda *a, **kw: [[0, None, 2]])))
    assert read_admission(db, 'ws') == Admission(0, 2)


def test_cache_capacity_and_lock_wait_fail_closed_without_duplicate_reads():
    calls = []
    cache = AdmissionCache(lambda ws: calls.append(ws) or Admission(0, 2), clock=lambda: 10)
    cache.health = DrainHealth(10, 0)
    cache.entries = {str(i): (10, Admission(0, 2)) for i in range(4096)}
    assert not cache.eligible('new', 0)
    assert cache.eligible('0', 0)
    assert not calls
    cache.lock.acquire()
    try:
        assert not cache.eligible('0', 0)
    finally:
        cache.lock.release()
    assert not calls


@pytest.mark.parametrize('allowlist,expected', [('', True), (' ws-v1,other,ws-v1 ', True),
                                               ('ws-v2', False)])
def test_pilot_workspace_authorize(allowlist, expected):
    config = settings(async_settle_pilot_workspaces=allowlist, async_settle_pilot_cap_micro=5000000)
    rt = runtime()
    result = projection(authorization=authorization(), endpoints=[endpoint()],
                        requested=Eligibility(), runtime=rt, settings=config)
    assert result['async_eligible'] is expected
    if not expected:
        assert not rt.admission.entries


@pytest.mark.parametrize('allowlist', ['ws-v2', 'other,ws-v2'])
def test_a_workspace_outside_the_pilot_set_gets_the_admission_off_answer(allowlist):
    """Admission on and the workspace outside the pilot set: Python's answer
    is its admission-off one byte for byte (shape B: no ticket, no status
    URL), with no admission read, which the edge composes itself from the
    policy's async_settle.admission_workspaces (G15 section 4.1). The
    control: a pilot workspace is still ticketed (shape C)."""
    import json

    def answer(pilots: str, *, admission: bool, rt=None):
        config = settings(async_settle_pilot_workspaces=pilots, async_settle_pilot_cap_micro=5000000)
        config.async_settle_enabled = admission
        return projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                          runtime=rt or runtime(), settings=config, now=1_800_000_000)

    rt = runtime()
    on, off = answer(allowlist, admission=True, rt=rt), answer(allowlist, admission=False)
    assert json.dumps(on) == json.dumps(off)
    assert list(on) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash'] and on['async_eligible'] is False
    assert not rt.admission.entries
    pilot = answer(allowlist + ',ws-v1', admission=True)
    assert list(pilot) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash', 'settlement_ticket',
                           'settlement_status_url']
    assert {k: pilot[k] for k in list(off)[1:]} == {k: off[k] for k in list(off)[1:]}


@pytest.mark.parametrize('allowlist', ['', 'ws-v1', 'other,ws-v1'])
@pytest.mark.parametrize('overlap', ['', 'other', 'ws-v1'])
@pytest.mark.parametrize('fastpath', ['', 'other', 'ws-v1', 'other,ws-v1'])
def test_a_fastpath_workspace_gets_the_admission_off_answer(allowlist, overlap, fastpath):
    """Async settle takes no request the fast path takes (G15 section 4.5):
    a workspace in the fast-path pilot set, not in the overlap list, gets
    Python's admission-off answer byte for byte (shape B: no ticket, no
    status URL, no admission read) with admission on, whether the pilot set
    is empty (every workspace) or names it. Every other combination is
    ticketed (shape C) as before: the control."""
    import json

    def answer(*, admission: bool, rt=None):
        config = settings(async_settle_pilot_workspaces=allowlist, async_settle_pilot_cap_micro=5000000,
                          fastpath_pilot_workspaces=fastpath, async_settle_fastpath_overlap_workspaces=overlap)
        config.async_settle_enabled = admission
        return projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                          runtime=rt or runtime(), settings=config, now=1_800_000_000)

    excluded = 'ws-v1' in fastpath.split(',') and overlap != 'ws-v1'
    rt = runtime()
    on, off = answer(admission=True, rt=rt), answer(admission=False)
    assert list(off) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash']
    if excluded:
        assert json.dumps(on) == json.dumps(off)
        assert on['async_eligible'] is False
        assert not rt.admission.entries
    else:
        assert list(on) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash', 'settlement_ticket',
                            'settlement_status_url']
        assert on['async_eligible'] is True
        assert {k: on[k] for k in list(off)[1:]} == {k: off[k] for k in list(off)[1:]}


EVERYONE = [(leased, overlap, reserved, exclude)
            for leased in (False, True) for overlap in ('', 'other', 'ws-v1')
            for reserved in ('', 'other', 'ws-v1') for exclude in ('', 'ws-v1')
            # The combinations the settings accept: a reserved workspace is
            # on neither the overlap list nor the exclude list.
            if not (reserved and reserved == overlap) and not (reserved == 'ws-v1' and exclude == 'ws-v1')]


@pytest.mark.parametrize('leased,overlap,reserved,exclude', EVERYONE)
def test_every_workspace_is_ticketed_in_all_mode_but_the_reserved_list_and_leased_requests(
        leased, overlap, reserved, exclude):
    """All mode with an empty async pilot set
    (docs/design/fast-admission-widening.md section 9.1): every workspace is
    admitted, so an authorization the pilot did not lease is ticketed (shape
    C) whether the fast path covers its workspace or its exclude list names
    it. A workspace on the reserved list, and an authorization the pilot
    leased (fastpath_envelope set) outside the overlap list, get Python's
    admission-off answer byte for byte (shape B: no ticket, no status URL)
    with no admission read; its settle goes through the lease."""
    import dataclasses
    import json

    auth = dataclasses.replace(authorization(), fastpath_envelope='sealed-v1' if leased else None)

    def answer(*, admission: bool, rt=None):
        config = settings(async_settle_pilot_cap_micro=5000000, fastpath_pilot_all=True,
                          storage_backend='spanner-clickhouse', fastpath_pilot_exclude=exclude,
                          async_settle_fastpath_overlap_workspaces=overlap,
                          async_settle_fastpath_reserved_workspaces=reserved)
        config.async_settle_enabled = admission
        return projection(authorization=auth, endpoints=[endpoint()], requested=Eligibility(),
                          runtime=rt or runtime(), settings=config, now=1_800_000_000)

    ticketed = reserved != 'ws-v1' and not (leased and overlap != 'ws-v1')
    rt = runtime()
    on, off = answer(admission=True, rt=rt), answer(admission=False)
    assert list(off) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash']
    if ticketed:
        assert list(on) == ['async_eligible', 'billing_snapshot', 'billing_snapshot_hash', 'settlement_ticket',
                            'settlement_status_url']
        assert on['async_eligible'] is True
        assert {k: on[k] for k in list(off)[1:]} == {k: off[k] for k in list(off)[1:]}
    else:
        assert json.dumps(on) == json.dumps(off)
        assert on['async_eligible'] is False
        assert not rt.admission.entries


@pytest.mark.parametrize('tier', [1, 2, 3])
@pytest.mark.parametrize('amount', [100000, 100001, 5000000, 5000001])
def test_pilot_authorize_cap_override(tier, amount):
    rt = runtime()
    rt.admission.read = lambda _: Admission(amount, tier)
    result = projection(authorization=authorization(), endpoints=[endpoint()],
                        requested=Eligibility(), runtime=rt,
                        settings=settings(async_settle_pilot_workspaces='ws-v1', async_settle_pilot_cap_micro=5000000))
    # The $5 override replaces the tier 2 and 3 thresholds; tier 1 keeps $0.10.
    assert result['async_eligible'] is (amount <= {1: 100000, 2: 5000000, 3: 5000000}[tier])
