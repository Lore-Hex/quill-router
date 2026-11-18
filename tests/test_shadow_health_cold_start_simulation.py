"""The lock-modelling simulator's own model, and real publishers run on it.

tests/shadow_health_cold_start_simulation.py is a helper module: its name has
no test_ prefix, so pytest never collected the tests it used to hold. They
live here now. The first four check the simulator itself (FIFO locks without
reader barging, a shared lock held through its Rollback RPC, an upgrade
deadlock aborting the younger transaction through the real claim wrapper, the
fake SDK's retry backoff), which the claim-contention and health simulations
rely on. The rest run the real publisher and owner on it: a post-ack stall, a
pre-boundary server stamp, an injected observation-ordering fault, and three
real-thread asyncio owners for one wall-clock slot.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted

from tests.shadow_health_cold_start_simulation import (
    START,
    Clock,
    Database,
    Reader,
    modules,
    run_workers,
    simulate_lifecycle,
)
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_SLOT_TRACE,
    validate_publisher_traces,
)
from trusted_router.async_settle_shadow_v2_schema import POLICY


def test_fifo_no_reader_barging_and_locks_last_through_empty_commit():
    storage, _ = modules()
    clock = Clock()
    db = Database(clock, storage)
    events = []
    key = ("key", "one")

    def holder():
        def callback(tx):
            db.lock(key).acquire(tx, "S")
            clock.sleep(.020)
        db.run_in_transaction(callback)
        events.append(("empty_commit_done", clock.now))

    def writer():
        clock.sleep(.005)
        def callback(tx):
            tx.mutations[key] = "written"
        db.run_in_transaction(callback)
        events.append(("writer_done", clock.now))

    def reader():
        clock.sleep(.010)
        def callback(tx):
            db.lock(key).acquire(tx, "S")
            events.append(("reader_acquired", clock.now))
        db.run_in_transaction(callback)

    run_workers(clock, holder, writer, reader)
    assert [name for name, _ in events] == ["empty_commit_done", "writer_done", "reader_acquired"]
    assert [stamp for _, stamp in events] == pytest.approx([.035, .068, .068])
    assert db.transactions[0].final_rpc == "empty_commit"
    assert not db.lock(key).shared and not db.lock(key).queue


def test_rollback_holds_shared_until_its_final_rpc_completes():
    storage, _ = modules()
    clock = Clock()
    db = Database(clock, storage)
    acquired = []
    key = ("key", "one")

    def holder():
        def callback(tx):
            db.lock(key).acquire(tx, "S")
            clock.sleep(.020)
            raise ValueError("lost")
        with pytest.raises(ValueError, match="lost"):
            db.run_in_transaction(callback)

    def writer():
        clock.sleep(.005)
        tx = Reader(db, transaction=True)
        db.lock(key).acquire(tx, "X")
        acquired.append(clock.now)
        db.release(tx)

    run_workers(clock, holder, writer)
    assert acquired == pytest.approx([.030])
    assert db.transactions[0].final_rpc == "rollback"


def test_upgrade_deadlock_aborts_younger_and_real_wrapper_reentry_does_not_read():
    storage, _ = modules()
    clock = Clock()
    # Use valid contemporary schema timestamps for real claim() callbacks.
    clock.now = 896313600 * 2.
    db = Database(clock, storage)
    slot = int(clock.now / 2)
    outcomes = []

    def contender(delay):
        clock.sleep(delay)
        store = storage.ObservationStore(db, clock=lambda: clock.now, wall=lambda: clock.now)
        try:
            outcomes.append(store.claim(f"boot-{round(delay*1000)}", slot, slot*2_000_000,
                                        storage.Budget(clock.now+.5, store.clock)))
        except storage.ContentionLoss:
            outcomes.append("lost")

    run_workers(clock, lambda: contender(0), lambda: contender(.001))
    assert outcomes.count(True) == outcomes.count("lost") == 1
    assert db.aborts == db.reentries == 1
    assert db.backoffs == [.010]
    assert len(db.transactions) == 3
    assert db.transactions[1].aborted  # Younger SHARED holder loses.
    assert [tx.reads for tx in db.transactions] == [1, 1, 0]
    assert db.transactions[2].final_rpc == "rollback"
    assert db.claim_commits == 2  # Both issued Commit; one was ABORTED.
    assert sum(bool(tx.mutations) and not tx.aborted for tx in db.transactions) == 1
    assert not any(lock.shared or lock.exclusive or lock.queue for lock in db.locks.values())


def test_fake_sdk_retries_callable_with_exponential_backoff_until_deadline():
    storage, _ = modules()
    clock = Clock()
    db = Database(clock, storage)
    calls = []

    def worker():
        def callback(tx):
            calls.append(clock.now)
            db.abort(tx)
            raise Aborted("forced SDK abort")
        with pytest.raises(Aborted):
            db.run_in_transaction(callback, timeout_secs=.449)

    run_workers(clock, worker)
    assert db.backoffs == [.010, .020, .040, .080, .100, .100]
    assert calls == pytest.approx([0, .010, .030, .070, .150, .250, .350])


def test_post_ack_stall_crosses_boundary_as_missed_not_released():
    result = simulate_lifecycle(slots=70, warm=True, post_ack_probability=1.)
    assert result["post_ack_stalls"] > 0
    assert result["previous_winner_missed"] >= .5
    assert result["previous_winner_failures"] == result["released"] == 0
    assert result["unknown_ratio"] == 0


def test_pre_boundary_server_claim_rolls_back_before_owner_validation():
    """A -5ms server stamp cannot become a published reply in these sources."""
    storage, publisher = modules()
    clock = Clock()
    clock.now = START*2.-.005
    publisher.sleep = clock.sleep
    db = Database(clock, storage)
    store = storage.ObservationStore(db, clock=lambda: clock.now, wall=lambda: clock.now+.005)
    job = publisher.lifecycle.PublisherJob(START, 1, clock.now, clock.now+.25)
    admission = SimpleNamespace(admission_lock=threading.Lock(), generation=1,
        revocation_epoch=0, admission_job=job, startup_retention=False)
    reply, = run_workers(clock, lambda: publisher.publish_cycle(store, "boot-skew",
        "us-central1", "a"*40, job, START*2_000_000, admission, True, True))
    assert reply.trace["outcome"] == "failed"
    assert reply.trace["rpc_count"] == 2  # Claim read, then Rollback.
    assert not reply.claimed and not reply.trace["unknown_outcome"]
    assert reply.trace["claim_at_ns"] is reply.trace["ack_at_us"] is None
    assert db.claim_commits == 0 and db.rows == {}
    assert db.transactions[0].final_rpc == "rollback"
    assert db.transactions[0].claim_at_ns < START*2_000_000_000
    PUBLISHER_SLOT_TRACE(reply.trace)  # Owner trace validation accepts this failure.

    published = dict(reply.trace, outcome="published", claim_at_ns=START*2_000_000_000,
        observed_at_ns=START*2_000_000_000+50_000_000,
        publisher_heartbeat_ns=START*2_000_000_000+50_000_000,
        ack_at_us=START*2_000_000+150_000, body_sha256="a"*64)
    PUBLISHER_SLOT_TRACE(published)
    published["claim_at_ns"] -= 1_000_000
    with pytest.raises(ValueError, match="a1-server-slot"):
        PUBLISHER_SLOT_TRACE(published)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_injected_observation_ordering_fault(caplog, warm):
    """Injected negative control, NOT a derivation of the production reversal."""
    result = simulate_lifecycle(warm=warm, injected_ordering_fault=True)
    diagnostics = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    rules = {message.split("exception_message=", 1)[1].split(" ", 1)[0]
             for message in diagnostics}
    # Captured: pytest shows it only when an assertion below fails.
    print(json.dumps(dict(scenario="injected_observation_ordering_fault",
        warm=warm, rejection_rules=sorted(rules), **result)))
    assert result["injected_reversals"] > 0
    assert result["injected_gap_max_ms"] < 30
    assert not diagnostics and result["invalid_reply_releases"] == 0
    assert result["rate"] >= .95
    assert result["unknown_ratio"] <= .01
    assert result["max_60_slot_commits"] <= 1.1
    assert result["max_60_slot_commits_including_warmup"] <= 1.1
    assert result["median_incumbency_streak"] >= 50
    assert 0 <= result["max_published_observation_ms"] <= 250
    assert 0 < result["bound_wait_max_ms"] <= 40
    assert result["max_published_cycle_ms"] <= 500


@pytest.mark.asyncio
async def test_real_threads_asyncio_owner_smoke(caplog):
    # One slot suffices: three real workers can claim, publish, lose and deliver
    # to each owner in that slot.
    await real_threads_asyncio_owner(caplog, slots=1)


async def real_threads_asyncio_owner(caplog, *, slots):
    """Real time/delivery, with a serializable fake DB; no Spanner-lock claim.

    Two-second slots are hard-coded in the contracts and cadence, so do not
    shorten them by patching constants. Only storage transport and evidence
    persistence are fake; Service.run/submit/publish/reply/accept are real.
    """
    import asyncio
    import time

    from trusted_router.config import Settings

    storage, publisher = modules()
    deliveries = {}

    class RecordingService(publisher.Service):
        def publisher_reply(self, reply, *, job=None):
            deliveries.setdefault(self.journal.boot, []).append(threading.get_ident())
            return super().publisher_reply(reply, job=job)

    class RealClock:
        local = SimpleNamespace(index=0)
        sleep = staticmethod(time.sleep)

        @property
        def now(self):
            return time.time()

    class SerialLock:
        @staticmethod
        def acquire(tx, mode):
            pass  # The database mutex encloses the whole transaction/snapshot.

        @staticmethod
        def release(tx):
            pass

    class RealDatabase(Database):
        def __init__(self):
            super().__init__(RealClock(), storage)
            self.mutex = threading.RLock()
            self.worker_threads = set()

        def lock(self, key):
            return SerialLock()

        @contextmanager
        def snapshot(self, *, min_read_timestamp=None):
            with self.mutex:
                self.worker_threads.add(threading.get_ident())
                yield Reader(self, min_read_timestamp=min_read_timestamp)

        def run_in_transaction(self, callback, **options):
            with self.mutex:
                self.worker_threads.add(threading.get_ident())
                return super().run_in_transaction(callback, **options)

    db = RealDatabase()
    services, tasks, traces = [], [], []
    def capture(day, trace):
        PUBLISHER_SLOT_TRACE(trace)
        traces.append(dict(trace))

    # Begin at an actual UTC slot boundary, with ordinary wall/monotonic clocks
    # throughout. No patched sleeps or owner-loop facsimile. The services are
    # built before the boundary, so the only work between the boundary and
    # their first ticks is starting three tasks: building them inside the
    # 250 ms admission window could make every candidate miss on a loaded
    # runner.
    try:
        for _ in range(3):
            settings = Settings(environment="test", async_settle_shadow_policy=POLICY,
                async_settle_shadow_workspaces="ws", source_revision="a"*40)
            settings.async_settle_shadow_health_region = settings.effective_serving_region
            svc = RecordingService(settings, storage.ObservationStore(db))
            svc.traces.append = capture
            # This reproduction isolates publishers: no evidence persistence,
            # session-recovery transport or admission observer traffic.
            svc.next_flush = float("inf")
            services.append(svc)
        # The next boundary at least half a second away.
        first_slot = int((time.time()+.5)/2)+1
        for index, svc in enumerate(services):
            svc.journal.boot = f"realtime-{index}"
            if slots == 1:
                # Use the real hash stagger, with a prompt cold candidate and
                # spaced followers. Avoid a wall-clock-dependent boot/slot hash
                # putting every smoke candidate at the observation deadline.
                candidate = 0
                while publisher.follower_stagger(svc.journal.boot, first_slot, False) != index*.09:
                    candidate += 1
                    svc.journal.boot = f"realtime-{index}-{candidate}"
        await asyncio.sleep(first_slot*2-time.time())
        started = time.monotonic()
        for svc in services:
            tasks.append(asyncio.create_task(svc.run()))
        await asyncio.sleep((first_slot+slots-1)*2+.8-time.time())
    finally:
        for svc in services:
            svc.stopped.set()
        await asyncio.gather(*tasks)
        for svc in services:
            if svc.publisher_job is not None:
                await asyncio.wait_for(svc.publisher_job, 3)
            for executor in (svc.publisher_executor, svc.evidence_executor, svc.session_executor):
                executor.shutdown(wait=True, cancel_futures=True)

    releases = [t for t in traces if t.get("release_reason") == "invalid_publisher_reply"]
    diagnostics = [r.getMessage() for r in caplog.records
                   if r.msg.startswith("shadow_v2_publisher_reply_rejected")]
    result = dict(reproduction="real_threads_asyncio", services=len(services), slots=slots,
        elapsed_seconds=round(time.monotonic()-started, 3), traces=len(traces),
        worker_threads=len(db.worker_threads), owner_deliveries={boot: len(ids) for boot, ids in deliveries.items()},
        outcomes={name: sum(t["outcome"] == name for t in traces)
                  for name in ("published", "lost_claim", "failed", "missed")},
        invalid_publisher_reply_releases=len(releases), diagnostics=diagnostics)
    print(json.dumps(result))  # Captured: shown when an assertion below fails.
    assert len(traces) == len({(t["boot"], t["slot"]) for t in traces}) == len(services)*slots
    assert {t["slot"] for t in traces} == set(range(first_slot, first_slot+slots))
    assert len(db.worker_threads) >= 2 and threading.get_ident() not in db.worker_threads
    published = [t for t in traces if t["outcome"] == "published"]
    assert len(published) == len({t["slot"] for t in published}) > 0
    assert db.claim_commits > 0
    assert any(t["outcome"] == "lost_claim" for t in traces)
    assert len(diagnostics) >= len(releases)
    for svc in services:
        assert deliveries.get(svc.journal.boot)
        assert set(deliveries[svc.journal.boot]) == {threading.get_ident()}
        assert svc.state.job is None and not svc.publisher_worker_active.is_set()
        for predicate in (publisher.lifecycle.accounted_once, publisher.lifecycle.conservation,
                          publisher.lifecycle.lifetime_coverage):
            assert predicate(svc.state)
        for day in svc.state.days:
            rows = [t for t in traces if t["boot"] == svc.journal.boot and t["day"] == day.day]
            validate_publisher_traces(dict(svc.counters[day.day]["publisher"]), rows,
                day=day.day, boot=svc.journal.boot, first_slot=rows[0]["slot"], last_slot=rows[-1]["slot"])
