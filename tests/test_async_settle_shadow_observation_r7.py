"""P3 lifetime, attempt admission, trace ordering and retained-byte proofs.

Not claimed here: regional latency/CPU, fleet churn and activation budgets (P6);
report joins and G1–G11/window evaluation (P5); certificate issuance (P4b).
"""
from __future__ import annotations

import asyncio
import pickle
import random
import threading

import pytest

from tests.shadow_observation_dormancy import dormancy_runs  # noqa: F401
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    service,
)
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.services.async_settle_shadow_health import PublisherReply


def missed(svc, slot):
    return dict(v=2, policy_version="shadow-v2", contract_revision="a1", day=m.day_name(slot*m.SLOT_US),
        boot=svc.journal.boot, slot=slot, claim_start_us=None, claim_at_ns=None, observed_at_ns=None,
        publisher_heartbeat_ns=None, ack_at_us=None, cycle_us=None, body_sha256=None, outcome="missed",
        unknown_outcome=False, late_cycle=False, rpc_count=0, rpc_elapsed_us=0)


def entries(svc):
    return [e for item in svc.traces.queue for e in svc.traces.schema.parse(item.body)["entries"]]


def audit(state, first, latest):
    for predicate in (m.conservation, m.final_barrier, m.one_final, m.bounded,
                      m.no_post_final_owners, m.accounted_once, m.never_reopens, m.lifetime_coverage):
        assert predicate(state), predicate.__name__
    # Expected endpoints come from input events, not the model's day roster.
    if first is not None:
        assert state.first_slot == first and state.accepted_stamp == latest
        assert sum(d.scheduled+len(d.publishers) for d in state.days)+state.retired_slots == latest//m.SLOT_US-first+1
        for day in state.days:
            assert day.day == m.day_name(day.first_slot*m.SLOT_US)
    assert len(pickle.dumps(state)) < 16_384
    def contains_bytes(value):
        if isinstance(value, bytes):
            return True
        if isinstance(value, (tuple, list, frozenset)):
            return any(contains_bytes(item) for item in value)
        return False
    assert not any(contains_bytes(value) for d in state.days for value in vars(d).values())


