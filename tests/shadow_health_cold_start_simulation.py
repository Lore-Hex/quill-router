"""Run the actual publisher/storage modules on a deterministic virtual clock.

Eight cooperating threads execute real synchronous callbacks. Only one thread
runs at a time; sleeps and RPCs yield to the event queue, so 130 two-second slots
need no wall-clock sleeps. Shared locks last through Commit/empty Commit/Rollback. Upgrades use a FIFO
queue with no reader barging; competing upgrades abort the younger holder.
Snapshot timestamps are selected at RPC start. The SDK retry loop re-enters
actual application callbacks, including the wrapper's ContentionLoss guard.

The small lock witnesses use fixed costs. Production calibration uses seeded
service distributions and the real Service owner, including dispatch, delivery,
cadence, watchdog and released replies. Post-ack stalls are an explicitly
hypothetical sensitivity input, not a measured explanation for released jobs.
"""
from __future__ import annotations

import heapq
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import Aborted, GoogleAPICallError
from google.protobuf.timestamp_pb2 import Timestamp

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from trusted_router.async_settle_shadow_journal import CLAIM_KIND, HEALTH_KIND
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_SLOT_TRACE,
    validate_publisher_traces,
)
from trusted_router.async_settle_shadow_v2_schema import POLICY
from trusted_router.storage_gcp_shadow_io import _RECORDER

ROOT = Path(__file__).resolve().parents[1]
START = 896313600
MODULE_SOURCES = (("observation", "storage_gcp_shadow_observation.py"),
                  ("publisher", "services/async_settle_shadow_health.py"))


