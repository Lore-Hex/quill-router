"""Claim-row contention for a 32-instance fleet on the lock-modelling simulator.

The 2026-10-22/23 canaries put 1,915 s/h of lock wait on the publisher claim
row. Every owner rejected its publisher reply (trace_claim_observed_order), so
the boot that really held the claim was treated as a follower: the claim aged
out every third slot and every contender raced a read-write claim transaction.

These runs execute the real Service owners, publisher workers and storage
adapter against Spanner-like shared/exclusive locks (FIFO, upgrade deadlocks
abort the younger transaction, SDK re-entry). Each scenario is paired with a
positive control: the same harness with the fix removed must show the storm.
"""
from __future__ import annotations

import pytest

from tests.shadow_health_cold_start_simulation import simulate_lifecycle
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from trusted_router.services import async_settle_shadow_health as shell

FLEET = 32
SLOTS = 70  # Ten warm-up slots, then a 2-minute measured window (60 slots).
STALLED = 30-10  # Index of the stall slot in the measured window.
TAKEOVER_GAP = shell.TAKEOVER_MIN_AGE - 1  # Slots without a claim before wave 0.


def without_fix(storage, publisher):
    """Positive control: the owner-only incumbency and immediate takeover of origin/main."""
    def is_incumbent(self, slot):
        self.assert_owner()
        return self.incumbent_slot in (slot-1, slot-2)
    publisher.Service.is_incumbent = is_incumbent
    publisher.takeover_due = lambda boot, slot, claim: True


def run(**kwargs):
    return simulate_lifecycle(warm=True, slots=SLOTS, n=FLEET, **kwargs)


def per_minute(result):
    return sum(result["claim_transactions_by_slot"])/(result["measured_slots"]*2/60)


@pytest.fixture(scope="module")
def rejected():
    return run(reject_published=True)


@pytest.fixture(scope="module")
def rejected_control():
    return run(reject_published=True, patch=without_fix)


def test_positive_control_rejected_replies_reproduce_the_claim_storm(rejected_control):
    result = rejected_control
    # Every third slot the whole fleet races read-write claims on one row.
    assert result["max_claim_contenders"] >= FLEET//2
    assert sum(n > 1 for n in result["claim_transactions_by_slot"]) >= 15
    assert result["aborts"] >= 30
    assert result["claim_lock_wait_seconds"] >= 2
    assert result["durable_publication_rate"] <= .4
    assert per_minute(result) >= 100


def test_rejected_replies_keep_a_single_claim_writer(rejected):
    result = rejected
    assert result["invalid_reply_releases"] > 0  # The owners did reject every reply.
    assert result["rate"] == 0
    # Storage still has exactly one writer: one claim transaction per slot,
    # from the holder, and no lock wait, abort or rollback on the claim row.
    assert set(result["claim_transactions_by_slot"]) == {1}
    assert result["max_claim_contenders"] == 1
    assert result["aborts"] == result["rollback_claims"] == result["empty_claim_commits"] == 0
    assert result["claim_lock_wait_seconds"] == 0
    assert result["durable_publication_rate"] == 1
    assert per_minute(result) == 30  # The holder's per-slot renewal, nothing else.


@pytest.mark.parametrize("reject", [False, True])
def test_holder_loss_costs_one_small_takeover(reject):
    result = run(reject_published=reject, stall_from=30)
    control = run(reject_published=reject, stall_from=30, patch=without_fix)
    takeover = [n for n in result["claim_transactions_by_slot"] if n > 1]
    assert result["stalled_boots"] and control["stalled_boots"]
    # The control races most of the fleet; the fix lets one rank-0 wave try.
    assert control["max_claim_contenders"] >= FLEET//2
    assert len(takeover) <= 1
    assert result["max_claim_contenders"] <= FLEET//4
    assert result["aborts"] <= 3 < control["aborts"]
    assert result["claim_lock_wait_seconds"] < .1 < control["claim_lock_wait_seconds"]
    # Liveness (S4): a new holder publishes within the spread window, then the
    # row is back to one writer per slot.
    # The last publication is slot 29; the first takeover wave is due at age 3.
    gap = next(i for i, n in enumerate(result["claim_transactions_by_slot"][STALLED:]) if n > 0)
    assert gap <= TAKEOVER_GAP + 1
    assert set(result["claim_transactions_by_slot"][STALLED+gap+1:]) == {1}
    assert result["durable_publication_rate"] >= .9


def test_takeover_ranks_spread_and_widen():
    claim = {"slot": 1000}
    ranks = [shell.takeover_rank(f"boot-{i}", 1000) for i in range(256)]
    assert set(ranks) == set(range(shell.TAKEOVER_SPREAD))
    assert all(ranks.count(r) >= 32 for r in range(shell.TAKEOVER_SPREAD))
    for boot in ("boot-0", "boot-1", "boot-2"):
        rank = shell.takeover_rank(boot, 1000)
        due = [shell.takeover_due(boot, 1000+age, claim) for age in range(8)]
        # Never inside the two-slot protection window; always by the last wave.
        assert due == [age >= shell.TAKEOVER_MIN_AGE + rank for age in range(8)]
        assert due[shell.TAKEOVER_MIN_AGE + shell.TAKEOVER_SPREAD - 1]
    assert shell.takeover_due("boot-0", 1000, None)  # A cold start is never gated.


@pytest.mark.parametrize("n", [1, 2])
def test_small_fleet_takeover_stays_live(n):
    result = simulate_lifecycle(warm=True, slots=SLOTS, n=n+1, stall_from=30)
    gap = next(i for i, c in enumerate(result["claim_transactions_by_slot"][STALLED:]) if c > 0)
    # Worst case: every survivor has the last rank.
    assert gap <= TAKEOVER_GAP + shell.TAKEOVER_SPREAD - 1
    assert result["aborts"] <= 1


def test_cold_fleet_still_elects_promptly():
    result = simulate_lifecycle(warm=False, slots=SLOTS, n=FLEET)
    # No claim row and no fleet health: the original immediate election.
    assert result["rate"] >= .95
    assert set(result["claim_transactions_by_slot"]) == {1}