def test_shutdown_enumerates_midnight_before_retirement():
    midnight = NOW_US+m.DAY_US
    state, _ = m.step(m.State(), m.Tick(midnight-m.SLOT_US))
    state, effects = m.step(state, m.ShutdownRequested(midnight+500_000))
    audit(state, midnight//m.SLOT_US-1, midnight+500_000)
    assert state.shutdown_stamp == midnight+500_000
    assert len(state.days) == 2
    assert not [e for e in effects if isinstance(e, m.EmitTrace) and e.slot == midnight//m.SLOT_US-1]


@pytest.mark.asyncio
async def test_async_shutdown_enumerates_midnight():
    svc = service()
    wall = [NOW_US+m.DAY_US-m.SLOT_US]
    svc.store.wall = svc.observer.evidence_wall = lambda: wall[0]/1e6
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    svc.tick(wall[0]+500_000, publisher=True)
    wall[0] += m.SLOT_US+500_000
    svc.closing_reason, svc.interval_end_us = "shutdown", wall[0]
    await svc.drive_async(svc._flush(svc.store.clock()+4))
    assert svc.state.shutdown and len(svc.state.days) == 2
    assert sum(d.scheduled for d in svc.state.days) == 2
    assert [e["slot"] for e in entries(svc)] == [wall[0]//m.SLOT_US-1, wall[0]//m.SLOT_US]


@pytest.mark.parametrize("later", [1, 7])
@pytest.mark.asyncio
async def test_async_queued_trace_frontier(later):
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.loop = asyncio.get_running_loop()
    wall, mono = [NOW_US], [100.]
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    svc.tick(wall[0], publisher=True)
    entered, release = asyncio.Event(), threading.Event()
    def blocker():
        svc.loop.call_soon_threadsafe(entered.set)
        release.wait()
    held = svc.publisher_executor.submit(blocker)
    try:
        await entered.wait()
        svc.submit_publisher(wall[0]//m.SLOT_US, wall[0])
        for i in range(1, later+1):
            wall[0], mono[0] = NOW_US+i*m.SLOT_US, 100.+2*i
            svc.tick(wall[0])
            assert entries(svc) == []
    finally:
        release.set()
    await svc.publisher_job
    held.result()
    assert [e["slot"] for e in entries(svc)] == list(range(NOW_US//m.SLOT_US, NOW_US//m.SLOT_US+later+1))
    assert all(e["outcome"] == "missed" for e in entries(svc))


@pytest.mark.asyncio
async def test_async_revoked_generation_never_calls_store(monkeypatch):
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.loop = asyncio.get_running_loop()
    svc.store.wall = lambda: NOW_US/1e6
    svc.tick(NOW_US, publisher=True)
    entered, release = asyncio.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(svc.store, "claim", lambda *a: calls.append(a))
    def blocker():
        svc.loop.call_soon_threadsafe(entered.set)
        release.wait()
    held = svc.publisher_executor.submit(blocker)
    try:
        await entered.wait()
        svc.submit_publisher(NOW_US//m.SLOT_US, NOW_US)
        svc.settings.async_settle_shadow_policy = ""
        assert not svc.enabled()
    finally:
        release.set()
    await svc.publisher_job
    held.result()
    assert calls == [] and svc.state.days[0].missed == 1
    assert entries(svc)[0]["outcome"] == "missed"


@pytest.mark.asyncio
async def test_async_actual_start_spacing(monkeypatch):
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.loop = asyncio.get_running_loop()
    wall, mono, claims = [NOW_US], [100.], []
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    svc.tick(wall[0], publisher=True)
    entered, release = asyncio.Event(), threading.Event()
    def blocker():
        svc.loop.call_soon_threadsafe(entered.set)
        release.wait()
    held = svc.publisher_executor.submit(blocker)
    original = svc.store.claim
    def claim(*a, **kw):
        claims.append(mono[0])
        return original(*a, **kw)
    monkeypatch.setattr(svc.store, "claim", claim)
    try:
        await entered.wait()
        svc.submit_publisher(wall[0]//m.SLOT_US, wall[0])
        wall[0], mono[0] = NOW_US+200_000, 100.2
    finally:
        release.set()
    await svc.publisher_job
    held.result()
    for i in (1, 2):
        wall[0], mono[0] = NOW_US+i*m.SLOT_US, 100.+2*i
        svc.store.database.server_ns = wall[0]*1000
        svc.tick(wall[0])
        svc.submit_publisher(wall[0]//m.SLOT_US, wall[0])
        await svc.publisher_job
    assert claims == [100.2, 104.]
    assert all(b-a >= 2. for a, b in zip(claims, claims[1:], strict=False))
    assert [e["outcome"] for e in entries(svc)] == ["published", "missed", "published"]


@pytest.mark.asyncio
async def test_async_dispatch_retains_origin_day(monkeypatch):
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.loop = asyncio.get_running_loop()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    wall, mono = [NOW_US+m.DAY_US-m.SLOT_US], [100.]
    svc.store.wall = svc.observer.evidence_wall = lambda: wall[0]/1e6
    svc.store.clock = lambda: mono[0]
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    submit = svc.submit_publisher
    def delayed(slot, stamp):
        wall[0] += 2_500_000
        mono[0] += 2.5
        submit(slot, stamp)
    monkeypatch.setattr(svc, "submit_publisher", delayed)
    await svc.drive_async(svc._flush(104.))
    await svc.publisher_job
    assert svc.publisher_job.done() and svc.publisher_faults == 0
    trace = entries(svc)[0]
    assert trace["slot"] == (NOW_US+m.DAY_US)//m.SLOT_US-1
    assert trace["day"] == "2026-10-12" and trace["outcome"] == "missed"


def test_reply_identity_precedes_all_mutation():
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6
    svc.tick(NOW_US, publisher=True)
    slot = NOW_US//m.SLOT_US
    original = svc.state
    for trace, generation in ((dict(missed(svc, slot), day="2026-10-13"), 0),
                              (missed(svc, slot+1), 0), (missed(svc, slot), 9)):
        svc.publisher_reply(PublisherReply(trace, (), False, generation))
        assert svc.state == original and not entries(svc)
    assert svc.publisher_faults == 3
    svc.publisher_reply(PublisherReply(missed(svc, slot), (), False))
    completed = svc.state
    svc.publisher_reply(PublisherReply(missed(svc, slot), (), False))
    assert svc.state == completed and svc.publisher_faults == 4


@exhaustive
def test_2000_unexported_replies_have_one_bounded_copy(record_property):
    svc = service()
    wall, mono = [NOW_US], [100.]
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    for i in range(2000):
        wall[0], mono[0] = NOW_US+i*m.SLOT_US, 100.+2*i
        svc.tick(wall[0], publisher=True)
        svc.publisher_reply(PublisherReply(missed(svc, wall[0]//m.SLOT_US), (), False))
        audit(svc.state, NOW_US//m.SLOT_US, wall[0])
        assert svc.traces.retained_bytes <= 256*1024 and len(svc.traces.queue) <= 4
        if i in (699, 1999):
            record_property(f"state_bytes_{i+1}", len(pickle.dumps(svc.state)))
            record_property(f"lane_bytes_{i+1}", svc.traces.retained_bytes)
            lane_snapshot = {key: value for key, value in vars(svc.traces).items()
                             if key not in {"lock", "schema", "transport"}}
            lane_bytes = len(pickle.dumps(lane_snapshot))
            assert lane_bytes <= 256*1024+8192  # Four bounded keys plus fixed metadata.
            record_property(f"lane_serialized_bytes_{i+1}", lane_bytes)
    d = svc.state.days[0]
    assert d.lost_traces == 2000-len(entries(svc)) > 0
    assert d.first_lost_slot == NOW_US//m.SLOT_US+len(entries(svc))
    assert d.last_lost_slot == wall[0]//m.SLOT_US
    assert not d.final and svc.journal.losses == d.lost_traces


@exhaustive
def test_exhaustive_lifetime_trace_and_actual_start_grid():
    first = (NOW_US+m.DAY_US)//m.SLOT_US-2
    stamp = first*m.SLOT_US
    initial, _ = m.step(m.State(), m.Tick(stamp, 100.))
    # Repeated event words, including arbitrary replies and shutdown at every
    # position. External trace prefixes are part of the exploration key.
    frontier = {(initial, (), stamp, ())}
    for _ in range(5):
        following = set()
        for state, emitted, latest, starts in frontier:
            events = [m.Tick(stamp+i*m.SLOT_US, 100.+2*i) for i in range(4)]
            events += [m.ShutdownRequested(stamp+i*m.SLOT_US+500_000) for i in range(4)]
            if state.job:
                job = state.job
                events += [m.SlotOutcome(latest, job.slot, "missed", generation=job.generation),
                    m.SlotOutcome(latest, job.slot, "published", generation=job.generation,
                                  actual_start_mono=100.+2*(job.slot-first)),
                    m.SlotOutcome(latest, job.slot, "published", generation=job.generation,
                                  actual_start_mono=100.+2*(job.slot-first)+.2),
                    m.SlotOutcome(latest, job.slot+1, "published", generation=job.generation),
                    m.SlotOutcome(latest, job.slot, "published", generation=job.generation+1)]
            for event in events:
                after, effects = m.step(state, event)
                accepted = not any(isinstance(e, m.Fault) for e in effects)
                expected_latest = event.stamp if accepted and isinstance(event, (m.Tick, m.ShutdownRequested)) else latest
                audit(after, first, expected_latest)
                more = tuple(e.slot for e in effects if isinstance(e, m.EmitTrace))
                trace = emitted+more
                assert_order(trace, first)
                for e in effects:
                    if isinstance(e, m.StartPublisherCycle):
                        assert e.job.slot*m.SLOT_US//m.DAY_US == event.stamp//m.DAY_US
                        assert e.job.earliest_start_mono >= (starts[-1]+2 if starts else 100.)
                actual = starts
                if accepted and isinstance(event, m.SlotOutcome) and event.outcome != "missed":
                    actual += (event.actual_start_mono,)
                    assert all(b-a >= 2 for a, b in zip(actual, actual[1:], strict=False))
                following.add((after, trace, expected_latest, actual))
        frontier = following
    assert len(frontier) > 100


@exhaustive
def test_generated_lifetime_traces_and_state_bound():
    rng = random.Random(171607)  # noqa: S311
    for _ in range(40):
        state, first, latest, emitted = m.State(), None, None, []
        start = NOW_US+m.DAY_US-10*m.SLOT_US
        for i in range(40):
            stamp = start+i*m.SLOT_US
            if state.job and rng.randrange(2):
                event = m.SlotOutcome(stamp, state.job.slot, "missed", generation=state.job.generation)
            else:
                event = m.Tick(stamp, float(100+2*i))
            after, effects = m.step(state, event)
            if isinstance(event, m.Tick) and not any(isinstance(e, m.Fault) for e in effects):
                first, latest = first if first is not None else stamp//m.SLOT_US, stamp
            state = after
            audit(state, first, latest)
            emitted += [e.slot for e in effects if isinstance(e, m.EmitTrace)]
            assert_order(emitted, first)


def test_terminal_cap_blocks_even_with_event_permits():
    svc = service()
    day = "2026-10-12"
    svc.journal.grant(day, 100)
    svc.journal.terminal_permits[day] = 0
    assert svc.port.reserve(NOW_US, terminal=True) is None
    assert svc.port.lost.is_set() and svc.journal.permits[day] == 100
    assert svc.journal.days[day].next_seq == 1
    svc.closing_reason = "shutdown"
    svc.flush(svc.store.clock()+4)
    assert not svc.state.days[0].closed and svc.journal.days[day].losses
    counter = svc.counter_row(day, int(svc.store.wall()*1e6), closed=False).value()
    assert not counter["closed"] and counter["loss_count"] > 0
    assert counter["receipt_count"] == counter["settle_attempts"] == counter["refund_attempts"] == 0


@pytest.mark.parametrize("streamed", [None, False, True])
def test_nullable_dimensions_fold_without_raising(streamed):
    from trusted_router.async_settle_shadow_v2_evidence import COUNTS
    svc = service()
    bucket = dict(phase="settle", dimensions=dict(adapter="openai", route_type=None, streamed=streamed),
                  **dict.fromkeys(COUNTS, 0), eligible_demand=None)
    bucket.update(settle_attempts=1, C=1)
    assert svc.port.fold_counts("2026-10-12", bucket)
    assert svc.counters["2026-10-12"]["C"] == 1


def assert_order(trace, first):
    for day_number in {slot*m.SLOT_US//m.DAY_US for slot in trace}:
        slots = [slot for slot in trace if slot*m.SLOT_US//m.DAY_US == day_number]
        begin = max(first, day_number*m.DAY_US//m.SLOT_US)
        assert slots == list(range(begin, begin+len(slots)))


@pytest.mark.parametrize("fault", ["lost", "late"])
def test_original_final_reply_fault_matches_native(fault):
    from tests.shadow_observation_faults import verify_final_fault
    verify_final_fault(service(), fault)


def test_one_attempt_handoff_matches_native():
    from tests.shadow_observation_faults import verify_handoff_fault
    verify_handoff_fault(service())


@pytest.mark.parametrize("upgrade", [False, True])
def test_completion_backs_original_unknown_booking(upgrade):
    import copy

    from tests.test_async_settle_shadow_observation import fixture
    from trusted_router.async_settle_shadow_journal import SAMPLE_KIND, Row
    from trusted_router.async_settle_shadow_v2_evidence import EVENT
    svc = service()
    day = "2026-10-12"
    svc.journal.grant(day, 100)
    r = svc.port.reserve(NOW_US+100_000_000)
    event = copy.deepcopy(next(v["object"] for v in fixture("receipt_pages")
                              if v["schema"] == "TERMINAL_EVENT" and v["expected"] == "accept"))
    sample = copy.deepcopy(fixture("sample")[0]["object"])
    sample.update(original_terminal_receipt=dict(day=day, boot=r.boot, event_seq=f"{r.event_seq:020}"),
                  classification="unevaluable", booking={**sample["booking"], "outcome": "unknown"})
    event.update(seq=f"{r.event_seq:020}", event_at_us=r.observed_us, receipt_at_us=r.observed_us,
                 partition="C", eligibility="eligible", proof_kind="compared", comparison="unevaluable",
                 payload_sha256=sample["payload_hash"], original_receipt=None, certificate_sha256=None,
                 booking=dict(outcome="unknown", reference=None, winner="settle"),
                 prediction=copy.deepcopy(fixture("sample")[1]["object"]))
    row = Row.build(SAMPLE_KIND, sample["authorization_day"]+"/"+sample["authorization_id"], sample)
    assert svc.port.finalize(r, event)
    assert svc.port.complete(r, (row,))
    original = svc.journal.freeze(day)[0]
    later = svc.port.reserve(r.observed_us+1)
    completion = dict(EVENT.parse(svc.journal.slot(r).body), phase="completion", seq=f"{later.event_seq:020}",
        event_at_us=later.observed_us, receipt_at_us=later.observed_us, prediction=None,
        completion_of=event["seq"], retry_original=sample["original_terminal_receipt"],
        booking=dict(outcome="settled", reference="a"*64, winner="settle"))
    if upgrade:
        completion.update(partition="A", comparison="exact")
    assert svc.port.complete_event(later, original, completion)
    assert svc.port.complete(later)
    pages = svc.journal.freeze(day)
    if upgrade:
        with pytest.raises(ValueError, match="reference_conflict"):
            svc.journal.verify_complete(later, [*pages, row])
        assert not svc.journal.slot(r).reconciled
    else:
        assert svc.journal.verify_complete(later, [*pages, row])
        assert svc.journal.slot(r).reconciled


@pytest.mark.parametrize("workspaces", ["", "ws"])
@pytest.mark.xdist_group("p3-dormancy")
def test_policy_empty_outputs_identical_with_package_imports_blocked(workspaces, dormancy_runs):  # noqa: F811
    normal, blocked, mutant = (dormancy_runs[workspaces, mode] for mode in ("normal", "blocked", "mutant"))
    for result in (normal, blocked):
        assert result.returncode == 0, result.stderr
    assert normal.stdout.splitlines()[-1] == blocked.stdout.splitlines()[-1]
    assert mutant.returncode != 0 and b"AssertionError: empty-policy v2 import" in mutant.stderr


def test_dormancy_closed_module_names_match_source_inventory():
    from pathlib import Path

    from tests.shadow_observation_dormancy import NAMES
    root = Path("src/trusted_router")
    package = {p.stem for p in root.glob("async_settle_shadow_v2_*.py")}
    package |= {p.stem for pattern in ("*shadow_lifecycle.py", "*shadow_journal.py", "*shadow_health_state.py",
        "*shadow_attachments.py", "*shadow_observation.py", "*shadow_io.py", "services/*shadow_health.py")
        for p in root.glob(pattern)}
    assert NAMES == package


@pytest.mark.asyncio
async def test_malformed_async_reply_always_resolves_future(monkeypatch):
    from trusted_router.services import async_settle_shadow_health as shell
    svc = service()
    svc.loop = asyncio.get_running_loop()
    svc.store.wall = lambda: NOW_US/1e6
    svc.tick(NOW_US, publisher=True)
    before = svc.state
    monkeypatch.setattr(shell, "publish_cycle", lambda *args: PublisherReply(
        dict(missed(svc, NOW_US//m.SLOT_US), day="2026-10-13"), (), False))
    svc.submit_publisher(NOW_US//m.SLOT_US, NOW_US)
    await svc.publisher_job
    assert svc.publisher_job.done() and svc.publisher_faults == 1
    assert svc.state == before and svc.state.job is not None


@pytest.mark.asyncio
async def test_owner_dispatch_queued_across_later_opportunities():
    svc = service()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.loop = asyncio.get_running_loop()
    wall, mono = [NOW_US], [100.]
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: mono[0]
    effects = svc.tick(wall[0])
    job = next(e.job for e in effects if isinstance(e, m.StartPublisherCycle))
    # Suspend the owner between accepting Tick and dispatching its effect.
    # On resumption it first accepts one newer wake spanning seven opportunities.
    wall[0], mono[0] = NOW_US+7*m.SLOT_US, 114.
    svc.tick(wall[0])
    assert svc.state.days[0].pending_missed == m.MissedRun(job.slot+1, 7)
    assert entries(svc) == []
    svc.submit_publisher(job.slot, NOW_US)
    await svc.publisher_job
    assert [entry["slot"] for entry in entries(svc)] == list(range(job.slot, job.slot+8))
    assert all(entry["outcome"] == "missed" for entry in entries(svc))
    assert svc.publisher_faults == 0
