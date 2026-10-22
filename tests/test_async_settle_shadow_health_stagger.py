"""Follower timing and owner acceptance through the production publisher."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_health_contention import native_owner, outcome
from tests.test_async_settle_shadow_health_watchdog import accounting, owner
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    sdk_database,  # noqa: F401
)
from tests.test_async_settle_shadow_observation_r7 import entries
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.async_settle_shadow_journal import CLAIM_KIND, Row
from trusted_router.async_settle_shadow_v2_schema import POLICY
from trusted_router.services import async_settle_shadow_health as shell
from trusted_router.storage_gcp_shadow_observation import Budget


def claim(boot, slot):
    return dict(v=2, policy_version=POLICY, contract_revision="a1", authority="local",
                slot=slot, claim_at_ns=slot*m.SLOT_US*1000, owner_boot_id=boot)


def timed_owner(monkeypatch):
    svc, wall, mono, job = owner()
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        mono[0] += seconds
        wall[0] += round(seconds*1e6)
        svc.store.database.server_ns = wall[0]*1000
    monkeypatch.setattr(shell, "sleep", sleep, raising=False)
    return svc, wall, mono, job, waits


def advance(svc, wall, mono, slot):
    wall[0], mono[0] = slot*m.SLOT_US, 100.+(slot-NOW_US//m.SLOT_US)*2
    svc.store.database.server_ns = wall[0]*1000
    svc.tick(wall[0])
    return svc.state.job


@pytest.mark.parametrize("gap", [1, 2, 3])
def test_incumbent_uses_only_recent_accepted_publication(monkeypatch, gap):
    svc, wall, mono, job, waits = timed_owner(monkeypatch)
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, "published")
    assert len(waits) == 1  # First cycle is a follower.
    next_job = advance(svc, wall, mono, job.slot+gap)
    before = svc.store.database.snapshot_execute_sql_calls
    svc.submit_publisher(next_job.slot, wall[0])
    trace = outcome(svc, next_job, "published")
    assert len(waits) == (1 if gap <= 2 else 2)
    # Incumbents issue only the observation snapshot, with no claim snapshot.
    assert svc.store.database.snapshot_execute_sql_calls-before == (1 if gap <= 2 else 2)
    if gap <= 2:
        assert trace["claim_at_ns"] == next_job.slot*m.SLOT_US*1000


def test_follower_committed_claim_is_one_measured_snapshot(sdk_database, monkeypatch):  # noqa: F811
    svc, job, db, clock, sdk_sleeps = native_owner(sdk_database)
    svc.incumbent_slot = None
    svc.observer.health = {"body": {"slot": job.slot-1}}
    db.rows = [Row.build(CLAIM_KIND, "local/publish", claim("other-boot", job.slot)).body.decode()]
    @contextmanager
    def snapshot():
        yield db.session.snapshot()
    db.snapshot = snapshot
    transactions = []
    original = db.run_in_transaction
    def transaction(*args, **kwargs):
        transactions.append(1)
        return original(*args, **kwargs)
    db.run_in_transaction = transaction
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(shell, "sleep", sleep, raising=False)
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert len(waits) == 1 and .35 <= waits[0] <= .37
    assert trace["rpc_count"] == 1 and trace["rpc_elapsed_us"] == 10_000
    assert trace["claim_at_ns"] is None
    assert [name for name, _ in db.calls] == ["read"]
    assert not sdk_sleeps and not transactions
    counts = svc.counters[trace["day"]]["publisher"]
    assert counts["claims"] == counts["scans"] == counts["failures"] == 0


@pytest.mark.parametrize("existing", ["absent", "previous", "same-owner"])
def test_follower_only_proceeds_without_recent_claim(monkeypatch, existing):
    svc, wall, _, job, waits = timed_owner(monkeypatch)
    if existing != "absent":
        value = claim("other" if existing == "previous" else svc.journal.boot,
                      job.slot-1 if existing == "previous" else job.slot)
        if existing == "same-owner":
            svc.store.claim(value["owner_boot_id"], value["slot"], wall[0], Budget(101., svc.store.clock))
        if existing == "previous":
            # A prior slot protects the incumbent even before it renews.
            svc.store.database.server_ns = (job.slot-1)*m.SLOT_US*1000
            svc.store.claim("other", job.slot-1, wall[0]-m.SLOT_US, Budget(101., svc.store.clock))
            svc.store.database.server_ns = wall[0]*1000
    before = svc.store.database.transaction_execute_sql_calls
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, "published" if existing == "absent" else "lost_claim")
    assert len(waits) == 1
    assert (svc.store.database.transaction_execute_sql_calls > before) == (existing == "absent")
    assert svc.store.database.snapshot_execute_sql_calls == (2 if existing == "absent" else 1)


def test_stagger_is_deterministic_and_bounded():
    stagger = getattr(shell, "follower_stagger", None)
    assert callable(stagger)
    values = {stagger(f"boot-{boot}", slot) for boot in range(12) for slot in range(100)}
    assert values == {.35, .36, .37}
    assert [stagger("boot", s) for s in range(100)] == [stagger("boot", s) for s in range(100)]
    assert stagger("boot", 7) == .37  # SHA-256's first eight bytes, big-endian.


def test_stopped_incumbent_follower_wins_and_then_attempts_immediately(monkeypatch):
    svc, wall, mono, job, waits = timed_owner(monkeypatch)
    svc.store.claim("stopped-incumbent", job.slot, wall[0], Budget(101., svc.store.clock))
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, "lost_claim")
    next_job = advance(svc, wall, mono, job.slot+1)
    svc.submit_publisher(next_job.slot, wall[0])
    outcome(svc, next_job, "lost_claim")
    next_job = advance(svc, wall, mono, job.slot+2)
    svc.submit_publisher(next_job.slot, wall[0])
    trace = outcome(svc, next_job, "published")
    assert trace["observed_at_ns"]-next_job.slot*m.SLOT_US*1000 <= 250_000_000
    assert len(waits) == 3 and svc.incumbent_slot == next_job.slot
    next_job = advance(svc, wall, mono, job.slot+3)
    svc.submit_publisher(next_job.slot, wall[0])
    outcome(svc, next_job, "published")
    assert len(waits) == 3
    accounting(svc)


@pytest.mark.parametrize("change", ["revoke", "generation", "replace", "late"])
def test_job_changed_during_stagger_never_attempts(monkeypatch, change):
    svc, wall, mono, job, waits = timed_owner(monkeypatch)
    def sleep(seconds):
        waits.append(seconds)
        if change == "revoke":
            svc.revocation_epoch = 1
        elif change == "generation":
            svc.accept(m.PolicyChanged(wall[0], job.generation+1))
        elif change == "replace":
            svc.accept(m.PublisherFailed(wall[0], job.slot, job.generation, "publisher_watchdog"))
            advance(svc, wall, mono, job.slot+1)
        else:
            mono[0] += .3
            wall[0] += 300_000
    monkeypatch.setattr(shell, "sleep", sleep, raising=False)
    reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                                svc.settings.source_revision, job, wall[0], svc)
    assert waits and reply.trace["outcome"] == "missed"
    assert reply.trace["rpc_count"] == 0 and not reply.claimed
    assert not svc.store.database.transaction_execute_sql_calls
    assert not svc.store.database.snapshot_execute_sql_calls
    if change == "replace":
        replacement = svc.state.job
        svc.publisher_reply(reply, job=job)
        assert svc.state.job == replacement
    else:
        svc.publisher_reply(reply)
        outcome(svc, job, "missed")
    accounting(svc)


def test_stale_published_reply_cannot_set_incumbent(monkeypatch):
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                                svc.settings.source_revision, job, wall[0], svc)
    assert reply.trace["outcome"] == "published"
    svc.accept(m.PublisherFailed(wall[0], job.slot, job.generation, "publisher_watchdog"))
    advance(svc, wall, mono, job.slot+1)
    svc.publisher_reply(reply, job=job)
    assert hasattr(svc, "incumbent_slot") and svc.incumbent_slot is None
    assert entries(svc)[0]["outcome"] == "failed"
    accounting(svc)


def test_stagger_uses_original_cycle_budget(monkeypatch):
    svc, wall, _, job, waits = timed_owner(monkeypatch)
    remaining = []
    original = svc.store.claim
    def bounded(boot, slot, claim_us, budget):
        remaining.append(budget.remaining(.5))
        return original(boot, slot, claim_us, budget)
    monkeypatch.setattr(svc.store, "claim", bounded)
    svc.submit_publisher(job.slot, wall[0])
    trace = outcome(svc, job, "published")
    assert len(waits) == 1
    assert remaining == pytest.approx([.5-waits[0]])
    assert trace["cycle_us"] == round(waits[0]*1e6)
