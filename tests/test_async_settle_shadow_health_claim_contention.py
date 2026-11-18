"""Claim-row contention for a 32-instance fleet on the lock-modelling simulator.

The 2026-10-22/23 canaries put 1,915 s/h of lock wait on the publisher claim
row. Every owner rejected its publisher reply (trace_claim_observed_order), so
the boot that really held the claim was treated as a follower: the claim aged
out every third slot and every contender raced a read-write claim transaction.

These runs execute the real Service owners, publisher workers and storage
adapter against Spanner-like shared/exclusive locks (FIFO, upgrade deadlocks
abort the younger transaction, SDK re-entry). Some scenarios are paired with a
positive control that patches the fix out of the loaded module copies.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from tests.shadow_health_cold_start_simulation import (
    START,
    Clock,
    Database,
    modules,
    run_workers,
    simulate_lifecycle,
)
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from trusted_router.async_settle_shadow_journal import CLAIM_KIND
from trusted_router.services import async_settle_shadow_health as shell

FLEET = 32
SLOTS = 70  # Ten warm-up slots, then a 2-minute measured window (60 slots).
STALLED = 30-10  # Index of the stall slot in the measured window.
TAKEOVER_GAP = shell.TAKEOVER_MIN_AGE - 1  # Slots without a claim before wave 0.


def run(**kwargs):
    return simulate_lifecycle(warm=True, slots=SLOTS, n=FLEET, **kwargs)


def per_minute(result):
    return sum(result["claim_transactions_by_slot"])/(result["measured_slots"]*2/60)


@pytest.fixture(scope="module")
def rejected():
    return run(reject_published=True)


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
def test_holder_loss_costs_one_bounded_takeover(reject):
    result = run(reject_published=reject, stall_from=30)
    takeover = [n for n in result["claim_transactions_by_slot"] if n > 1]
    assert result["stalled_boots"]
    # One exceptional election can involve every survivor. The steady-state
    # claim row is still single-writer; prompt takeover trades this bounded
    # burst for the previous potentially minute-long silence.
    assert len(takeover) == 1
    assert result["max_claim_contenders"] == FLEET-1
    assert result["aborts"] <= FLEET-2
    # Seeded transport: 2.184735 aggregate lock-seconds for 31 survivors,
    # once in this 120s window. Lock waiting (not just abort count) is bounded.
    assert result["claim_lock_wait_seconds"] < 2.25
    print("HANDOFF", reject, result["max_claim_contenders"], result["aborts"],
          result["claim_lock_wait_seconds"])
    gap = next(i for i, n in enumerate(result["claim_transactions_by_slot"][STALLED:]) if n > 0)
    assert gap == TAKEOVER_GAP == 1
    assert set(result["claim_transactions_by_slot"][STALLED+gap+1:]) == {1}
    assert result["durable_publication_rate"] >= .9


def test_takeover_ranks_spread_without_accumulating_missed_turns():
    claim = {"slot": 1000}
    ranks = [shell.takeover_rank(f"boot-{i}", 1000) for i in range(4096)]
    assert set(ranks) == set(range(shell.TAKEOVER_SPREAD))
    assert all(ranks.count(r) >= 32 for r in range(shell.TAKEOVER_SPREAD))
    for boot in ("boot-0", "boot-1", "boot-2"):
        rank = shell.takeover_rank(boot, 1000)
        due = [shell.takeover_due(boot, 1000+age, claim) for age in range(shell.TAKEOVER_MIN_AGE+2*shell.TAKEOVER_SPREAD)]
        # Never inside the protection window; exactly one turn per spread.
        assert due == [age >= shell.TAKEOVER_MIN_AGE and
                       (age-shell.TAKEOVER_MIN_AGE) % shell.TAKEOVER_SPREAD == rank
                       for age in range(len(due))]
        assert any(due[:shell.TAKEOVER_MIN_AGE + shell.TAKEOVER_SPREAD])
        assert sum(due) == 2
    assert shell.takeover_due("boot-0", 1000, None)  # A cold start is never gated.


@pytest.mark.parametrize("n", [1, 2])
def test_small_fleet_takeover_stays_live(n):
    result = simulate_lifecycle(warm=True, slots=SLOTS,
                                n=n+1, stall_from=30)
    gap = next(i for i, c in enumerate(result["claim_transactions_by_slot"][STALLED:]) if c > 0)
    # Worst case: every survivor has the last rank.
    assert gap <= TAKEOVER_GAP + shell.TAKEOVER_SPREAD - 1
    assert result["aborts"] <= 1


def test_cold_fleet_still_elects_promptly():
    result = simulate_lifecycle(warm=False, slots=SLOTS, n=FLEET)
    # No claim row and no fleet health: the original immediate election.
    assert result["rate"] >= .95
    assert set(result["claim_transactions_by_slot"]) == {1}


def without_storage_renewal(storage, publisher):
    """Positive control: old renewal needs a recent visible publication."""
    publisher.HOLDER_FAILURE_SLOTS = 0
    storage.ObservationStore.read_shadow_publication = lambda self, budget: None


@pytest.mark.parametrize("control", [False, True])
def test_degraded_observer_cannot_demote_the_holder(control):
    # Owners reject every reply (as on 2026-10-23), so incumbency rests on the
    # holder's observer cache. Freeze that cache for three boundaries (~6 s).
    result = run(reject_published=True, stale_holder=(30, 3),
                 patch=without_storage_renewal if control else None)
    if control:
        # Demoted: the holder stands down and the fleet loses publications.
        assert result["durable_publication_rate"] < 1
        assert 0 in result["claim_transactions_by_slot"]
    else:
        # Same-owner renewal from storage keeps exactly one writer every slot.
        assert set(result["claim_transactions_by_slot"]) == {1}
        assert result["durable_publication_rate"] == 1
        assert result["aborts"] == result["claim_lock_wait_seconds"] == 0


def test_microsecond_floor_at_waited_bound():
    # Production replica behaviour: a waited-for bound is returned floored to
    # a microsecond. CLAIM_SQL still returns a statement stamp ending in 777ns.
    result = simulate_lifecycle(warm=True, slots=SLOTS, n=8,
                                microsecond_reads=True, bound_wait_probability=1.)
    traces = result["worker_traces"]
    ordering = [t for t in traces if t.get("observation_failure") == "claim_observed_order"]
    assert not ordering
    assert result["durable_publication_rate"] == 1
    assert set(result["claim_transactions_by_slot"]) == {1}
    published = [t for t in traces if t["outcome"] == "published"]
    assert published and all(t["observed_at_ns"] % 1000 == 0 for t in published)
    assert all(t["observed_at_ns"] - t["claim_at_ns"] == 223 for t in published)
    assert result["max_published_observation_ms"] <= 250
    assert result["max_published_cycle_ms"] <= 500


@pytest.mark.parametrize("n", [8, 20])
def test_two_failed_cycles_recover_without_a_takeover(n):
    from tests.shadow_health_cold_start_simulation import START
    result = simulate_lifecycle(warm=True, slots=SLOTS, n=n,
                                fail_observations=(30, 2), microsecond_reads=True)
    claims = result["claim_transactions_by_slot"]
    publications = set(result["durable_publication_slots"])
    assert START+30 not in publications and START+31 not in publications
    assert START+32 in publications
    assert set(claims) == {1}
    assert result["aborts"] == result["claim_lock_wait_seconds"] == 0
    assert result["durable_publication_rate"] == (SLOTS-12)/(SLOTS-10)


@pytest.mark.parametrize("n", [8, 20])
def test_failure_limit_yields_to_fenced_takeover(n):
    from tests.shadow_health_cold_start_simulation import START
    result = simulate_lifecycle(warm=True, slots=100, n=n,
                                fail_observations=(30, shell.HOLDER_FAILURE_SLOTS))
    traces = result["worker_traces"]
    holder_claims = [t["slot"] for t in traces if t["boot"] == "boot-0" and t["claim_at_ns"] is not None]
    assert max(holder_claims) == START+30+shell.HOLDER_FAILURE_SLOTS-1
    takeovers = [t["slot"] for t in traces if t["boot"] != "boot-0" and t["claim_at_ns"] is not None]
    assert takeovers
    assert shell.TAKEOVER_MIN_AGE <= min(takeovers)-max(holder_claims)
    assert min(takeovers)-max(holder_claims) < shell.TAKEOVER_MIN_AGE+shell.TAKEOVER_SPREAD
    assert result["max_claim_contenders"] <= n-1
    # Only the K forced failures are attempts by the old holder; S5 holds.
    identities = [(t["boot"], t["slot"]) for t in traces]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("warm", [False, True])
def test_unknown_claim_commit_is_reconciled_next_slot(warm):
    from tests.shadow_health_cold_start_simulation import START
    result = simulate_lifecycle(warm=warm, slots=SLOTS, n=8, unknown_claim_at=0)
    traces = result["worker_traces"]
    unknown = [t for t in traces if t["slot"] == START and t["unknown_outcome"]]
    assert len(unknown) == 1
    boot = unknown[0]["boot"]
    following = next(t for t in traces if t["boot"] == boot and t["slot"] == START+1)
    assert following["outcome"] == "published"
    assert following["rpc_count"] == 8  # Claim + publication snapshots, then normal six.
    assert result["aborts"] == 0


def test_two_failed_claim_reads_allow_a_fenced_recovery_race():
    from tests.shadow_health_cold_start_simulation import START
    result = simulate_lifecycle(warm=True, slots=SLOTS, n=20, fail_claim_reads=(30, 2))
    # A failed claim cannot extend protection. Followers may race the owner
    # once that claim is age two, but each boot gets only one attempt per slot.
    # At +31 the old claim is age two and ALL claim reads fail, including
    # the owner's: no winner can suppress the other 19 attempts. Expect one
    # exact 20-way failed burst, not the tautological <= fleet-size bound.
    # At +32 the healthy holder stamps before followers and is the sole writer.
    expected = [1]*(SLOTS-10)
    expected[31-10] = 20
    assert result["claim_transactions_by_slot"] == expected
    assert result["max_claim_contenders"] == 20
    assert result["claim_lock_wait_seconds"] == result["aborts"] == 0
    assert len(result["claim_identities"]) == len(set(result["claim_identities"]))
    print("FAILED_READS", result["max_claim_contenders"], result["claim_lock_wait_seconds"])
    assert START+32 in result["durable_publication_slots"]
    identities = [(t["boot"], t["slot"]) for t in result["worker_traces"]]
    assert len(identities) == len(set(identities))


class PublisherControl:
    """Real copied worker/store and simulator transport, with explicit missed slots."""

    def __init__(self):
        self.storage, self.publisher = modules()
        self.clock = Clock()
        self.db = Database(self.clock, self.storage)
        self.store = self.new_store()
        self.waits = []
        def sleep(seconds):
            self.waits.append(seconds)
            self.clock.sleep(seconds)
        self.publisher.sleep = sleep

    def new_store(self):
        return self.storage.ObservationStore(self.db, clock=lambda: self.clock.now,
                                              wall=lambda: self.clock.now)

    def run(self, slot, callback):
        self.clock.now = slot*2.
        self.clock.ready = self.clock.finished = 0
        def worker():
            self.clock.local.slot = slot
            return callback()
        result, = run_workers(self.clock, worker)
        return result

    def cycle(self, slot, *, store=None, boot="holder", incumbent=False):
        job = self.publisher.lifecycle.PublisherJob(slot, 1, slot*2., slot*2.+.25)
        admission = SimpleNamespace(admission_lock=threading.Lock(), generation=1,
            revocation_epoch=0, admission_job=job, startup_retention=False)
        return self.run(slot, lambda: self.publisher.publish_cycle(
            store or self.store, boot, "us-central1", "a"*40, job, slot*2_000_000,
            admission, incumbent, incumbent, True))


@pytest.mark.parametrize("forget_local_publication", [False, True])
def test_missed_slots_consume_holder_allowance(forget_local_publication):
    control = PublisherControl()
    publisher, store = control.publisher, control.store
    assert control.cycle(START, incumbent=True).trace["outcome"] == "published"
    # No dispatch at s+1. At s+2 the recent publication permits the fast path,
    # but the observation fails.
    observe = store.observe
    def fail(*args, **kwargs):
        raise TimeoutError("failed observation after a missed slot")
    store.observe = fail
    failed = control.cycle(START+2, incumbent=True)
    assert failed.claimed and failed.trace["outcome"] == "failed"
    store.observe = observe
    if forget_local_publication:
        # Storage must supply the publication anchor even without local history.
        control.store = control.new_store()
    # No dispatch at s+3 either. K unpublished slots have now elapsed, even
    # though only one of them had a claim. Renewal at s+4 is forbidden.
    expired = control.cycle(START+publisher.HOLDER_FAILURE_SLOTS+1)
    assert not expired.claimed, "holder renewed after K unpublished slots"
    assert expired.trace["outcome"] == "lost_claim"
    claim = json.loads(control.db.rows[CLAIM_KIND, "local/publish"])
    assert claim["slot"] == START+2
    follower = next(f"follower-{i}" for i in range(10000)
                    if publisher.takeover_rank(f"follower-{i}", claim["slot"]) == 0)
    due = claim["slot"]+publisher.TAKEOVER_MIN_AGE
    assert not publisher.takeover_due(follower, due-1, claim)
    assert publisher.takeover_due(follower, due, claim)
    assert not control.cycle(due).claimed
    reply = control.cycle(due, store=control.new_store(), boot=follower)
    assert reply.claimed and reply.trace["outcome"] == "published"
    assert due-START <= publisher.HOLDER_FAILURE_SLOTS+publisher.TAKEOVER_MIN_AGE+publisher.TAKEOVER_SPREAD-1


def test_uninterrupted_missed_slots_leave_takeover_due():
    control = PublisherControl()
    publisher = control.publisher
    assert control.cycle(START, incumbent=True).trace["outcome"] == "published"
    claim = json.loads(control.db.rows[CLAIM_KIND, "local/publish"])
    # Miss K+1 slots without any claim, then resume at the advertised S4 bound.
    assert not control.cycle(START+publisher.HOLDER_FAILURE_SLOTS+2).claimed
    due = START+publisher.HOLDER_FAILURE_SLOTS+publisher.TAKEOVER_MIN_AGE+publisher.TAKEOVER_SPREAD-1
    follower = next(f"follower-{i}" for i in range(10000)
                    if publisher.takeover_due(f"follower-{i}", due, claim))
    assert due > START+publisher.HOLDER_FAILURE_SLOTS+1
    assert not control.cycle(due).claimed
    assert publisher.takeover_due(follower, due, claim)
    reply = control.cycle(due, store=control.new_store(), boot=follower)
    assert reply.claimed and reply.trace["outcome"] == "published"


def test_definite_claim_loser_staggers_next_slot():
    control = PublisherControl()
    publisher = control.publisher
    assert control.cycle(START, incumbent=True).trace["outcome"] == "published"
    loser = control.new_store()
    # A race reaches the transaction after another boot has committed the slot.
    with pytest.raises(control.storage.ContentionLoss):
        control.run(START, lambda: loser.claim("loser", START, START*2_000_000,
            control.storage.Budget(control.clock.now+.5, lambda: control.clock.now)))
    assert loser.won is None and not loser.claim_unknown
    assert loser.claim_attempt == ("loser", START)
    before = control.db.claim_transactions
    control.waits.clear()
    reply = control.cycle(START+1, store=loser, boot="loser")
    assert reply.trace["outcome"] == "lost_claim" and not reply.claimed
    assert reply.trace["rpc_count"] == 1
    assert control.db.claim_transactions == before
    assert control.waits == [publisher.follower_stagger("loser", START+1, True)], "definite loser skipped follower stagger"


def takeover_measurements(result):
    """Actual durable commits, fenced attempts and reader bases over 120 seconds.

    Readers poll every 500ms, take 20ms to return and use the cache 250ms later.
    Server O/C/R come from the fake transport; no local-wall correction is used.
    The window is offsets 10..70, with the fault at offset 30 (40s into it).
    """
    from collections import Counter

    from trusted_router.async_settle_shadow_health_state import health_basis
    from trusted_router.async_settle_shadow_v2_a1_health import validate_publication

    history = result["publication_history"]
    assert max(Counter(body["slot"] for _, body in history).values()) == 1  # S1
    identities = result["claim_identities"]
    assert len(identities) == len(set(identities))  # S5 at the transaction boundary
    traces = {(t["boot"], t["slot"]): t for t in result["worker_traces"]}
    previous = None
    for committed, body in history:
        trace = traces[body["publisher_boot_id"], body["slot"]]
        claim = dict(v=2, policy_version="shadow-v2", contract_revision="a1",
            authority="local", owner_boot_id=trace["boot"], slot=trace["slot"],
            claim_at_ns=trace["claim_at_ns"])
        validate_publication(claim, body, previous)  # S2 and S3 on durable bodies
        assert body["observed_at_ns"] <= round(committed*1e9)
        previous = body
    installed = 0
    unusable = []
    for sample in range(240):
        read = (START+10)*2 + sample*.5
        committed, body = max((entry for entry in history if entry[0] <= read), key=lambda e: e[0])
        observer = SimpleNamespace(stale=set(), health=dict(body=body,
            committed_at_ns=round(committed*1e9), read_at_ns=round(read*1e9),
            read_elapsed_ns=20_000_000, reply_at=read+.02))
        basis = health_basis(observer, "simulated-reader", at=read+.27)
        installed += basis["source_state"] == "installed"
        if basis["source_state"] != "installed":
            unusable.append(sample)
    return dict(installed=installed, samples=240, share=installed/240,
                unusable_seconds=len(unusable)*.5,
                max_contenders=result["max_claim_contenders"], aborts=result["aborts"])


@pytest.mark.parametrize("n", [1, 2, 4, 12, 20])
@pytest.mark.parametrize("kind", ["order", "refused", "post_commit"])
def test_full_rpc_failure_recovery_and_reader_freshness(n, kind):
    result = simulate_lifecycle(warm=True, n=n, slots=70,
                                publication_fault=(30, 2, kind), microsecond_reads=True)
    failed = [t for t in result["worker_traces"] if t["outcome"] == "failed"]
    assert [(t["slot"]-START, t["rpc_count"]) for t in failed] == [(30, 6), (31, 6)]
    assert all(t.get("observation_failure") == (None if kind == "refused" else "claim_observed_order")
               for t in failed)
    recovered = [t for t in result["worker_traces"] if t["slot"] == START+32 and t["outcome"] == "published"]
    assert len(recovered) == 1 and recovered[0]["boot"] == "boot-0"
    assert result["max_claim_contenders"] == 1
    for trace in result["worker_traces"]:
        if START+10 <= trace["slot"] < START+30 or trace["slot"] >= START+35:
            assert trace["rpc_count"] == (6 if trace["boot"] == "boot-0" else 1)
    # Recovery at last acknowledged publication+3.
    metrics = takeover_measurements(result)
    assert metrics["share"] >= .95
    if kind == "post_commit":
        assert metrics["share"] == 1  # Failed worker reports did not erase commits.
    print("TAKEOVER_TWO_FAILURES", n, kind, metrics)


@pytest.mark.parametrize("n", [2, 4, 12, 20])
@pytest.mark.parametrize("exhausted", [False, True])
def test_takeover_reader_bound_with_healthy_survivors(n, exhausted):
    # One total process gone leaves zero survivors, tested separately below.
    kwargs = dict(publication_fault=(30, 3, "refused")) if exhausted else dict(stall_from=30)
    result = simulate_lifecycle(warm=True, n=n, slots=70, **kwargs)
    if not exhausted:
        assert all(t["rpc_count"] == 0 for t in result["worker_traces"]
                   if t["boot"] == "boot-0" and t["slot"] >= START+30)
    resumed = min(s for s in result["durable_publication_slots"] if s >= START+30)
    assert resumed-(START+29) == (5 if exhausted else 2)
    metrics = takeover_measurements(result)
    assert metrics["share"] >= .95
    assert metrics["max_contenders"] <= n-1
    assert metrics["aborts"] <= n-2
    print("TAKEOVER_SURVIVORS", n-1, exhausted, resumed-(START+29), metrics)


@pytest.mark.parametrize("kind", [None, "order", "refused", "post_commit"])
def test_four_contender_long_gap(kind):
    import hashlib

    # Deterministic adverse ranks, not a changed protocol or a random search at
    # runtime: all three survivors' first rank-based turns are at least age 40.
    last_claim = START+(31 if kind else 29)
    boots = ["boot-0"]
    for i in range(1000):
        boot = f"late-survivor-{i}"
        rank = int.from_bytes(hashlib.sha256(f"takeover:{boot}:{last_claim}".encode()).digest()[:8], "big") % 64
        if rank >= 40:
            boots.append(boot)
        if len(boots) == 4:
            break
    assert len(boots) == 4
    kwargs = dict(publication_fault=(30, 2, kind), stall_from=32) if kind else dict(stall_from=30)
    result = simulate_lifecycle(warm=True, n=4, boots=boots, slots=130,
                                microsecond_reads=True, **kwargs)
    anchor = START+(31 if kind == "post_commit" else 29)
    resumed = min(s for s in result["durable_publication_slots"] if s > anchor)
    gap = resumed-anchor
    assert gap <= (4 if kind in ("order", "refused") else 2)
    metrics = takeover_measurements(result)
    assert metrics["share"] >= .95
    print("TAKEOVER_LONG_GAP", kind, gap, metrics)


def test_only_process_gone_cannot_publish():
    result = simulate_lifecycle(warm=True, n=1, slots=70, stall_from=30)
    assert max(result["durable_publication_slots"]) == START+29


def test_takeover_eligibility_is_not_a_successful_rpc_guarantee():
    # Additional fleet size: the first winner can miss S3 under the seeded RPC
    # tail despite having become eligible at +2. Its next-slot renewal recovers.
    result = simulate_lifecycle(warm=True, n=13, slots=70, stall_from=30)
    assert START+31 not in result["durable_publication_slots"]
    assert START+32 in result["durable_publication_slots"]
    assert any(t["claim_at_ns"] is not None
               for t in result["worker_traces"] if t["slot"] == START+31 and t["outcome"] == "failed")
    assert takeover_measurements(result)["share"] >= .95


def test_late_recovery_start_explains_two_read_renewal_refusal():
    control = PublisherControl()
    store = control.store
    assert control.cycle(START, incumbent=True).trace["outcome"] == "published"
    publish = store.publish
    store.publish = lambda value, budget: publish(dict(value, publisher_boot_id="fence-conflict"), budget)
    for offset in (1, 2):
        failed = control.cycle(START+offset, incumbent=True)
        assert failed.trace["outcome"] == "failed" and failed.trace["rpc_count"] == 6
    store.publish = publish
    worker = control.publisher.publish_cycle
    def late_start(*args):
        # An admitted start at B+230ms is valid, but two 20ms point reads
        # finish at B+270ms. Another claim cannot fit inside S3's 250ms window.
        control.clock.sleep(.230)
        return worker(*args)
    control.publisher.publish_cycle = late_start
    refused = control.cycle(START+3)
    assert refused.trace["outcome"] == "lost_claim" and refused.trace["rpc_count"] == 2
    assert refused.trace["cycle_us"] == 40_000 and not refused.trace["late_cycle"]
    control.publisher.publish_cycle = worker
    # The missed recovery slot consumed K. This reproduces the canary's next
    # two 2-RPC refusals followed by 1-RPC refusals without any clock skew.
    for offset, rpcs in ((4, 2), (5, 2), (6, 1)):
        refused = control.cycle(START+offset)
        assert refused.trace["outcome"] == "lost_claim" and refused.trace["rpc_count"] == rpcs


def recovery_preflight_delay(storage, publisher):
    """Admitted late start; a preflight-only scheduling delay spends S3's margin."""
    worker = publisher.publish_cycle
    publication = storage.ObservationStore.read_shadow_publication
    def delayed_worker(*args):
        if args[1] == "boot-0" and args[4].slot == START+32:
            publisher.sleep(.190)
        return worker(*args)
    def delayed_preflight(self, budget):
        if self.won == ("boot-0", START+31):
            publisher.sleep(.040)
        return publication(self, budget)
    publisher.publish_cycle = delayed_worker
    storage.ObservationStore.read_shadow_publication = delayed_preflight


