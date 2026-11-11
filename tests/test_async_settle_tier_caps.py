"""Tier 1 admission: one threshold function and the same answer at every site.

Production admission (the cache at authorize and at settle), the denial
reason, and both shadow predictions each take the threshold from
``admission_cap``. Expected values here are written out by hand.
"""
# ruff: noqa: F811 - shared env fixture
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from tests.test_async_settle_handler import NOW, call, env, prepare  # noqa: F401 - fixture
from tests.test_async_settle_shadow_admission import health as v1_health
from tests.test_async_settle_shadow_observation import NOW_US, installed
from tests.test_async_settle_shadow_observation import health as v2_health
from tests.test_async_settle_ticket import authorization, endpoint
from trusted_router import async_settle_timing as timing
from trusted_router.billing_snapshot import Eligibility
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    DrainHealth,
    admission_cap,
    projection,
)
from trusted_router.services.async_settle_handler import admission_reason
from trusted_router.services.async_settle_shadow_admission import Observer

TIER_ONE = 100_000  # $0.10 in microdollars
PRODUCTION_OVERRIDE = 5_000_000  # the deployed pilot override, $5

# (tier, override) -> threshold; None means the tier is not admitted.
THRESHOLDS = {
    (0, 0): None, (0, 50_000): None, (0, PRODUCTION_OVERRIDE): None,
    (1, 0): 100_000, (1, 50_000): 50_000, (1, PRODUCTION_OVERRIDE): 100_000,
    (1, 200_000_000): 100_000,
    (2, 0): 25_000_000, (2, 50_000): 50_000, (2, PRODUCTION_OVERRIDE): 5_000_000,
    (2, 200_000_000): 200_000_000,
    (3, 0): 100_000_000, (3, 50_000): 50_000, (3, PRODUCTION_OVERRIDE): 5_000_000,
    (3, 200_000_000): 200_000_000,
    (4, 0): None, (4, PRODUCTION_OVERRIDE): None,
}


def cache_for(value: Admission) -> AdmissionCache:
    cache = AdmissionCache(lambda _: value, clock=lambda: 10.0)
    cache.health = DrainHealth(10.0, 0)
    return cache


def v1_observer(value: Admission, override: int) -> Observer:
    observer = Observer(frozenset({'ws'}), lambda _: value, lambda: v1_health(), cap=override,
                        clock=lambda: 10., wall=lambda: 10.)
    observer.install_workspace('ws', 10., value)
    observer.install_health(10., v1_health())
    return observer


def v2_observer(value: Admission, override: int) -> Observer:
    observer = Observer(frozenset({'ws'}), lambda _: value, lambda: None, cap=override,
                        policy='shadow-v2', clock=lambda: 10., wall=lambda: NOW_US/1e6)
    observer.install_workspace('ws', 10., value)
    observer.install_health(10., installed(v2_health()))
    return observer


@pytest.mark.parametrize('tier', [True, False, None, '1', 1.0, -1, 0, 4])
@pytest.mark.parametrize('override', [0, 50_000, PRODUCTION_OVERRIDE])
def test_only_integer_tiers_one_to_three_have_a_threshold(tier, override):
    assert admission_cap(tier, override) is None
    assert not cache_for(Admission(0, tier)).eligible('ws', override)


@pytest.mark.parametrize('tier,override', sorted(THRESHOLDS))
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_every_site_applies_the_same_threshold(tier, override, delta):
    threshold = THRESHOLDS[tier, override]
    amount = max(0, (override if threshold is None else threshold) + delta)
    value = Admission(amount, tier)
    admitted = threshold is not None and amount <= threshold

    cache = cache_for(value)
    assert cache.eligible('ws', override) is admitted
    # The settle-time path answers from the same entry without a read.
    assert cache.eligible('ws', override, cached_only=True) is admitted
    reason = admission_reason(SimpleNamespace(admission=cache), 'ws', override)
    expected = ('ineligible_tier' if threshold is None else
                'eligible' if admitted else 'cap_exceeded')
    if not admitted:
        assert reason == {'ineligible_tier': 'not_eligible', 'cap_exceeded': 'cap_exceeded'}[expected]

    for observer in (v1_observer(value, override), v2_observer(value, override)):
        answer = observer.peek('ws')
        assert answer['reason'] == expected, observer.policy
        assert answer['prediction'] == ('yes' if admitted else 'no')
        assert answer['tier'] == tier and answer['pending_micro'] == amount
        # A tier that is not admitted keeps reporting the configured override.
        assert answer['cap_micro'] == (threshold if threshold is not None else override or None)


@pytest.mark.parametrize('pending,queued,admitted', [
    (0, 0, True), (0, TIER_ONE, True), (60_000, 40_000, True),
    (60_000, 40_001, False), (0, TIER_ONE + 1, False), (TIER_ONE, 1, False),
])
def test_tier_one_counts_queued_cost_against_ten_cents(pending, queued, admitted):
    cache = cache_for(Admission(pending, 1))
    assert cache.eligible('ws', PRODUCTION_OVERRIDE)
    assert cache.eligible('ws', PRODUCTION_OVERRIDE, queued_micro=queued, cached_only=True) is admitted
    reason = admission_reason(SimpleNamespace(admission=cache), 'ws', PRODUCTION_OVERRIDE,
                              queued_micro=queued)
    assert admitted or reason == 'cap_exceeded'


@pytest.mark.parametrize('amount', [TIER_ONE, TIER_ONE + 1])
def test_tier_one_settle_through_the_handler_at_the_production_override(env, amount):
    body, _, _ = prepare(env)
    env[3].async_settle_pilot_cap_micro = PRODUCTION_OVERRIDE
    env[2].admission.entries.clear()
    env[2].admission.read = lambda _: Admission(amount, 1)
    response = call(env, body)
    if amount <= TIER_ONE:
        assert response.status_code == 202
        assert json.loads(response.body)['data']['acceptance']['status'] == 'accepted'
        assert env[1].settle_outbox
    else:
        assert response.status_code == 200
        assert json.loads(response.body)['data']['reason'] == 'cap_exceeded'
        assert not env[1].settle_outbox


@pytest.mark.parametrize('tier,amount,offered', [
    (1, 0, True), (1, TIER_ONE, True), (1, TIER_ONE + 1, False), (0, 0, False),
])
def test_authorize_offers_async_to_tier_one_and_says_so_in_the_metric(env, caplog, tier, amount, offered):
    cfg = env[3]
    cfg.async_settle_pilot_cap_micro = PRODUCTION_OVERRIDE
    env[2].admission.entries.clear()
    env[2].admission.read = lambda _: Admission(amount, tier)
    with caplog.at_level(logging.INFO, logger=timing.logger.name):
        result = projection(authorization=authorization(), endpoints=[endpoint()],
                            requested=Eligibility(), runtime=env[2], settings=cfg, now=NOW)
    assert result['async_eligible'] is offered
    rows = [record.getMessage() for record in caplog.records
            if record.name == timing.logger.name
            and record.getMessage().startswith('metric=async_settle_projection_admission ')]
    assert len(rows) == 1
    assert rows[0].split()[1] == ('eligible=yes' if offered else 'eligible=no')
