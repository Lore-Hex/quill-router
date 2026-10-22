"""Run the actual publisher/storage modules on a deterministic virtual clock.

Eight cooperating threads execute real synchronous callbacks. Only one thread
runs at a time; sleeps and RPCs yield to the event queue, so 130 two-second slots
need no wall-clock sleeps. Shared locks last through Commit/empty Commit/Rollback. Upgrades use a FIFO
queue with no reader barging; competing upgrades abort the younger holder.
Snapshot timestamps are selected at RPC start. The SDK retry loop re-enters
actual application callbacks, including the wrapper's ContentionLoss guard.

All point reads cost 20 ms, mutation Commit 33 ms, empty Commit 15 ms,
Rollback 10 ms and observation 80 ms. The actual publish callable has TWO
reads, so these RPC costs imply 73 ms end to end, not 60 ms. The optional
publish_commit_seconds=0.020 variant gives the requested aggregate 60 ms;
it does not change claim RPCs. Neither variant injects contention or lag.
"""
from __future__ import annotations

import heapq
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import Aborted, GoogleAPICallError
from google.protobuf.timestamp_pb2 import Timestamp

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from trusted_router.async_settle_shadow_journal import CLAIM_KIND, HEALTH_KIND
from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE
from trusted_router.async_settle_shadow_v2_schema import POLICY

ROOT = Path(__file__).resolve().parents[1]
START = 896313600


def modules(ref=None):
    """Import copies, never stash or change the working tree to run a baseline."""
    result = []
    with tempfile.TemporaryDirectory() as directory:
        for name, source in (("observation", "storage_gcp_shadow_observation.py"),
                             ("publisher", "services/async_settle_shadow_health.py")):
            path = "src/trusted_router/" + source
            raw = (subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT)  # noqa: S603, S607 - local git revision, no shell
                   if ref else (ROOT/path).read_bytes())
            dest = Path(directory)/f"{name}.py"
            dest.write_bytes(raw)
            key = "shadow_sim_" + name
            spec = importlib.util.spec_from_file_location(key, dest)
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
            result.append(module)
    storage, publisher = result
    # The copied publisher must catch exceptions from the copied store too.
    for name in ("Budget", "ContentionLoss", "UnknownCommit", "ObservationStore"):
        setattr(publisher, name, getattr(storage, name))
    return storage, publisher


class Clock:
    def __init__(self):
        self.now = 0.
        self.condition = threading.Condition()
        self.events = []
        self.local = threading.local()
        self.active = None
        self.finished = 0
        self.ready = 0

    def sleep(self, seconds):
        ident = threading.get_ident()
        with self.condition:
            heapq.heappush(self.events, (self.now + seconds, self.local.index, ident))
            self.active = None
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.active == ident)

    def park(self):
        ident = threading.get_ident()
        with self.condition:
            self.active = None
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.active == ident)

    def wake(self, tx):
        heapq.heappush(self.events, (self.now, tx.index, tx.ident))

    def worker(self, function, index):
        self.local.index = index
        with self.condition:
            self.ready += 1
        self.sleep(0)
        try:
            return function()
        finally:
            with self.condition:
                self.finished += 1
                self.active = None
                self.condition.notify_all()

    def run(self, count):
        with self.condition:
            self.condition.wait_for(lambda: self.ready == count and len(self.events) == count)
            while self.finished < count:
                self.now, _, self.active = heapq.heappop(self.events)
                self.condition.notify_all()
                self.condition.wait_for(lambda: self.active is None)


class Lock:
    """One FIFO per key. Only the selected clock worker mutates this state."""

    def __init__(self, db):
        self.db = db
        self.shared = set()
        self.exclusive = None
        self.queue = []

    def acquire(self, tx, mode):
        tx.check()
        if mode == "S" and (tx in self.shared or self.exclusive is tx):
            return
        request = (tx, mode)
        self.queue.append(request)
        # All upgraders retain their S lock. Resolve the upgrade cycle before
        # draining; aborting a waiter removes its requests from every key.
        upgraders = [t for t, m in self.queue if m == "X" and t in self.shared]
        while len(upgraders) > 1:
            victim = max(upgraders, key=lambda t: (t.started, t.sequence))
            self.db.abort(victim)
            upgraders.remove(victim)
        self.drain()
        if request in self.queue:
            tx.waiting = True
            started = self.db.clock.now
            self.db.clock.park()
            tx.lock_wait += self.db.clock.now - started
            tx.waiting = False
        tx.check()

    def drain(self):
        while self.queue and self.exclusive is None:
            tx, mode = self.queue[0]
            if mode == "X" and self.shared - {tx}:
                return
            self.queue.pop(0)
            if mode == "S":
                self.shared.add(tx)
            else:
                self.shared.discard(tx)
                self.exclusive = tx
            if tx.waiting:
                self.db.clock.wake(tx)
            if mode == "X":
                return

    def release(self, tx):
        self.shared.discard(tx)
        if self.exclusive is tx:
            self.exclusive = None
        self.queue = [(t, m) for t, m in self.queue if t is not tx]
        self.drain()


