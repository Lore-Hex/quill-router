"""Run the actual publisher/storage modules on a deterministic virtual clock.

Eight cooperating threads execute real synchronous callbacks. Only one thread
runs at a time; sleeps and RPCs yield to the event queue, so 130 two-second slots
need no wall-clock sleeps. The claim row is held across the entire transaction,
including an empty Commit. Reads cost 20 ms, commits 33 ms. Snapshot timestamps
are selected at RPC start. Publication also locks the claim row for its fence.

This deliberately does not invent extra lock delay to force a negative control.
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

from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import Aborted
from google.protobuf.timestamp_pb2 import Timestamp

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


class Database:
    def __init__(self, clock, storage):
        self.clock, self.storage = clock, storage
        self.rows = {}
        self.locked = False
        self.claim_commits = 0
        self.claim_transactions = 0
        self.aborts = 0
        self.inject_abort = False

    @contextmanager
    def snapshot(self):
        yield Reader(self)

    def run_in_transaction(self, callback, **options):
        # The lock includes the entire callback and commit. Readers waiting
        # behind a writer read the new committed version when they acquire it.
        while self.locked:
            self.clock.sleep(.001)
        self.locked = True
        tx = Reader(self, transaction=True)
        try:
            result = callback(tx)
            self.clock.sleep(.033)
            if self.inject_abort and tx.mutations:
                self.inject_abort = False
                self.aborts += 1
                # Exercise the real wrapper's one-attempt ABORTED handling.
                raise Aborted("conflicting writer")
            if tx.claim:
                self.claim_commits += 1
            self.rows.update(tx.mutations)
            return result
        finally:
            self.locked = False


class Reader:
    def __init__(self, database, transaction=False):
        self.db, self.transaction = database, transaction
        self.mutations = {}
        self.claim = False

    def execute_sql(self, sql, *, params, **options):
        db = self.db
        ns = round(db.clock.now*1e9)
        stamp = DatetimeWithNanoseconds.from_timestamp_pb(
            Timestamp(seconds=ns//10**9, nanos=ns%10**9))
        body = db.rows.get((params.get("kind"), params.get("id")))
        if sql == db.storage.CLAIM_SQL:
            self.claim = True
            db.claim_transactions += 1
            rows = [(body, stamp)]
        elif sql == db.storage.UNRESOLVED_SQL:
            rows = []
        else:
            rows = [] if body is None else [(body,)]
        db.clock.sleep(.020)
        class Stream(list):
            metadata = SimpleNamespace(transaction=SimpleNamespace(read_timestamp=stamp))
        return Stream(rows)

    def insert_or_update(self, *, table, columns, values):
        for kind, key, body, _ in values:
            self.mutations[kind, key] = body


def simulate(n=8, *, warm=False, slots=130, ref=None, abort=False):
    storage, publisher = modules(ref)
    clock = Clock()
    publisher.sleep = clock.sleep
    db = Database(clock, storage)
    boots = [f"boot-{i}" for i in range(n)]
    incumbent = boots[0] if warm else None
    seen = dict.fromkeys(boots)
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
            for index, boot in enumerate(boots):
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
                        admission, boot == incumbent, present)
                futures.append(pool.submit(clock.worker, lambda args=args: publisher.publish_cycle(*args), index))
            clock.run(n)
            replies = [future.result() for future in futures]
            winners = []
            for boot, reply in zip(boots, replies, strict=True):
                trace = reply.trace
                PUBLISHER_SLOT_TRACE(trace)
                seen[boot] = getattr(reply, "seen_claim_slot", None)
                if trace["outcome"] == "published":
                    winners.append(boot)
                    max_observed = max(max_observed, (trace["observed_at_ns"]-slot*2_000_000_000)//1000)
                    if first is None:
                        first, first_ack = offset, trace["ack_at_us"]-slot*2_000_000
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
    return dict(slots=slots, measured_slots=measured, published=sum(publications[10:]),
        rate=sum(publications[10:])/measured, claim_transactions=db.claim_transactions,
        max_claim_transactions_per_slot=max(transactions),
        claim_commits=sum(commits[10:]), commits_per_slot=sum(commits[10:])/measured,
        max_60_slot_commits=max(sum(commits[i:i+60])/60 for i in range(10, slots-59)),
        unknowns=unknowns, attempts=attempts, unknown_ratio=unknowns/max(1, attempts),
        first_published_slot_offset=first,
        first_incumbent_ms=None if first is None else first*2000+first_ack/1000,
        max_published_observation_ms=max_observed/1000, aborts=db.aborts)


if __name__ == "__main__":
    baseline = []
    for ref in ("origin/main", None):
        for warm in (False, True):
            result = simulate(ref=ref, warm=warm)
            print(json.dumps(dict(ref=ref or "worktree", warm=warm, **result)), flush=True)
            if ref:
                baseline.append(result)
    assert all(r["commits_per_slot"] >= 5 and r["rate"] <= .5 for r in baseline), (
        "Negative control failed: serialized 20 ms reads / 33 ms commits do not "
        "reproduce the production convoy. Do not treat this model as differential proof."
    )