@pytest.mark.parametrize("n", [1, 2, 4, 12, 20])
@pytest.mark.parametrize("kind", ["order", "refused"])
def test_two_full_rpc_failures_with_late_recovery(n, kind):
    import hashlib

    # Every follower gets a late rank; each one still gets a turn. This does
    # not patch Service decisions, claim fences or RPC retries.
    boots = ["boot-0"]
    for i in range(1000):
        boot = f"recovery-survivor-{i}"
        rank = int.from_bytes(hashlib.sha256(f"takeover:{boot}:{START+31}".encode()).digest()[:8], "big") % 64
        if rank >= 40:
            boots.append(boot)
    boots = boots[:n]
    result = simulate_lifecycle(warm=True, n=n, boots=boots, slots=130,
        publication_fault=(30, 2, kind), patch=recovery_preflight_delay, fixed_rpc_costs=True)
    traces = {t["slot"]-START: t for t in result["worker_traces"] if t["boot"] == "boot-0"}
    assert [(traces[s]["outcome"], traces[s]["rpc_count"]) for s in (30, 31)] == [("failed", 6)]*2
    resumed = min((s for s in result["durable_publication_slots"] if s > START+31), default=None)
    assert resumed == START+32
    assert traces[32]["outcome"] == "published" and traces[32]["rpc_count"] == 6
    metrics = takeover_measurements(result)
    assert metrics["share"] >= .95
    print("TAKEOVER_LATE_RECOVERY", n, kind, resumed-(START+29), metrics)