class Database:
    def __init__(self, clock, storage, *, publish_commit_seconds=.033):
        self.clock, self.storage = clock, storage
        self.rows = {}
        self.locks = {}
        self.transactions = []
        self.claim_commits = 0
        self.claim_transactions = 0
        self.aborts = 0
        self.inject_abort = False
        self.publish_commit_seconds = publish_commit_seconds
        self.reentries = 0
        self.backoffs = []

    def lock(self, key):
        return self.locks.setdefault(key, Lock(self))

    def release(self, tx):
        for lock in self.locks.values():
            lock.release(tx)

    def abort(self, tx):
        tx.aborted = True
        self.aborts += 1
        self.release(tx)
        if tx.waiting:
            self.clock.wake(tx)

    @contextmanager
    def snapshot(self):
        yield Reader(self)

    def run_in_transaction(self, callback, **options):
        deadline = self.clock.now + options.get("timeout_secs", 1.)
        backoff = .010
        while True:
            tx = Reader(self, transaction=True)
            self.transactions.append(tx)
            try:
                try:
                    result = callback(tx)
                except (Aborted, GoogleAPICallError):
                    raise
                except Exception:
                    tx.final_rpc = "rollback"
                    self.clock.sleep(.010)
                    raise
                tx.final_rpc = "commit" if tx.mutations else "empty_commit"
                # Count Commit RPC requests, including aborted requests. Losers
                # returning False still submit an empty Commit with S held.
                if tx.claim:
                    self.claim_commits += 1
                for key in sorted(tx.mutations):
                    self.lock(key).acquire(tx, "X")
                seconds = .033 if tx.mutations else .015
                if tx.mutations and not tx.claim:
                    seconds = self.publish_commit_seconds
                self.clock.sleep(seconds)
                tx.check()
                if self.inject_abort and tx.claim and tx.mutations:
                    self.inject_abort = False
                    self.abort(tx)
                    tx.check()
                self.rows.update(tx.mutations)
                return result
            except Aborted:
                tx.ended = self.clock.now
                self.release(tx)
                if self.clock.now + backoff >= deadline:
                    raise
                self.backoffs.append(backoff)
                self.clock.sleep(backoff)
                backoff = min(.100, backoff * 2)
                self.reentries += 1
            finally:
                # Locks are released only after final RPC completion, except
                # ABORTED which releases them at the deadlock decision itself.
                self.release(tx)
                if tx.ended is None:
                    tx.ended = self.clock.now


