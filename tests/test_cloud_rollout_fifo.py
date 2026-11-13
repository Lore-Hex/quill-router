"""FIFO admission for the shared two-cloud release journal.

Both repositories (control plane and enclave gateway) queue in the same
generation-CAS journal. See docs/design/deploy-fifo-admission.md.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy import cloud_rollout as rollout

QR = "https://github.com/Lore-Hex/quill-router/actions/runs/{}"
QCP = "https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/{}"


class MemoryStore:
    def __init__(self):
        self.record = None
        self.generation = 0
        self.lock = threading.Lock()
        self.writes = 0
        self.max_leases = 0
        self.history = []

    def read(self):
        with self.lock:
            return copy.deepcopy(self.record), self.generation

    def write(self, record, generation):
        with self.lock:
            if generation != self.generation:
                raise rollout.Conflict()
            for cloud, lease in record["leases"].items():
                previous = (self.record or {}).get("leases", {}).get(cloud)
                if previous is None or previous["operation_id"] != lease["operation_id"]:
                    self.history.append((cloud, lease["owner"]))
            self.record = copy.deepcopy(record)
            self.generation += 1
            self.writes += 1
            self.max_leases = max(self.max_leases, len(record["leases"]))


@pytest.fixture
def system():
    store = MemoryStore()
    health = {c: "a" * 40 for c in rollout.CLOUDS}
    clock = [10_000]

    def probe(cloud):
        if health[cloud] is None:
            raise rollout.Refused("unhealthy")
        return health[cloud]

    coordinator = rollout.Coordinator(store, probe, lambda: clock[0])
    return SimpleNamespace(store=store, health=health, clock=clock, coordinator=coordinator)


def queued(system):
    return [e["ticket"] for e in (system.store.record or {}).get("queue", [])]


def enqueue(system, cloud, owner, component="control-plane", wait=3600):
    return system.coordinator.enqueue(cloud, owner, component, wait)


def admit(system, ticket):
    return system.coordinator.try_admit(ticket, 3600)


def hold(system, cloud, owner="manual:holder", component="control-plane"):
    return system.coordinator.acquire(cloud, owner, component, 3600)


# FIFO order -----------------------------------------------------------------

def test_same_cloud_waiters_from_both_repositories_are_admitted_in_arrival_order(system):
    holder = hold(system, "gcp", QR.format(1))
    gateway = enqueue(system, "gcp", QCP.format(2), "gateway")
    control = enqueue(system, "gcp", QR.format(3))
    system.coordinator.release("gcp", holder["operation_id"], True)
    # The later control-plane waiter polls first and must not take the lease.
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        admit(system, control)
    lease = admit(system, gateway)
    assert (lease["owner"], lease["component"], lease["operation_id"]) == (QCP.format(2), "gateway", gateway)
    assert queued(system) == [control]
    with pytest.raises(rollout.Busy, match="already reserved"):
        admit(system, control)
    system.coordinator.release("gcp", gateway, True)
    assert admit(system, control)["owner"] == QR.format(3)
    assert "queue" not in system.store.record


def test_direct_acquire_never_jumps_a_live_waiter(system):
    holder = hold(system, "gcp")
    waiter = enqueue(system, "gcp", QCP.format(2), "gateway")
    system.coordinator.release("gcp", holder["operation_id"], True)
    before = system.store.read()
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        hold(system, "gcp", QR.format(3))
    assert system.store.read() == before
    assert admit(system, waiter)["operation_id"] == waiter


def test_last_free_slot_goes_to_the_earliest_waiter_across_clouds(system):
    gcp = hold(system, "gcp")
    azure = enqueue(system, "azure", QR.format(2))
    aws = enqueue(system, "aws", QR.format(3))
    with pytest.raises(rollout.Busy, match="take the free slot"):
        admit(system, aws)
    admit(system, azure)
    with pytest.raises(rollout.Busy, match="two clouds"):
        admit(system, aws)
    system.coordinator.release("gcp", gcp["operation_id"], True)
    assert admit(system, aws)["cloud"] == "aws"
    assert set(system.store.record["leases"]) == {"aws", "azure"}


def test_two_free_slots_admit_two_earliest_clouds_in_any_poll_order(system):
    aws = enqueue(system, "aws", QR.format(1))
    azure = enqueue(system, "azure", QR.format(2))
    gcp = enqueue(system, "gcp", QCP.format(3), "gateway")
    with pytest.raises(rollout.Busy, match="aws, azure take the free slot"):
        admit(system, gcp)
    admit(system, azure)
    admit(system, aws)
    assert queued(system) == [gcp]


def test_concurrent_waiters_keep_per_cloud_arrival_order_and_never_exceed_two(system):
    arrivals = list(itertools.islice(itertools.cycle(rollout.CLOUDS), 9))
    tickets = [(cloud, enqueue(system, cloud, QR.format(i), ("control-plane", "gateway")[i % 2]))
               for i, cloud in enumerate(arrivals)]
    order = {cloud: [QR.format(i) for i, c in enumerate(arrivals) if c == cloud] for cloud in rollout.CLOUDS}
    start = threading.Barrier(len(tickets))

    def waiter(item):
        cloud, ticket = item
        start.wait(timeout=10)
        while True:
            try:
                lease = admit(system, ticket)
                break
            except rollout.Busy:
                continue
        system.coordinator.release(cloud, lease["operation_id"], True)

    with ThreadPoolExecutor(max_workers=len(tickets)) as workers:
        list(workers.map(waiter, tickets))
    assert system.store.max_leases <= 2
    for cloud in rollout.CLOUDS:
        assert [owner for c, owner in system.store.history if c == cloud] == order[cloud]
    assert system.store.record == {"schema_version": 2, "leases": {}, "holdback": None}


# Per-component exclusion (unchanged: one lease per cloud) --------------------

@pytest.mark.parametrize("first,second", [("gateway", "control-plane"), ("control-plane", "gateway")])
def test_queued_gateway_and_control_plane_on_one_cloud_never_overlap(system, first, second):
    a = enqueue(system, "gcp", QCP.format(1), first)
    b = enqueue(system, "gcp", QR.format(2), second)
    lease = admit(system, a)
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        admit(system, b)
    system.coordinator.release("gcp", lease["operation_id"], True)
    assert admit(system, b)["component"] == second


# Fencing --------------------------------------------------------------------

def test_admission_is_a_single_cas_write_that_moves_ticket_to_lease(system):
    ticket = enqueue(system, "gcp", QR.format(1))
    writes = system.store.writes
    lease = admit(system, ticket)
    assert system.store.writes == writes + 1
    assert system.store.record["leases"]["gcp"]["operation_id"] == ticket == lease["operation_id"]
    assert "queue" not in system.store.record


def test_polling_without_a_due_heartbeat_does_not_write(system):
    hold(system, "gcp")
    ticket = enqueue(system, "gcp", QR.format(1))
    writes = system.store.writes
    for _ in range(5):
        with pytest.raises(rollout.Busy):
            admit(system, ticket)
    assert system.store.writes == writes
    system.clock[0] += rollout.HEARTBEAT_SECONDS
    with pytest.raises(rollout.Busy):
        admit(system, ticket)
    assert system.store.writes == writes + 1
    assert system.store.record["queue"][0]["heartbeat_at"] == system.clock[0]


def test_lost_cas_rereads_and_never_overwrites_a_peer(system, monkeypatch):
    ticket = enqueue(system, "gcp", QR.format(1))
    write = system.store.write
    raced = []

    def racing_write(record, generation):
        if not raced:
            raced.append(True)
            # A peer joins the queue between this client's read and write.
            peer = rollout.Coordinator(system.store, system.coordinator.probe, lambda: system.clock[0])
            peer.enqueue("aws", QR.format(2), "control-plane", 3600)
        return write(record, generation)

    monkeypatch.setattr(system.store, "write", racing_write)
    admit(system, ticket)
    assert [e["cloud"] for e in system.store.record["queue"]] == ["aws"]
    assert system.store.record["leases"]["gcp"]["operation_id"] == ticket


def test_contention_is_bounded_and_leaves_journal_unchanged(system, monkeypatch):
    hold(system, "gcp")
    before = system.store.read()

    def conflict(*_):
        raise rollout.Conflict()

    monkeypatch.setattr(system.store, "write", conflict)
    with pytest.raises(rollout.Refused, match="contention"):
        enqueue(system, "gcp", QR.format(1))
    assert system.store.read() == before


def test_prune_is_fenced_by_the_heartbeat_it_checked(system):
    hold(system, "gcp")
    ticket = enqueue(system, "gcp", QR.format(1))
    system.clock[0] += rollout.WAITER_STALE_SECONDS

    def stopped(entry):
        # The owner looked finished, but the waiter heartbeats meanwhile.
        with pytest.raises(rollout.Busy):
            admit(system, ticket)
        return True

    assert system.coordinator.prune(stopped) == 0
    assert queued(system) == [ticket]


def test_prune_removes_only_stale_waiters_whose_owner_stopped(system):
    hold(system, "gcp")
    dead = enqueue(system, "gcp", QR.format(1))
    running = enqueue(system, "gcp", QCP.format(2), "gateway")
    system.clock[0] += rollout.WAITER_STALE_SECONDS
    fresh = enqueue(system, "gcp", QR.format(3))
    checked = []

    def stopped(entry):
        checked.append(entry["owner"])
        return entry["owner"] != QCP.format(2)

    assert system.coordinator.prune(stopped) == 1
    assert checked == [QR.format(1), QCP.format(2)]  # a fresh waiter is never even checked
    assert queued(system) == [running, fresh]
    assert dead not in queued(system)


def test_dequeue_removes_only_its_own_ticket_and_never_a_lease(system):
    lease = hold(system, "gcp")
    mine = enqueue(system, "gcp", QR.format(1))
    other = enqueue(system, "gcp", QR.format(2))
    system.coordinator.dequeue(mine)
    assert queued(system) == [other]
    writes = system.store.writes
    system.coordinator.dequeue(mine)
    system.coordinator.dequeue(lease["operation_id"])
    assert system.store.writes == writes
    assert system.store.record["leases"]["gcp"] == lease


def test_ambiguous_admission_write_keeps_the_lease_for_recovery(system, monkeypatch):
    ticket = enqueue(system, "gcp", QR.format(1))
    write = system.store.write

    def lost_response(record, generation):
        write(record, generation)
        raise rollout.Refused("ambiguous")

    monkeypatch.setattr(system.store, "write", lost_response)
    with pytest.raises(rollout.Refused, match="ambiguous"):
        admit(system, ticket)
    assert system.store.record["leases"]["gcp"]["operation_id"] == ticket
    monkeypatch.setattr(system.store, "write", write)
    system.coordinator.dequeue(ticket)
    assert system.store.record["leases"]["gcp"]["operation_id"] == ticket


# Dead waiters ---------------------------------------------------------------

def test_silent_waiter_loses_its_turn_but_is_not_deleted(system):
    holder = hold(system, "gcp")
    silent = enqueue(system, "gcp", QR.format(1))
    later = enqueue(system, "gcp", QCP.format(2), "gateway")
    system.coordinator.release("gcp", holder["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        admit(system, later)
    system.clock[0] += rollout.WAITER_STALE_SECONDS
    assert admit(system, later)["operation_id"] == later
    assert queued(system) == [silent]


def test_stale_waiter_regains_its_place_when_it_heartbeats(system):
    holder = hold(system, "gcp")
    first = enqueue(system, "gcp", QR.format(1))
    second = enqueue(system, "gcp", QCP.format(2), "gateway")
    system.clock[0] += rollout.WAITER_STALE_SECONDS
    with pytest.raises(rollout.Busy, match="already reserved"):
        admit(system, first)  # heartbeat written: live again
    with pytest.raises(rollout.Busy):
        admit(system, second)
    system.coordinator.release("gcp", holder["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        admit(system, second)
    assert admit(system, first)["operation_id"] == first


def test_waiter_past_its_own_deadline_is_refused(system):
    hold(system, "gcp")
    ticket = enqueue(system, "gcp", QR.format(1), wait=600)
    entry = system.store.record["queue"][0]
    assert entry["expires_at"] == entry["enqueued_at"] + 600 + rollout.WAITER_DEADLINE_MARGIN
    system.clock[0] = entry["expires_at"]
    with pytest.raises(rollout.Refused, match="deadline passed"):
        admit(system, ticket)
    assert queued(system) == [ticket]


def test_observer_deletes_a_waiter_only_after_seeing_it_silent_on_its_own_clock(system):
    hold(system, "gcp")
    dead = enqueue(system, "gcp", QR.format(1))
    observer = enqueue(system, "gcp", QCP.format(2), "gateway")
    with pytest.raises(rollout.Busy):
        admit(system, observer)  # first sighting of the silent waiter
    for _ in range(rollout.OBSERVED_DEAD_SECONDS // rollout.HEARTBEAT_SECONDS):
        system.clock[0] += rollout.HEARTBEAT_SECONDS
        with pytest.raises(rollout.Busy):
            admit(system, observer)
    assert dead in queued(system)
    system.clock[0] += rollout.HEARTBEAT_SECONDS
    with pytest.raises(rollout.Busy):
        admit(system, observer)
    assert queued(system) == [observer]


def test_a_fast_clock_never_drops_a_live_waiter(system):
    # Review probe 4: a host about 8 minutes fast expired a live waiter.
    hold(system, "gcp")
    live = enqueue(system, "gcp", QR.format(1), wait=60)
    skew = 60 + rollout.WAITER_DEADLINE_MARGIN + 300 + 1
    fast = rollout.Coordinator(system.store, system.coordinator.probe, lambda: system.clock[0] + skew)
    fast.enqueue("aws", "manual:other", "control-plane", 3600)
    assert live in queued(system)
    with pytest.raises(rollout.Busy, match="already reserved"):
        admit(system, live)


def test_a_fast_clock_never_skips_a_live_waiter(system):
    holder = hold(system, "gcp")
    live = enqueue(system, "gcp", QR.format(1), wait=3600)
    skew = rollout.WAITER_STALE_SECONDS + 300
    fast = rollout.Coordinator(system.store, system.coordinator.probe, lambda: system.clock[0] + skew)
    # A fast poller behind it for the same cloud observes for ten minutes
    # while the live waiter keeps heartbeating on its own clock.
    poller = fast.enqueue("gcp", QCP.format(3), "gateway", 3600)
    for _ in range(20):
        system.clock[0] += 30
        with pytest.raises(rollout.Busy):
            admit(system, live)
        with pytest.raises(rollout.Busy):
            fast.try_admit(poller, 3600)
    system.coordinator.release("gcp", holder["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        fast.try_admit(poller, 3600)
    assert admit(system, live)["operation_id"] == live


def test_a_day_past_its_deadline_any_writer_may_drop_a_waiter(system):
    ticket = enqueue(system, "gcp", QR.format(1), wait=60)
    expires = system.store.record["queue"][0]["expires_at"]
    system.clock[0] = expires + rollout.WAITER_ABANDONED_SECONDS - 1
    other = enqueue(system, "aws", QR.format(2))
    assert queued(system) == [ticket, other]
    system.clock[0] += 1
    third = enqueue(system, "azure", QR.format(3))
    assert queued(system) == [other, third]


def test_removed_ticket_is_never_admitted(system):
    ticket = enqueue(system, "gcp", QR.format(1))
    system.coordinator.dequeue(ticket)
    with pytest.raises(rollout.Refused, match="no longer queued"):
        admit(system, ticket)
    assert system.store.record == {"schema_version": 2, "leases": {}, "holdback": None}


def test_owner_status_decides_whether_a_waiter_is_dead(monkeypatch):
    statuses = {"1": "completed", "2": "in_progress", "3": "queued"}

    def run(args, **kwargs):
        if args[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(args, 0, "ghp_fake_token\n", "")
        run_id = args[-1].rsplit("/", 1)[1]
        return subprocess.CompletedProcess(args, 0, json.dumps({"status": statuses[run_id]}), "")

    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(rollout.subprocess, "run", run)
    waiter = {"cloud": "gcp", "component": "gateway", "enqueued_at": 1, "heartbeat_at": 1,
              "expires_at": 99_999_999_999, "host": "runner", "pid": 0, "ticket": "a" * 32}
    assert rollout.owner_stopped({**waiter, "owner": QCP.format(1)}) is True
    assert rollout.owner_stopped({**waiter, "owner": QR.format(2)}) is False
    assert rollout.owner_stopped({**waiter, "owner": QR.format(3)}) is False
    assert rollout.owner_stopped({**waiter, "owner": "manual:laptop"}) is False


# Lease checks are not disturbed by queue traffic ----------------------------

def test_waiter_heartbeat_during_a_lease_check_does_not_fail_it(system):
    lease = hold(system, "gcp")
    ticket = enqueue(system, "gcp", QCP.format(1), "gateway")
    probe = system.coordinator.probe

    def heartbeat_during_probe(cloud):
        system.clock[0] += rollout.HEARTBEAT_SECONDS
        with pytest.raises(rollout.Busy):
            admit(system, ticket)
        return probe(cloud)

    system.coordinator.probe = heartbeat_during_probe
    assert system.coordinator.check("gcp", lease["operation_id"]) == lease


def test_lease_change_during_a_check_still_refuses(system):
    lease = hold(system, "gcp")
    probe = system.coordinator.probe
    peer = rollout.Coordinator(system.store, probe, lambda: system.clock[0])

    def lease_churn_during_probe(cloud):
        # Every verification window sees a peer lease appear or disappear.
        leases = system.store.record["leases"]
        if "aws" in leases:
            peer.release("aws", leases["aws"]["operation_id"], True)
        else:
            peer.acquire("aws", "manual:peer", "control-plane", 3600)
        return probe(cloud)

    system.coordinator.probe = lease_churn_during_probe
    with pytest.raises(rollout.Refused, match="changed during guard"):
        system.coordinator.check("gcp", lease["operation_id"])


# Journal format -------------------------------------------------------------

def test_empty_queue_leaves_the_exact_v2_record(system):
    ticket = enqueue(system, "gcp", QR.format(1))
    assert len(system.store.record["queue"]) == 1
    system.coordinator.dequeue(ticket)
    assert system.store.record == {"schema_version": 2, "leases": {}, "holdback": None}


def valid_entry(**changes):
    entry = {"ticket": "b" * 32, "cloud": "gcp", "component": "gateway", "owner": QCP.format(1),
             "enqueued_at": 1, "heartbeat_at": 1, "expires_at": 2, "host": "h", "pid": 0}
    entry.update(changes)
    return entry


@pytest.mark.parametrize("queue", [
    {}, "x", [None], [valid_entry(ticket="short")], [valid_entry(cloud="mars")],
    [valid_entry(component="other")], [valid_entry(owner="")], [valid_entry(owner="a\nb")],
    [valid_entry(enqueued_at=2)], [valid_entry(heartbeat_at=0)], [valid_entry(expires_at=1)],
    [valid_entry(pid="0")], [valid_entry(host=None)], [valid_entry(heartbeat_at=1.5)],
])
def test_malformed_waiters_are_ignored_and_never_block_lease_operations(system, queue, capsys):
    lease = hold(system, "gcp")
    system.store.record["queue"] = queue
    assert "queue" not in rollout.state_record(system.store.record)
    assert system.coordinator.check("gcp", lease["operation_id"]) == lease
    assert "queue_ignored" in capsys.readouterr().err
    system.coordinator.release("gcp", lease["operation_id"], False)
    system.coordinator.recover("gcp", lease["operation_id"], lambda _: True)
    assert system.store.record == {"schema_version": 2, "leases": {}, "holdback": None}


def test_repeated_and_overflowing_waiters_are_deduplicated_not_fatal():
    state = rollout.state_record({"schema_version": 2, "leases": {}, "holdback": None, "queue": [
        valid_entry(), valid_entry(), *[valid_entry(ticket=f"{i:032x}") for i in range(rollout.MAX_QUEUE + 3)]]})
    tickets = [e["ticket"] for e in state["queue"]]
    assert len(tickets) == rollout.MAX_QUEUE == len(set(tickets))
    assert tickets[0] == "b" * 32


def test_waiter_equal_to_a_lease_fence_is_dropped_and_the_lease_kept():
    lease = {"cloud": "gcp", "owner": "o", "component": "gateway", "operation_id": "b" * 32,
             "state": "active", "created_at": 1, "expires_at": 2}
    state = rollout.state_record({"schema_version": 2, "leases": {"gcp": lease},
                                  "holdback": {"cloud": "aws", "release": "a" * 40},
                                  "queue": [valid_entry()]})
    assert "queue" not in state
    assert state["leases"]["gcp"] == lease


def test_full_queue_refuses_new_waiters(system):
    for i in range(rollout.MAX_QUEUE):
        enqueue(system, "gcp", QR.format(i))
    with pytest.raises(rollout.Refused, match="full"):
        enqueue(system, "gcp", QR.format(99))


# Client loop ----------------------------------------------------------------

class FakeTime:
    def __init__(self, system, on_sleep=None):
        self.system, self.on_sleep, self.t = system, on_sleep, 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.system.clock[0] += int(seconds)
        if self.on_sleep:
            self.on_sleep(self)


def test_client_waits_its_turn_then_holds_the_lease(system):
    holder = hold(system, "gcp", QCP.format(1), "gateway")

    def release_later(fake):
        if fake.t >= 100 and "gcp" in system.store.record["leases"] \
                and system.store.record["leases"]["gcp"]["owner"] == QCP.format(1):
            system.coordinator.release("gcp", holder["operation_id"], True)

    fake = FakeTime(system, release_later)
    lease = rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600, 600,
                                     stopped=lambda _: False, sleep=fake.sleep, monotonic=fake.monotonic)
    assert lease["owner"] == QR.format(2)
    assert "queue" not in system.store.record
    assert 100 <= fake.t < 200


def test_client_never_logs_its_ticket_which_becomes_the_lease_fence(system, capsys):
    holder = hold(system, "gcp", QCP.format(1), "gateway")

    def release_later(fake):
        if "gcp" in system.store.record["leases"] and \
                system.store.record["leases"]["gcp"]["operation_id"] == holder["operation_id"]:
            system.coordinator.release("gcp", holder["operation_id"], True)

    fake = FakeTime(system, release_later)
    lease = rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600, 600,
                                     stopped=lambda _: False, sleep=fake.sleep, monotonic=fake.monotonic)
    output = capsys.readouterr()
    assert "cloud_rollout.queued" in output.err
    assert lease["operation_id"] not in output.err + output.out


def test_client_that_times_out_removes_only_its_own_ticket(system):
    hold(system, "gcp")
    other = enqueue(system, "gcp", QCP.format(1), "gateway")
    fake = FakeTime(system)
    with pytest.raises(rollout.Busy):
        rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600, 120,
                                 stopped=lambda _: False, sleep=fake.sleep, monotonic=fake.monotonic)
    assert queued(system) == [other]
    assert fake.t == 120


def test_client_prunes_stopped_waiters_ahead_of_it(system):
    hold(system, "gcp")
    dead = enqueue(system, "gcp", QR.format(1))
    system.clock[0] += rollout.WAITER_STALE_SECONDS

    def stopped(entry):
        return entry["ticket"] == dead

    fake = FakeTime(system)
    with pytest.raises(rollout.Busy):
        rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600,
                                 rollout.PRUNE_INTERVAL_SECONDS + 60, stopped=stopped,
                                 sleep=fake.sleep, monotonic=fake.monotonic)
    assert dead not in queued(system)


def test_no_wait_is_a_single_attempt_that_still_respects_the_queue(system):
    waiter = enqueue(system, "gcp", QCP.format(1), "gateway")
    fake = FakeTime(system)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600, 0,
                                 sleep=fake.sleep, monotonic=fake.monotonic)
    assert fake.t == 0
    assert queued(system) == [waiter]


# Writes that land but report a conflict (gcloud's own precondition retry) ---

class LandsThenConflicts:
    """Wraps a store so chosen writes land and then raise Conflict."""

    def __init__(self, store, times=1):
        self.store, self.times = store, times

    def read(self):
        return self.store.read()

    def write(self, record, generation):
        self.store.write(record, generation)
        if self.times:
            self.times -= 1
            raise rollout.Conflict("journal compare-and-swap lost")


def test_enqueue_that_lands_then_conflicts_is_queued_exactly_once(system):
    flaky = rollout.Coordinator(LandsThenConflicts(system.store), system.coordinator.probe, lambda: system.clock[0])
    ticket = flaky.enqueue("gcp", QR.format(1), "control-plane", 3600, ticket="c" * 32)
    assert queued(system) == [ticket]
    # The journal stays usable for everyone.
    other = hold(system, "aws")
    system.coordinator.check("aws", other["operation_id"])
    system.coordinator.dequeue(ticket)
    assert "queue" not in system.store.record


def test_admission_that_lands_then_conflicts_returns_the_granted_lease(system):
    ticket = enqueue(system, "gcp", QR.format(1))
    flaky = rollout.Coordinator(LandsThenConflicts(system.store), system.coordinator.probe, lambda: system.clock[0])
    lease = flaky.try_admit(ticket, 3600)
    assert lease["operation_id"] == ticket
    assert system.store.record["leases"]["gcp"] == lease
    assert "queue" not in system.store.record


def test_ambiguous_enqueue_in_the_client_loop_is_undone(system, monkeypatch):
    hold(system, "gcp")
    write = system.store.write
    calls = []

    def lands_then_errors(record, generation):
        write(record, generation)
        calls.append(1)
        if len(calls) == 1:
            raise rollout.Refused("journal write failed or ambiguous; never delete on ambiguity")

    monkeypatch.setattr(system.store, "write", lands_then_errors)
    fake = FakeTime(system)
    with pytest.raises(rollout.Refused, match="ambiguous"):
        rollout.acquire_in_order(system.coordinator, "gcp", QR.format(2), "control-plane", 3600, 600,
                                 stopped=lambda _: False, sleep=fake.sleep, monotonic=fake.monotonic)
    assert "queue" not in system.store.record


def test_old_coordinator_against_a_queued_journal_keeps_the_queue(system, tmp_path):
    # The exact coordinator quill-cloud-proxy runs today (its pinned digest).
    fixture = Path(__file__).parent / "fixtures" / "cloud_rollout_200f758.py.txt"
    source = fixture.read_bytes()
    assert hashlib.sha256(source).hexdigest() == "63579c677b28ad05e81461f43f2535231007e102a3980d77b636783860e2c7b9"
    path = tmp_path / "old_cloud_rollout.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("old_cloud_rollout", path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    ticket = enqueue(system, "gcp", QR.format(1))
    legacy = old.Coordinator(system.store, system.coordinator.probe, lambda: system.clock[0])
    lease = legacy.acquire("gcp", QCP.format(9), "gateway", 3600)  # unfair, not unsafe
    assert queued(system) == [ticket]
    legacy.check("gcp", lease["operation_id"])
    legacy.release("gcp", lease["operation_id"], True)
    assert queued(system) == [ticket]
    assert admit(system, ticket)["operation_id"] == ticket


def test_status_shortens_tickets_and_keeps_lease_fences(system, monkeypatch, capsys):
    lease = hold(system, "gcp")
    ticket = enqueue(system, "gcp", QR.format(1))
    monkeypatch.setattr(rollout, "GCSStore", lambda: system.store)
    monkeypatch.setattr(sys, "argv", ["cloud_rollout.py", "status"])
    assert rollout.main() == 0
    output = capsys.readouterr().out
    assert ticket not in output
    assert ticket[:6] + "..." in output
    assert lease["operation_id"] in output
    assert system.store.record["queue"][0]["ticket"] == ticket


# Observation margins ---------------------------------------------------------

def test_a_waiter_at_its_longest_heartbeat_gap_is_never_skipped(system):
    # A polling waiter heartbeats when due (120 s), then may spend one bounded
    # prune pass (60 s) and a poll (20 s) before its next write: 200 s, plus
    # write latency. A fast-clocked observer that polls every 10 s (so the
    # cross-clock hint already calls the waiter stale) must never skip it.
    holder = hold(system, "gcp")
    live = enqueue(system, "gcp", QR.format(1), wait=3600)
    skew = rollout.WAITER_STALE_SECONDS + 60
    fast = rollout.Coordinator(system.store, system.coordinator.probe, lambda: system.clock[0] + skew)
    observer = fast.enqueue("gcp", QCP.format(2), "gateway", 3600)
    gap = rollout.HEARTBEAT_SECONDS + rollout.PRUNE_BUDGET_SECONDS + 20 + 10
    for cycle in range(3):
        for _ in range(gap // 10 - 1):
            system.clock[0] += 10
            with pytest.raises(rollout.Busy):
                fast.try_admit(observer, 3600)
        system.clock[0] += 9
        if cycle < 2:
            system.clock[0] += 1
            with pytest.raises(rollout.Busy):
                admit(system, live)  # the live waiter's next heartbeat
    # Worst moment: the lease frees one second before that waiter's next write.
    system.coordinator.release("gcp", holder["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        fast.try_admit(observer, 3600)
    assert admit(system, live)["operation_id"] == live


def test_a_prune_pass_is_bounded_in_time_and_in_checks(system):
    hold(system, "gcp")
    stale = [enqueue(system, "gcp", QR.format(i)) for i in range(6)]
    system.clock[0] += rollout.WAITER_STALE_SECONDS
    elapsed = [0.0]
    checked = []

    def slow_stopped(entry):
        checked.append(entry["ticket"])
        elapsed[0] += 25
        return True

    removed = system.coordinator.prune(slow_stopped, monotonic=lambda: elapsed[0])
    # Checks start at 0, 25 and 50 s; none starts at 75 s (budget 60 s).
    assert removed == len(checked) == 3
    assert queued(system) == stale[3:]
    checked.clear()
    removed = system.coordinator.prune(lambda entry: checked.append(entry["ticket"]) or True,
                                       monotonic=lambda: 0.0)
    assert removed == len(checked) == 3


def test_a_prune_pass_never_checks_more_than_its_cap(system):
    hold(system, "gcp")
    for i in range(rollout.PRUNE_MAX_CHECKS + 3):
        enqueue(system, "gcp", QR.format(i))
    system.clock[0] += rollout.WAITER_STALE_SECONDS
    checked = []
    removed = system.coordinator.prune(lambda entry: checked.append(entry["ticket"]) or True,
                                       monotonic=lambda: 0.0)
    assert removed == len(checked) == rollout.PRUNE_MAX_CHECKS