def test_scheduling_overshoot_does_not_ratchet_holder_start():
    result = simulate_lifecycle(n=1, warm=True, slots=1000,
        fixed_rpc_costs=True, tick_offset_seconds=.020, admission_overshoot_seconds=.0007)
    traces = result["worker_traces"]
    starts = [t["claim_start_us"]-t["slot"]*2_000_000 for t in traces
              if t["claim_at_ns"] is not None]
    assert len(starts) == 1000
    assert 20_699 <= min(starts) <= max(starts) <= 20_702
    assert all(t["outcome"] == "published" and t["rpc_count"] == 6 for t in traces)
    assert result["max_published_observation_ms"] < 250
    assert result["max_published_cycle_ms"] < 500
    assert len(result["claim_identities"]) == len(set(result["claim_identities"]))
    print("CADENCE", min(starts), max(starts))


def test_age_two_claim_does_not_race_a_healthy_reclaiming_holder():
    # One omitted holder wake every 100 slots creates an age-two claim on the
    # next turn. The holder remains healthy; no transport fault is injected.
    skipped = tuple(range(50, 1000, 100))
    result = simulate_lifecycle(n=2, warm=True, slots=1000, fixed_rpc_costs=True,
        tick_offset_seconds=.020, admission_overshoot_seconds=.0007, skip_holder_slots=skipped)
    claims = result["claim_identities"]
    traces = {(t["boot"], t["slot"]): t for t in result["worker_traces"]}
    stamps = []
    for offset in skipped:
        slot = START+offset+1
        assert (0, slot-2) in claims and not any(s == slot-1 for _, s in claims)
        assert [(boot, s) for boot, s in claims if s == slot] == [(0, slot)]
        holder = traces["boot-0", slot]
        stamps.append((holder["claim_at_ns"]-slot*2_000_000_000)/1e6)
        assert holder["outcome"] == "published"
        assert traces["boot-1", slot]["outcome"] == "lost_claim"
    assert max(stamps) < 60
    assert all(t["outcome"] == "published" for t in result["worker_traces"] if t["boot"] == "boot-0")
    assert result["claim_lock_wait_seconds"] == result["aborts"] == 0
    assert len(claims) == len(set(claims))
    extra_claims = sum(boot != 0 and slot-START-1 in skipped for boot, slot in claims)
    assert extra_claims == 0
    print("AGE_TWO", len(skipped), "extra_claim_transactions", extra_claims, "max_claim_ms", max(stamps))