class Reader:
    def __init__(self, database, transaction=False):
        self.db, self.transaction = database, transaction
        self.mutations = {}
        self.claim = False
        self.started = database.clock.now
        self.ended = None
        self.sequence = len(database.transactions)
        self.ident = threading.get_ident()
        self.index = database.clock.local.index
        self.waiting = False
        self.aborted = False
        self.final_rpc = None
        self.lock_wait = 0.
        self.reads = 0

    def check(self):
        if self.aborted:
            raise Aborted("younger shared-lock upgrader")

    def execute_sql(self, sql, *, params, **options):
        db = self.db
        self.reads += 1
        key = (params.get("kind"), params.get("id"))
        if sql == db.storage.CLAIM_SQL:
            self.claim = True
            db.claim_transactions += 1
        if self.transaction:
            db.lock(key).acquire(self, "S")
        ns = round(db.clock.now*1e9)
        stamp = DatetimeWithNanoseconds.from_timestamp_pb(
            Timestamp(seconds=ns//10**9, nanos=ns%10**9))
        body = db.rows.get(key)
        if sql == db.storage.CLAIM_SQL:
            rows = [(body, stamp)]
        elif sql == db.storage.UNRESOLVED_SQL:
            rows = []
        else:
            rows = [] if body is None else [(body,)]
        db.clock.sleep(.080 if sql == db.storage.UNRESOLVED_SQL else .020)
        self.check()
        class Stream(list):
            metadata = SimpleNamespace(transaction=SimpleNamespace(read_timestamp=stamp))
        return Stream(rows)

    def insert_or_update(self, *, table, columns, values):
        self.check()
        for kind, key, body, _ in values:
            self.mutations[kind, key] = body


def simulate(n=8, *, warm=False, slots=130, ref=None, abort=False, kill_at=None,
             publish_commit_seconds=.033):
    storage, publisher = modules(ref)
    clock = Clock()
    publisher.sleep = clock.sleep
    db = Database(clock, storage, publish_commit_seconds=publish_commit_seconds)
    boots = [f"boot-{i}" for i in range(n)]
    incumbent = boots[0] if warm else None
    seen = dict.fromkeys(boots)
    accepted = {boot: START-1 if boot == incumbent else None for boot in boots}
    killed = None
    takeover = None
    traces = []
    if warm:
        db.rows[CLAIM_KIND, "local/publish"] = json.dumps(dict(
            v=2, policy_version=POLICY, contract_revision="a1", authority="local",
            slot=START-1, claim_at_ns=(START-1)*2_000_000_000, owner_boot_id=incumbent))
        db.rows[HEALTH_KIND, "local/fleet"] = json.dumps(storage.summarize([], boot=incumbent,
            region="us-central1", revision="a"*40, slot=START-1,
            observed_ns=(START-1)*2_000_000_000, age_seconds=float((START-1)*2), row_limit=10000))
    commits, transactions, publications, unknowns, attempts, max_observed = [], [], [], 0, 0, 0
    first, first_ack = None, None
    with ThreadPoolExecutor(max_workers=n) as pool:
        for offset in range(slots):
            slot = START+offset
            clock.now = float(slot*2)
            clock.finished = clock.ready = 0
            before = db.claim_commits
            transactions_before = db.claim_transactions
            job = publisher.lifecycle.PublisherJob(slot, 1, clock.now, clock.now+.25)
            futures = []
            if offset == kill_at:
                killed = incumbent
                assert killed is not None
            active = [boot for boot in boots if boot != killed]
            for index, boot in enumerate(active):
                store = object.__new__(storage.ObservationStore)
                store.database, store.clock, store.wall = db, lambda: clock.now, lambda: clock.now
                store.last_claim = None
                store.startup_retention = False
                admission = SimpleNamespace(admission_lock=threading.Lock(), generation=1,
                    revocation_epoch=0, admission_job=job, startup_retention=False)
                health = db.rows.get((HEALTH_KIND, "local/fleet"))
                health_slot = json.loads(health)["slot"] if health else None
                present = (health_slot in (slot-1, slot-2) or seen[boot] == slot-1)
                args = (store, boot, "us-central1", "a"*40, job, slot*2_000_000,
                        admission, accepted[boot] in (slot-1, slot-2), present)
                futures.append(pool.submit(clock.worker, lambda args=args: publisher.publish_cycle(*args), index))
            clock.run(len(active))
            replies = [future.result() for future in futures]
            winners = []
            for boot, reply in zip(active, replies, strict=True):
                trace = reply.trace
                PUBLISHER_SLOT_TRACE(trace)
                traces.append(trace)
                seen[boot] = getattr(reply, "seen_claim_slot", None)
                if trace["outcome"] == "published":
                    assert 0 <= trace["observed_at_ns"] - slot*2_000_000_000 <= 250_000_000
                    winners.append(boot)
                    accepted[boot] = slot
                    if killed is not None and takeover is None:
                        takeover = offset - kill_at + 1
                    max_observed = max(max_observed, (trace["observed_at_ns"]-slot*2_000_000_000)//1000)
                    if first is None:
                        first, first_ack = offset, trace["ack_at_us"]-slot*2_000_000
                elif ref:
                    # origin/main clears incumbent on any non-publication.
                    accepted[boot] = None
                if offset >= 10:
                    unknowns += trace["unknown_outcome"]
                    attempts += trace["outcome"] != "missed"
            assert len(winners) <= 1
            incumbent = winners[0] if winners else None
            commits.append(db.claim_commits-before)
            transactions.append(db.claim_transactions-transactions_before)
            publications.append(len(winners))
            if abort and offset == 20:
                db.inject_abort = True
    measured = slots-10
    claim_txs = [tx for tx in db.transactions if tx.claim and tx.started >= (START+10)*2]
    mean_ms = sum(tx.ended-tx.started for tx in claim_txs)*1000/max(1, len(claim_txs))
    return dict(slots=slots, measured_slots=measured, published=sum(publications[10:]),
        rate=sum(publications[10:])/measured, claim_transactions=db.claim_transactions,
        average_claim_transaction_ms=mean_ms,
        claim_lock_wait_seconds=sum(tx.lock_wait for tx in claim_txs),
        empty_claim_commits=sum(tx.final_rpc == "empty_commit" for tx in claim_txs),
        rollback_claims=sum(tx.final_rpc == "rollback" for tx in claim_txs),
        sdk_reentries=db.reentries,
        takeover_slots=takeover,
        takeover_contenders=max(transactions[kill_at:kill_at+3]) if kill_at is not None else None,
        publications_by_boot={boot: sum(t["boot"] == boot and t["outcome"] == "published" for t in traces) for boot in boots},
        max_claim_transactions_per_slot=max(transactions),
        claim_commits=sum(commits[10:]), commits_per_slot=sum(commits[10:])/measured,
        max_60_slot_commits=max(sum(commits[i:i+60])/60 for i in range(10, slots-59)),
        unknowns=unknowns, attempts=attempts, unknown_ratio=unknowns/max(1, attempts),
        first_published_slot_offset=first,
        first_incumbent_ms=None if first is None else first*2000+first_ack/1000,
        max_published_observation_ms=max_observed/1000, aborts=db.aborts)


def assert_calibrated(results):
    assert all(r["commits_per_slot"] >= 5 and r["rate"] <= .5
               and 150 <= r["average_claim_transaction_ms"] <= 300 for r in results), (
        "Negative control failed: the specified FIFO/retry/RPC model does not "
        "reproduce production on origin/main. Do not treat this as differential proof."
    )


def run_workers(clock, *callbacks):
    with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:
        futures = [pool.submit(clock.worker, callback, index)
                   for index, callback in enumerate(callbacks)]
        clock.run(len(callbacks))
        return [future.result() for future in futures]


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


@pytest.mark.parametrize("ref", ["origin/main", None])
def test_upgrade_deadlock_aborts_younger_and_real_wrapper_reentry_does_not_read(ref):
    storage, _ = modules(ref)
    clock = Clock()
    # Use valid contemporary schema timestamps for real claim() callbacks.
    clock.now = 896313600 * 2.
    db = Database(clock, storage)
    slot = int(clock.now / 2)
    outcomes = []

    def contender(delay):
        clock.sleep(delay)
        store = object.__new__(storage.ObservationStore)
        store.database, store.clock, store.wall = db, lambda: clock.now, lambda: clock.now
        store.startup_retention = False
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


def test_production_calibration(capsys):
    """Must stay red until the baseline reproduces production, never xfail."""
    import json
    import time

    started = time.monotonic()
    baseline = []
    with capsys.disabled():
        for publish_commit_seconds in (.033, .020):
            for ref in ("origin/main", None):
                for warm in (False, True):
                    result = simulate(ref=ref, warm=warm,
                                      publish_commit_seconds=publish_commit_seconds)
                    print(json.dumps(dict(ref=ref or "worktree", warm=warm,
                        publish_end_to_end_ms=round((.040+publish_commit_seconds)*1000),
                        **result)), flush=True)
                    if ref:
                        baseline.append(result)
    elapsed = time.monotonic() - started
    with capsys.disabled():
        print(f"Eight 130-slot scenarios: {elapsed:.3f} seconds", flush=True)
    assert elapsed < 20
    assert_calibrated(baseline)


if __name__ == "__main__":
    baseline = []
    for ref in ("origin/main", None):
        for warm in (False, True):
            result = simulate(ref=ref, warm=warm)
            print(json.dumps(dict(ref=ref or "worktree", warm=warm, **result)), flush=True)
            if ref:
                baseline.append(result)
    assert_calibrated(baseline)