def baseline_ref():
    """Use local baseline objects only; source archives need no Git metadata."""
    candidates = ["origin/main"]
    base = os.environ.get("GITHUB_BASE_REF")
    if base:
        candidates.extend((base, f"origin/{base}"))
    for ref in dict.fromkeys(candidates):
        try:
            for _, source in MODULE_SOURCES:
                subprocess.run(["git", "cat-file", "-e", f"{ref}:src/trusted_router/{source}"],  # noqa: S603, S607 - local git objects, no shell
                    cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            continue
        return ref
    pytest.skip("baseline unavailable: neither origin/main nor GITHUB_BASE_REF has local source objects "
                "(Git metadata/refs may be absent); worktree cases run independently")


def modules(ref=None):
    """Import copies, never stash or change the working tree to run a baseline."""
    if ref == "baseline":
        ref = baseline_ref()
    result = []
    with tempfile.TemporaryDirectory() as directory:
        for name, source in MODULE_SOURCES:
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


@pytest.mark.parametrize("available,expected", [
    ({"origin/main", "release", "origin/release"}, "origin/main"),
    ({"release", "origin/release"}, "release"),
    ({"origin/release"}, "origin/release"),
    (set(), None),
])
def test_baseline_resolution_order_and_missing_metadata(monkeypatch, available, expected):
    monkeypatch.setenv("GITHUB_BASE_REF", "release")
    checked = []

    def check(command, **kwargs):
        ref, path = command[-1].split(":", 1)
        checked.append(ref)
        assert path in {"src/trusted_router/" + source for _, source in MODULE_SOURCES}
        if ref not in available:
            raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(subprocess, "run", check)
    if expected is None:
        with pytest.raises(pytest.skip.Exception, match="baseline unavailable.*worktree cases run independently"):
            baseline_ref()
        # A missing baseline must never prevent loading the worktree copies.
        storage, publisher = modules()
        assert publisher.ObservationStore is storage.ObservationStore
    else:
        assert baseline_ref() == expected
    order = ["origin/main", "release", "origin/release"]
    assert list(dict.fromkeys(checked)) == (order if expected is None else order[:order.index(expected)+1])


def test_baseline_requires_both_source_blobs(monkeypatch):
    monkeypatch.setenv("GITHUB_BASE_REF", "release")

    def check(command, **kwargs):
        if command[-1] == "origin/main:src/trusted_router/services/async_settle_shadow_health.py":
            raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(subprocess, "run", check)
    assert baseline_ref() == "release"


class Clock:
    def __init__(self):
        self.now = 0.
        self.condition = threading.Condition()
        self.events = []
        self.local = threading.local()
        self.active = None
        self.finished = 0
        self.ready = 0
        self.callbacks = 0

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

    def schedule(self, stamp, callback):
        with self.condition:
            self.callbacks += 1
            heapq.heappush(self.events, (stamp, -self.callbacks, callback))

    def run_queue(self):
        """Owner callbacks run on the caller, workers on cooperative threads."""
        with self.condition:
            while self.events:
                self.now, _, event = heapq.heappop(self.events)
                if callable(event):
                    event()
                else:
                    self.active = event
                    self.condition.notify_all()
                    self.condition.wait_for(lambda: self.active is None)
            assert self.finished == self.ready


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
    def __init__(self, clock, storage, *, publish_commit_seconds=.033, distributed=False):
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
        self.distributed = distributed
        self.random = random.Random(20261022)  # noqa: S311 - reproducible simulation, not secrets

    def duration(self, kind):
        median, tail = {"read": (.020, .060), "snapshot": (.020, .030),
                        "commit": (.033, .100), "empty_commit": (.015, .0225),
                        "rollback": (.010, .015), "observe": (.050, .075)}[kind]
        if not self.distributed:
            return .080 if kind == "observe" else median
        # Two uniform halves give the specified median and bounded upper tail.
        # Lower endpoint = median/2. Tail shapes are assumptions: traces only
        # supplied medians and approximate tails, not per-RPC empirical CDFs.
        u = self.random.random()
        return median * (.5 + u) if u <= .5 else median + (2*u-1)*(tail-median)

    @contextmanager
    def rpc(self):
        started = self.clock.now
        try:
            yield
        finally:
            record = _RECORDER.get()
            if record is not None:
                record(max(0, round((self.clock.now-started)*1e6)))

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
                    with self.rpc():
                        self.clock.sleep(self.duration("rollback"))
                    raise
                tx.final_rpc = "commit" if tx.mutations else "empty_commit"
                # Count Commit RPC requests, including aborted requests. Losers
                # returning False still submit an empty Commit with S held.
                if tx.claim:
                    self.claim_commits += 1
                with self.rpc():
                    for key in sorted(tx.mutations):
                        self.lock(key).acquire(tx, "X")
                    seconds = self.duration("commit" if tx.mutations else "empty_commit")
                    if tx.mutations and not tx.claim and not self.distributed:
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
        self.slot = getattr(database.clock.local, "slot", None)
        self.claim_at_ns = None
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
        with self.db.rpc():
            return self._execute_sql(sql, params=params, **options)

    def _execute_sql(self, sql, *, params, **options):
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
            self.claim_at_ns = ns
            rows = [(body, stamp)]
        elif sql == db.storage.UNRESOLVED_SQL:
            rows = []
        else:
            rows = [] if body is None else [(body,)]
        db.clock.sleep(db.duration("observe" if sql == db.storage.UNRESOLVED_SQL
                                   else "read" if self.transaction else "snapshot"))
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


def simulate_lifecycle(*, ref=None, warm=False, slots=130, post_ack_probability=0.,
                       post_ack_seconds=2., seed=73, clock_offsets_ms=None, n=8,
                       reject_published=False, stall_from=None, patch=None):
    """Execute actual owner/worker admission, not a synthesized busy outcome.

    Clock offsets are server minus each boot's local wall in milliseconds;
    the boundary timers use local wall, while RPC stamps use the server clock.
    No artificial transit, post-ack hold or owner rejection is added for skew.

    The 40% sensitivity holds publish_cycle *after* ack but before it returns.
    A 2s hold crosses exactly the next boundary; its duration is hypothetical,
    selected only to test the operator's allowed post-ack-busy hypothesis.
    Nothing forces a release: tick/submit/reply/watchdog decide all outcomes.
    No bookkeeping delay is injected into claims or their row locks.

    ``n`` sizes the contender fleet. ``reject_published`` reproduces the
    2026-10-23 canaries: every owner rejected its publisher reply
    (trace_claim_observed_order) although the claim and publication committed.
    ``stall_from`` is a slot offset after which the current publisher can no
    longer reach Spanner (its session is unavailable), forcing a takeover.
    ``patch(storage, publisher)`` edits the loaded module copies (controls).
    """
    from trusted_router.config import Settings

    storage, publisher = modules(ref)
    if patch is not None:
        patch(storage, publisher)
    clock = Clock()
    # Offset means server minus local wall time. A negative boot's local
    # boundary happens before the server boundary. The DB uses one common
    # server clock; local walls and boundary timers move together per boot.
    offsets = tuple(value/1000 for value in (clock_offsets_ms or (0,)*n))
    assert len(offsets) == n
    clock.now = START*2. + min(offsets)
    publisher.sleep = clock.sleep
    db = Database(clock, storage, distributed=True)
    boots = [f"boot-{i}" for i in range(n)]
    traces, services, futures, replies, releases = [], [], [], [], []
    stalls = random.Random(seed)  # noqa: S311 - reproducible simulation, not secrets
    held = []
    stalled = set()
    original = publisher.publish_cycle
    def cycle(*args):
        clock.local.slot = args[4].slot
        if stall_from is not None and not stalled and args[4].slot >= START+stall_from:
            health = db.rows.get((HEALTH_KIND, "local/fleet"))
            if health:
                stalled.add(json.loads(health)["publisher_boot_id"])
        if args[1] in stalled:
            args[0].session_ready = lambda: False
        result = original(*args)
        if reject_published and result.trace["outcome"] == "published":
            # The owner's contract check fails exactly as in production.
            result.trace["claim_at_ns"] = result.trace["observed_at_ns"]+1000
        replies.append(result)
        if result.trace["outcome"] == "published" and stalls.random() < post_ack_probability:
            held.append((args[1], args[4].slot))
            clock.sleep(post_ack_seconds)
        return result
    publisher.publish_cycle = cycle

    class Loop:
        @staticmethod
        def create_future():
            return Future()

        @staticmethod
        def call_soon_threadsafe(callback, *args):
            clock.schedule(clock.now, lambda: callback(*args))

    class Executor:
        def __init__(self, pool, index):
            self.pool, self.index = pool, index

        def submit(self, callback):
            with clock.condition:
                expected = clock.ready+1
                future = self.pool.submit(clock.worker, callback, self.index)
                futures.append(future)
                # Ensure the initial worker wake is queued before advancing time.
                clock.condition.wait_for(lambda: clock.ready == expected
                    and any(index == self.index for _, index, _ in clock.events))
                return future

    def capture(day, trace):
        PUBLISHER_SLOT_TRACE(trace)
        traces.append(dict(trace))

    with ThreadPoolExecutor(max_workers=n) as pool:
        for index, boot in enumerate(boots):
            clock.now = START*2. + offsets[index]
            store = storage.ObservationStore(db, clock=lambda: clock.now,
                wall=lambda offset=offsets[index]: clock.now-offset)
            settings = Settings(environment="test", async_settle_shadow_policy=POLICY,
                async_settle_shadow_workspaces="ws", source_revision="a"*40)
            settings.async_settle_shadow_health_region = settings.effective_serving_region
            svc = publisher.Service(settings, store)
            svc.journal.boot = boot
            accept = svc.accept
            def capture_release(event, *, accept=accept, boot=boot, **kwargs):
                # Observe real failure events, including on origin/main where
                # release_reason was not yet retained in the emitted trace.
                if isinstance(event, publisher.lifecycle.PublisherFailed):
                    releases.append((boot, event.slot, event.reason))
                return accept(event, **kwargs)
            svc.accept = capture_release
            # Evidence sink only: retain/validate every trace without simulating
            # GCS. Admission, counters and lifecycle still execute unchanged.
            svc.traces.append = capture
            svc.publisher_executor.shutdown(wait=True)
            svc.publisher_executor = Executor(pool, index)
            svc.loop = Loop()
            services.append(svc)
        clock.now = START*2. + min(offsets)
        if warm:
            services[0].incumbent_slot = START-1
            db.rows[CLAIM_KIND, "local/publish"] = json.dumps(dict(v=2, policy_version=POLICY,
                contract_revision="a1", authority="local", slot=START-1,
                claim_at_ns=(START-1)*2_000_000_000, owner_boot_id=boots[0]))
            db.rows[HEALTH_KIND, "local/fleet"] = json.dumps(storage.summarize([], boot=boots[0],
                region="us-central1", revision="a"*40, slot=START-1,
                observed_ns=(START-1)*2_000_000_000, age_seconds=(START-1)*2., row_limit=10000))

        no_incumbent = set()
        def tick(slot, boundary, indexes):
            if boundary:
                health = db.rows.get((HEALTH_KIND, "local/fleet"))
                body = json.loads(health) if health else None
                if body is None or body["slot"] != slot-1:
                    no_incumbent.add(slot)
                for svc in services:
                    # Ideal observer installation, as in round 2. Reader cache
                    # transport/session contention is not modeled here.
                    svc.observer.health = {"body": body} if body else None
            for index in indexes:
                svc = services[index]
                stamp = round(svc.store.wall()*1e6)
                for effect in svc.tick(stamp):
                    if isinstance(effect, publisher.lifecycle.StartPublisherCycle):
                        svc.submit_publisher(effect.slot, stamp)
        for offset in range(slots):
            slot = START+offset
            for clock_offset in sorted(set(offsets)):
                indexes = tuple(i for i, value in enumerate(offsets) if value == clock_offset)
                clock.schedule(slot*2.+clock_offset,
                    lambda slot=slot, indexes=indexes: tick(slot, True, indexes))
                # Each owner's watchdog also uses that boot's local wall.
                clock.schedule(slot*2.+clock_offset+.750001,
                    lambda slot=slot, indexes=indexes: tick(slot, False, indexes))
        clock.run_queue()
        for future in futures:
            future.result()
        for svc in services:
            assert svc.state.job is None
            for predicate in (publisher.lifecycle.accounted_once, publisher.lifecycle.conservation,
                              publisher.lifecycle.lifetime_coverage):
                assert predicate(svc.state)
            svc.evidence_executor.shutdown(wait=True)
            svc.session_executor.shutdown(wait=True)

    rows = {(t["boot"], t["slot"]): t for t in traces}
    assert len(rows) == len(traces) == slots*n
    measured = [t for t in traces if t["slot"] >= START+10]
    winners = {t["slot"]: t["boot"] for t in traces if t["outcome"] == "published"}
    assert len(winners) == sum(t["outcome"] == "published" for t in traces)
    following = [rows[boot, slot+1] for slot, boot in winners.items()
                 if START+9 <= slot < START+slots-1]
    claim_txs = [tx for tx in db.transactions if tx.claim and tx.slot >= START+10]
    commits = {slot: sum(tx.final_rpc in ("commit", "empty_commit")
        and tx.slot == slot for tx in claim_txs) for slot in range(START+10, START+slots)}
    absent = no_incumbent & commits.keys()
    durable = {tx.slot for tx in db.transactions if tx.final_rpc == "commit" and not tx.aborted
               and any(kind == HEALTH_KIND for kind, _ in tx.mutations)}
    rejected = {slot for _, slot, reason in releases if reason == "invalid_publisher_reply"}
    after_rejected = {slot+1 for slot in rejected} & commits.keys()
    streaks, last, length = [], None, 0
    for slot in range(START+10, START+slots):
        boot = winners.get(slot)
        if boot != last or boot is None:
            if length:
                streaks.append(length)
            length = 0
        if boot is not None:
            length += 1
        last = boot
    if length:
        streaks.append(length)
    attempts = sum(t["outcome"] != "missed" for t in measured)
    published = [t for t in measured if t["outcome"] == "published"]
    def fraction(outcome):
        return sum(t["outcome"] == outcome for t in following)/max(1, len(following))
    return dict(slots=slots, measured_slots=slots-10,
        rate=sum(t["outcome"] == "published" for t in measured)/(slots-10),
        commits_per_slot=sum(commits.values())/(slots-10),
        clock_offsets_ms=[round(offset*1000) for offset in offsets],
        invalid_reply_releases=sum(reason == "invalid_publisher_reply" for _, _, reason in releases),
        invalid_reply_boots=sorted({boot for boot, _, reason in releases if reason == "invalid_publisher_reply"}),
        slots_after_rejection=len(after_rejected),
        commits_after_rejection=(sum(commits[slot] for slot in after_rejected)/len(after_rejected)
            if after_rejected else None),
        empty_claim_commits=sum(tx.final_rpc == "empty_commit" for tx in claim_txs),
        rollback_claims=sum(tx.final_rpc == "rollback" for tx in claim_txs),
        early_claim_reads=sum(tx.claim_at_ns is not None and tx.claim_at_ns < tx.slot*2_000_000_000
            for tx in db.transactions if tx.claim),
        out_of_window_worker_claims=sum(r.trace["claim_at_ns"] is not None
            and not r.trace["slot"]*2_000_000_000 <= r.trace["claim_at_ns"]
                <= r.trace["slot"]*2_000_000_000+250_000_000 for r in replies),
        worker_failed_without_claim=sum(r.trace["outcome"] == "failed"
            and r.trace["claim_at_ns"] is None and r.trace["rpc_count"] > 0 for r in replies),
        no_incumbent_slots=len(absent),
        no_incumbent_commits_per_slot=sum(commits[slot] for slot in absent)/max(1, len(absent)),
        previous_winner_failures=fraction("failed"), previous_winner_missed=fraction("missed"),
        unknown_ratio=sum(t["unknown_outcome"] for t in measured)/max(1, attempts),
        attempts=attempts, median_incumbency_streak=median(streaks) if streaks else 0,
        max_60_slot_commits=max(sum(commits[s] for s in range(slot, slot+60))/60
            for slot in range(START+10, START+slots-59)),
        average_claim_transaction_ms=sum(tx.ended-tx.started for tx in claim_txs)*1000/max(1, len(claim_txs)),
        claim_lock_wait_seconds=sum(tx.lock_wait for tx in claim_txs), aborts=db.aborts,
        published_claim_at_ms=median((t["claim_at_ns"]-t["slot"]*2_000_000_000)/1e6 for t in published)
            if published else None,
        published_observed_at_ms=median((t["observed_at_ns"]-t["slot"]*2_000_000_000)/1e6 for t in published)
            if published else None,
        published_ack_ms=median((t["ack_at_us"]-t["slot"]*2_000_000)/1000 for t in published)
            if published else None,
        max_claim_contenders=max(sum(tx.slot == slot for tx in claim_txs) for slot in commits),
        # Storage-side facts, independent of whether an owner accepted a reply.
        claim_transactions_by_slot=[sum(tx.slot == slot for tx in claim_txs) for slot in sorted(commits)],
        durable_publication_slots=sorted(durable),
        durable_publication_rate=len(durable & commits.keys())/(slots-10),
        stalled_boots=sorted(stalled),
        post_ack_stalls=len(held),
        released=sum(t["unknown_outcome"] and t["cycle_us"] == t["rpc_count"] == 0 for t in measured),
        release_reasons={reason: sum(t.get("release_reason") == reason for t in measured)
            for reason in sorted({t["release_reason"] for t in measured if "release_reason" in t})})


def record_calibration(results):
    """Measurements, not correctness assertions: these paths do not explain production."""
    print(json.dumps(dict(calibration="production", matched=all(
        r["commits_per_slot"] >= 3 and r["no_incumbent_commits_per_slot"] >= 5
        and r["rate"] <= .5 and .30 <= r["previous_winner_failures"] <= .50
        and r["unknown_ratio"] >= .15 for r in results),
        transaction_calibration_matched=all(r["commits_per_slot"] >= 5
        and 150 <= r["average_claim_transaction_ms"] <= 300 for r in results))), flush=True)


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


@pytest.mark.parametrize("ref", ["baseline", None], ids=["baseline", "worktree"])
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


@pytest.fixture(scope="module")
def calibration_elapsed():
    return []


@pytest.mark.parametrize("ref", ["baseline", None], ids=["baseline", "worktree"])
def test_production_calibration(capsys, ref, calibration_elapsed):
    """Record the mismatch with production; keep all mechanism invariants."""
    import time

    if ref:
        ref = baseline_ref()
    started = time.monotonic()
    baseline = []
    with capsys.disabled():
        for post_ack_probability in (0., .4):
            for warm in (False, True):
                result = simulate_lifecycle(ref=ref, warm=warm,
                                            post_ack_probability=post_ack_probability)
                print(json.dumps(dict(ref=ref or "worktree", warm=warm,
                    post_ack_probability=post_ack_probability, **result)), flush=True)
                if ref and post_ack_probability:
                    baseline.append(result)
    elapsed = time.monotonic() - started
    with capsys.disabled():
        print(f"Four 130-slot lifecycle scenarios: {elapsed:.3f} seconds", flush=True)
    # Preserve the original combined eight-scenario runtime bound even though
    # baseline and worktree now have independent skip/reporting outcomes.
    calibration_elapsed.append(elapsed)
    assert sum(calibration_elapsed) < 120
    if ref:
        with capsys.disabled():
            record_calibration(baseline)


@pytest.mark.parametrize("ref", ["baseline", None], ids=["baseline", "worktree"])
def test_post_ack_stall_crosses_boundary_as_missed_not_released(ref):
    result = simulate_lifecycle(ref=ref, slots=70, warm=True, post_ack_probability=1.)
    assert result["post_ack_stalls"] > 0
    assert result["previous_winner_missed"] >= .5
    assert result["previous_winner_failures"] == result["released"] == 0
    assert result["unknown_ratio"] == 0


if __name__ == "__main__":
    baseline = []
    for ref in (None, "baseline"):
        for warm in (False, True):
            result = simulate_lifecycle(ref=ref, warm=warm, post_ack_probability=.4)
            print(json.dumps(dict(ref=ref or "worktree", warm=warm, **result)), flush=True)
            if ref:
                baseline.append(result)
    record_calibration(baseline)


@pytest.mark.parametrize("ref", ["baseline", None], ids=["baseline", "worktree"])
def test_pre_boundary_server_claim_rolls_back_before_owner_validation(ref):
    """A -5ms server stamp cannot become a published reply in these sources."""
    storage, publisher = modules(ref)
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


def test_clock_skew_production_calibration(capsys):
    """Record that clock skew does not explain owner rejection."""
    import time

    ref = baseline_ref()
    started = time.monotonic()
    baseline = []
    with capsys.disabled():
        for offsets in ((-4, -3, -2, -1, 0, 1, 2, 3), (-3,)*8):
            for warm in (False, True):
                result = simulate_lifecycle(ref=ref, warm=warm, clock_offsets_ms=offsets)
                baseline.append(result)
                print(json.dumps(dict(ref=ref, warm=warm, **result)), flush=True)
        print(f"Four skew scenarios: {time.monotonic()-started:.3f} seconds", flush=True)
    assert time.monotonic()-started < 20
    with capsys.disabled():
        print(json.dumps(dict(calibration="clock_skew", matched=all(
            r["invalid_reply_releases"] > 0 and r["invalid_reply_boots"]
            and .20 <= r["previous_winner_failures"] <= .50 and r["rate"] <= .60
            and r["slots_after_rejection"] > 0 and r["commits_after_rejection"] >= 3
            and r["empty_claim_commits"] > 0 and r["unknown_ratio"] > 0 for r in baseline))), flush=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("TR_SHADOW_REALTIME_SIMULATION") != "1",
                    reason="60-slot real-time simulation requires TR_SHADOW_REALTIME_SIMULATION=1")
async def test_real_threads_asyncio_owner_sixty_slots(capsys, caplog):
    await real_threads_asyncio_owner(capsys, caplog, slots=60)


@pytest.mark.asyncio
async def test_real_threads_asyncio_owner_smoke(capsys, caplog):
    # One slot suffices: three real workers can claim, publish, lose and deliver
    # to each owner in that slot. The long run adds sustained cadence coverage.
    await real_threads_asyncio_owner(capsys, caplog, slots=1)


async def real_threads_asyncio_owner(capsys, caplog, *, slots):
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
        def snapshot(self):
            with self.mutex:
                self.worker_threads.add(threading.get_ident())
                yield Reader(self)

        def run_in_transaction(self, callback, **options):
            with self.mutex:
                self.worker_threads.add(threading.get_ident())
                return super().run_in_transaction(callback, **options)

    db = RealDatabase()
    services, tasks, traces = [], [], []
    def capture(day, trace):
        PUBLISHER_SLOT_TRACE(trace)
        traces.append(dict(trace))

    # Begin just after an actual UTC slot boundary, with ordinary wall/monotonic
    # clocks throughout. No patched sleeps or owner-loop facsimile.
    first_slot = int(time.time()/2)+1
    await asyncio.sleep(first_slot*2-time.time())
    started = time.monotonic()
    try:
        for index in range(3):
            settings = Settings(environment="test", async_settle_shadow_policy=POLICY,
                async_settle_shadow_workspaces="ws", source_revision="a"*40)
            settings.async_settle_shadow_health_region = settings.effective_serving_region
            svc = RecordingService(settings, storage.ObservationStore(db))
            svc.journal.boot = f"realtime-{index}"
            if slots == 1:
                # Use the real hash stagger, with a prompt cold candidate and
                # spaced followers. Avoid a wall-clock-dependent boot/slot hash
                # putting every smoke candidate at the observation deadline.
                candidate = 0
                while publisher.follower_stagger(svc.journal.boot, first_slot, False) != index*.09:
                    candidate += 1
                    svc.journal.boot = f"realtime-{index}-{candidate}"
            svc.traces.append = capture
            # This reproduction isolates publishers: no evidence persistence,
            # session-recovery transport or admission observer traffic.
            svc.next_flush = float("inf")
            services.append(svc)
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
    with capsys.disabled():
        print(json.dumps(result), flush=True)
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