@pytest.mark.parametrize("delay", [.12, .2])
@pytest.mark.parametrize("late_sampling", [True, False], ids=["round2-order", "pre-wall-order"])
def test_tick_prelude_pause_costs_only_its_slot(delay, late_sampling):
    pause_slot = START+30
    pauses, admissions = [], {}

    def patch(storage, publisher):
        tick = publisher.Service.tick
        record_breaker = publisher.Service.record_breaker
        worker = publisher.publish_cycle

        def delayed_breaker(self):
            clock = self.store.database.clock
            if not pauses and int(clock.now//2) == pause_slot:
                pauses.append(clock.now)
                # Owner callback, with n=1 and no outstanding worker/event in
                # this interval: advance time inside tick's actual prelude.
                clock.now += delay
            return record_breaker(self)

        def old_order(self, stamp, *, mono=None, publisher=None):
            # CURRENT source, only discard the pre-wall sample. This control
            # survives squash merges and never needs a historical git object.
            return tick(self, stamp, publisher=publisher)

        def measured_worker(*args):
            reply = worker(*args)
            admissions[reply.trace["slot"]] = reply.actual_start_mono
            return reply

        publisher.Service.record_breaker = delayed_breaker
        publisher.publish_cycle = measured_worker
        if late_sampling:
            publisher.Service.tick = old_order

    result = simulate_lifecycle(n=1, warm=True, slots=330, fixed_rpc_costs=True,
        tick_offset_seconds=.020, admission_overshoot_seconds=.0007, patch=patch)
    assert len(pauses) == 1
    traces = {t["slot"]: t for t in result["worker_traces"]}
    later = range(pause_slot+1, START+330)
    starts = [(admissions[s]-s*2)*1e6 for s in later]
    late = [traces[s] for s in later if traces[s]["outcome"] == "failed"]
    if late_sampling:
        assert all(start >= delay*1e6 for start in starts)
        if delay == .2:
            assert late
            assert all(t["observed_at_ns"] > t["slot"]*2_000_000_000+250_000_000
                       and t["rpc_count"] == 6 and not t["late_cycle"]
                       and not t["unknown_outcome"] and "observation_failure" not in t for t in late)
        else:
            assert all(traces[s]["outcome"] == "published" for s in later)
    else:
        assert 20_699 <= min(starts) <= max(starts) <= 20_702
        assert all(traces[s]["outcome"] == "published" for s in later)
        paused = traces[pause_slot]
        # S3 may reject a late admitted observation; that costs only this slot.
        assert paused["outcome"] == ("published" if delay == .12 else "failed")
        assert paused["claim_start_us"]-pause_slot*2_000_000 >= delay*1e6
        if paused["outcome"] == "failed":
            assert paused["observed_at_ns"] > pause_slot*2_000_000_000+250_000_000
        assert result["max_published_observation_ms"] <= 250
        assert result["max_published_cycle_ms"] <= 500
    print("SAMPLING_PAUSE", "round2" if late_sampling else "pre-wall", delay,
          "pause", traces[pause_slot]["outcome"],
          "pause_start_us", (admissions[pause_slot]-pause_slot*2)*1e6,
          "pause_observation_ms", (traces[pause_slot]["observed_at_ns"]-pause_slot*2_000_000_000)/1e6,
          "later_start_us", min(starts), max(starts),
          "later_outcomes", {outcome: sum(traces[s]["outcome"] == outcome for s in later)
                             for outcome in ("published", "failed", "lost_claim", "missed")},
          "late_observation_ms", [(t["observed_at_ns"]-t["slot"]*2_000_000_000)/1e6 for t in late])


@pytest.mark.parametrize("site", ["run", "start", "_flush", "schedule_cycle", "publish_cycle", "observer_bootstrap"])
def test_job_creating_tick_samples_monotonic_before_wall(monkeypatch, site):
    import asyncio

    from tests.test_async_settle_shadow_observation import NOW_US, service

    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    at, reads, ticks = [100.], {}, []

    def clock():
        at[0] += .001
        return at[0]

    def wall():
        at[0] += .001
        value = NOW_US/1e6 + at[0]-100
        reads[int(value*1e6)] = at[0]
        return value

    class ReachedTick(Exception):
        pass

    accept = svc.accept
    def capture(event):
        if isinstance(event, shell.lifecycle.Tick):
            ticks.append(event)
            raise ReachedTick
        return accept(event)

    monkeypatch.setattr(svc.store, "clock", clock)
    monkeypatch.setattr(svc.store, "wall", wall)
    monkeypatch.setattr(svc.observer, "evidence_wall", wall)
    monkeypatch.setattr(svc, "accept", capture)
    monkeypatch.setattr(svc, "trap_sigterm", lambda: None)
    monkeypatch.setattr(svc.runtime_stop, "poll", lambda: True)
    monkeypatch.setattr(svc.runtime_stop, "start", lambda *args: None)
    monkeypatch.setattr(svc.store, "session_recovery_due", lambda: False)

    async def drive():
        with pytest.raises(ReachedTick):
            if site == "run":
                await svc.run()
            elif site == "start":
                # start only installs the retry supervisor now. The first tick
                # lives in run; inspect that same paired-clock boundary.
                monkeypatch.setattr(svc, "supervise", svc.run)
                svc.start()
                svc.runtime_stop.available = True
                await svc.task
            elif site == "_flush":
                next(svc._flush(200.))
            elif site == "observer_bootstrap":
                svc.observer._count("health_reads")
                svc.observer.inflight_ticks[None] = 100.
            else:
                getattr(svc, site)(NOW_US//2_000_000, NOW_US)

    try:
        asyncio.run(drive())
        tick, = ticks
        assert tick.admit
        assert tick.mono <= reads[tick.stamp], (site, tick.mono, reads[tick.stamp])
        # The chosen stamp is the immediately following wall read, not one of
        # enabled()/pin()'s later policy reads inside the real tick prelude.
        assert reads[tick.stamp]-tick.mono == pytest.approx(.001)
    finally:
        for executor in (svc.publisher_executor, svc.evidence_executor, svc.session_executor):
            executor.shutdown(wait=True, cancel_futures=True)
