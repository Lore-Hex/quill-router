"""Owner transition proofs: no threads, clocks or sleeps in the model tests."""
from __future__ import annotations

import random
from dataclasses import replace
from functools import lru_cache

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_OPERATIONS,
    validate_publisher_final,
)

MIDNIGHT = 1_792_022_400_000_000  # A UTC boundary; only integer arithmetic below.
MIDNIGHT -= MIDNIGHT % m.DAY_US
OLD = m.day_name(MIDNIGHT-1)
NEW = m.day_name(MIDNIGHT)
LAST = MIDNIGHT//m.SLOT_US-1


@lru_cache(maxsize=8192)
def invariants(state):
    assert m.lifetime_coverage(state)
    assert m.conservation(state)
    assert m.final_barrier(state)
    assert m.one_final(state)
    assert m.bounded(state)
    assert m.no_post_final_owners(state)
    assert m.accounted_once(state)
    assert m.never_reopens(state)
    for d in state.days:
        p = dict.fromkeys(set(PUBLISHER_OPERATIONS.fields)-{"cycle_us", "rpc_us"}, 0)
        p.update(scheduled_slots=d.scheduled, missed_slots=d.missed, claim_attempts=d.attempted,
                 lost_claims=d.lost, publications=d.published, failures=d.failed,
                 claims=d.published, scans=d.published)
        p["cycle_us"] = dict(bounds_us=[100, 500, 1000, 2000, 5000, 10000, 50000, 200000], counts=[d.attempted]+[0]*8)
        p["rpc_us"] = dict(bounds_us=p["cycle_us"]["bounds_us"], counts=[0]*9)
        validate_publisher_final(p)


def checked(state, event):
    if isinstance(event, m.SlotOutcome) and event.outcome != "missed" and event.actual_start_mono is None and state.job:
        event = replace(event, actual_start_mono=state.job.earliest_start_mono)
    after, effects = m.step(state, event)
    invariants(after)
    if any(isinstance(e, m.Fault) for e in effects):
        current_reply = (isinstance(event, m.SlotOutcome) and state.job is not None
                         and (event.slot, event.generation) == (state.job.slot, state.job.generation))
        # Capacity refusal during a forward tick can still account the old day.
        if current_reply:
            assert after.job is None
            assert sum(d.failed for d in after.days) == sum(d.failed for d in state.days)+1
            assert m.EmitTrace(event.slot, "failed", unknown_outcome=True) in effects
        elif effects != (m.Fault("day_limit"),) and not any(isinstance(e, m.Fault) and e.reason == "day_limit" for e in effects):
            assert after == state
    for old in state.days:
        if old.final:
            new = next((d for d in after.days if d.day == old.day), None)
            if new is not None:
                assert new.final and not (new.publishers or new.observers or new.attachments or new.traces)
                assert (new.scheduled, new.missed, new.attempted) == (old.scheduled, old.missed, old.attempted)
    return after, effects


def boot():
    state, _ = checked(m.State(), m.Tick(MIDNIGHT-4_000_000))
    state, _ = checked(state, m.SlotOutcome(MIDNIGHT-4_000_000, LAST-1, "published", b"trace"))
    state, _ = checked(state, m.FlushReply(MIDNIGHT-3_000_000, OLD, True, False, (LAST-1,)))
    return state


def drain(state, outcome="timely"):
    # Complete only work actually issued by the machine. Early injected replies
    # above were refused, so the real effect still requires its own completion.
    if not state.shutdown:
        state, _ = checked(state, m.ShutdownRequested(max(MIDNIGHT+1, state.accepted_stamp or 0)))
    for _ in range(20):
        before = state
        for d in state.days:
            if d.closed or d.veto:
                continue
            if d.final:
                if d.final_reply is None:
                    state, _ = checked(state, m.FinalReply(MIDNIGHT+2, d.day, outcome))
                else:
                    state, _ = checked(state, m.CloseResultReply(MIDNIGHT+3, d.day, True))
                continue
            for slot in d.publishers:
                state, _ = checked(state, m.SlotOutcome(MIDNIGHT+2, slot, "missed"))
            for key in d.observers:
                state, _ = checked(state, m.ObserverFolded(MIDNIGHT+2, d.day, key))
            for key in d.attachments:
                state, _ = checked(state, m.AttachmentSettled(MIDNIGHT+2, d.day, key))
            current = next(v for v in state.days if v.day == d.day)
            if current.flush:
                state, _ = checked(state, m.FlushReply(MIDNIGHT+2, d.day, True, True, current.traces))
        if state == before:
            break
    assert all(d.closed and d.result or d.veto for d in state.days)
    return state


def test_midnight_flush_precedes_unaccounted_missed_slots():
    state = boot()
    state, effects = checked(state, m.Tick(MIDNIGHT+500_000))
    old = next(d for d in state.days if d.day == OLD)
    assert old.cursor == LAST and old.missed == 1 and old.scheduled == 2
    assert tuple(old.traces) == (LAST,) and not old.final
    assert not any(isinstance(e, m.StartFinalClose) for e in effects)
    state = drain(state)
    old = next(d for d in state.days if d.day == OLD)
    assert old.closed and old.missed == 1 and old.published == 1


def test_scheduled_cycle_can_enter_frozen_final_close():
    state = boot()
    state, _ = checked(state, m.Tick(MIDNIGHT-2_000_000))
    state, _ = checked(state, m.Tick(MIDNIGHT+500_000))
    assert not state.days[0].final and state.days[0].publishers == {LAST}
    state, _ = checked(state, m.SlotOutcome(MIDNIGHT-1, LAST, "lost_claim", b"old trace"))
    assert state.days[0].lost == 1 and state.days[1].lost == 0
    state = drain(state)
    before = state
    for event in (m.Tick(MIDNIGHT-1), m.SlotOutcome(MIDNIGHT-1, LAST, "published"),
                  m.ObserverEnqueued(MIDNIGHT-1, "late"), m.AttachmentAdmitted(MIDNIGHT-1, "late")):
        state, effects = checked(state, event)
        assert state == before and isinstance(effects[0], m.Fault)
    # A refused old event does not poison the owner or allow a second final.
    assert state.days[0].closed


@pytest.mark.parametrize("outcome", ["timely", "error", "unknown", "late"])
def test_final_reply_branches_and_permanent_veto(outcome):
    state = drain(boot(), outcome)
    old = state.days[0]
    assert old.closed == (outcome == "timely")
    assert old.veto == (outcome != "timely")
    for event in (m.FinalReply(MIDNIGHT+8, OLD, "timely"), m.CloseResultReply(MIDNIGHT+9, OLD, True)):
        after, effects = checked(state, event)
        assert after == state and isinstance(effects[0], m.Fault)


@pytest.mark.parametrize("outcome", ["missed", "lost_claim", "published", "failed"])
def test_slot_outcome_branches(outcome):
    state, _ = checked(m.State(), m.Tick(MIDNIGHT-2_000_000))
    state, _ = checked(state, m.SlotOutcome(MIDNIGHT+1, LAST, outcome))
    assert state.days[0].scheduled == 1
    assert state.days[0].missed == (outcome == "missed")
    assert state.days[0].attempted == (outcome != "missed")


def test_owner_and_reply_branches():
    state = boot()
    for event in (m.ObserverEnqueued(MIDNIGHT-1, "read"), m.AttachmentAdmitted(MIDNIGHT-1, "object")):
        state, _ = checked(state, event)
        unchanged, effects = checked(state, event)
        assert unchanged == state and isinstance(effects[0], m.Fault)
    state, _ = checked(state, m.Tick(MIDNIGHT+500_000))
    for event in (m.ObserverFolded(MIDNIGHT+1, OLD, "read"), m.AttachmentSettled(MIDNIGHT+1, OLD, "object")):
        state, _ = checked(state, event)
    state, _ = checked(state, m.FlushReply(MIDNIGHT+1, OLD, False))
    state, _ = checked(state, m.Tick(MIDNIGHT+600_000))
    state = drain(state)
    assert state.days[0].closed


@pytest.mark.parametrize("event", [m.SlotOutcome(MIDNIGHT, LAST+999, "failed"),
    m.ObserverFolded(MIDNIGHT, OLD, "missing"), m.AttachmentSettled(MIDNIGHT, OLD, "missing"),
    m.FinalReply(MIDNIGHT, OLD, "timely"), m.CloseResultReply(MIDNIGHT, OLD, True),
    m.FlushReply(MIDNIGHT, "1970-01-01", True), m.Tick(10**100)])
def test_refused_events_are_total(event):
    state = boot()
    after, effects = checked(state, event)
    assert after == state and isinstance(effects[0], m.Fault)


def test_close_result_error_veto_and_day_bound():
    state = boot()
    state, _ = checked(state, m.ShutdownRequested(MIDNIGHT))
    d = state.days[0]
    state, _ = checked(state, m.FlushReply(MIDNIGHT, OLD, True, True, d.traces))
    state, _ = checked(state, m.FinalReply(MIDNIGHT, OLD, "timely"))
    state, _ = checked(state, m.CloseResultReply(MIDNIGHT, OLD, False))
    assert state.days[0].veto
    state = m.State(publisher_enabled=False)
    for offset in range(5):
        state, _ = checked(state, m.Tick(MIDNIGHT+offset*m.DAY_US))
    assert len(state.days) == 3


@exhaustive
@pytest.mark.parametrize("outcome", ["timely", "error", "unknown", "late"])
def test_exhaustive_boundary_permutations(outcome, record_property):
    initial = boot()
    initial, _ = checked(initial, m.ObserverEnqueued(MIDNIGHT-1, "read"))
    initial, _ = checked(initial, m.AttachmentAdmitted(MIDNIGHT-1, "object"))
    events = (m.Tick(MIDNIGHT-2_000_000), m.Tick(MIDNIGHT+500_000),
        m.SlotOutcome(MIDNIGHT+1, LAST, "published", b"trace"),
        m.SlotOutcome(MIDNIGHT-1, LAST, "published", b"trace"),
        m.ObserverFolded(MIDNIGHT+1, OLD, "read"), m.AttachmentSettled(MIDNIGHT+1, OLD, "object"),
        m.FlushReply(MIDNIGHT+1, OLD, True, True, (LAST,)), m.FinalReply(MIDNIGHT+1, OLD, outcome),
        m.CloseResultReply(MIDNIGHT+1, OLD, True), m.ShutdownRequested(MIDNIGHT+1))
    # Exact prefix-state memoization explores all 10! orderings without rerunning
    # identical suffixes. No commuting event is dropped and every edge is checked.
    seen = set()
    terminal = set()
    stack = [(initial, (1 << len(events))-1)]
    while stack:
        state, remaining = stack.pop()
        key = state, remaining
        if key in seen:
            continue
        seen.add(key)
        if not remaining:
            terminal.add(drain(state, outcome).days[0])
            continue
        for index, event in enumerate(events):
            bit = 1 << index
            if remaining & bit:
                after, _ = checked(state, event)
                stack.append((after, remaining ^ bit))
    record_property("distinct_prefix_states", len(seen))
    record_property("represented_orderings", 3_628_800)
    record_property("terminal_states", len(terminal))
    assert terminal and all(d.closed and d.result or d.veto for d in terminal)
    assert all(d.closed == (outcome == "timely") for d in terminal)


@exhaustive
def test_replay_determinism_for_event_sequences():
    rng = random.Random(1716)  # noqa: S311 - deterministic event generation
    for _ in range(200):
        events = []
        for _ in range(30):
            stamp = MIDNIGHT+rng.randrange(-6, 7)*1_000_000
            events.append(rng.choice([m.Tick(stamp), m.ShutdownRequested(stamp),
                m.ObserverEnqueued(stamp, "read"), m.ObserverFolded(stamp, OLD, "read"),
                m.SlotOutcome(stamp, LAST, rng.choice(["missed", "published", "failed", "lost_claim"])),
                m.FinalReply(stamp, OLD, rng.choice(["timely", "error", "late", "unknown"])),
                m.CloseResultReply(stamp, OLD, True), m.FlushReply(stamp, OLD, True, True)]))
        states = []
        for _ in range(2):
            state = m.State()
            for event in events:
                state, _ = checked(state, event)
            states.append(state)
        assert states[0] == states[1]


def test_final_requires_cursor_at_end_even_with_conserved_empty_counters():
    state = boot()
    day = replace(state.days[0], ready=True, flush=True)
    state = replace(state, days=(day,), current=NEW)
    state, effects = checked(state, m.FlushReply(MIDNIGHT, OLD, True, True))
    assert not state.days[0].final
    assert not any(isinstance(e, m.StartFinalClose) for e in effects)


def test_no_admission_while_final_reply_is_outstanding():
    state = boot()
    state, _ = checked(state, m.ShutdownRequested(MIDNIGHT))
    d = state.days[0]
    state, effects = checked(state, m.FlushReply(MIDNIGHT, OLD, True, True, d.traces))
    assert state.days[0].final and state.days[0].final_reply is None
    assert any(isinstance(e, m.StartFinalClose) for e in effects)
    for event in (m.ObserverEnqueued(MIDNIGHT-1, "late"), m.AttachmentAdmitted(MIDNIGHT-1, "late"),
                  m.SlotOutcome(MIDNIGHT-1, LAST, "published"), m.Tick(MIDNIGHT-1)):
        after, effects = checked(state, event)
        assert after == state and isinstance(effects[0], m.Fault)


def test_tick_accounts_new_day_gap_and_intervening_day():
    state = boot()
    state, _ = checked(state, m.Tick(MIDNIGHT+m.DAY_US+4_500_000))
    assert len(state.days) == 3
    assert state.days[0].missed == 1
    assert state.days[1].missed == m.DAY_US//m.SLOT_US
    assert state.days[2].missed == 3


@pytest.mark.parametrize("attachment", [False, True])
def test_completed_owner_event_cannot_be_replayed(attachment):
    state = boot()
    admit = m.AttachmentAdmitted if attachment else m.ObserverEnqueued
    settle = m.AttachmentSettled if attachment else m.ObserverFolded
    admitted = admit(MIDNIGHT-2, "item")
    folded = settle(MIDNIGHT-2, OLD, "item")
    state, _ = checked(state, admitted)
    state, _ = checked(state, folded)
    for event in (admitted, folded):
        after, effects = checked(state, event)
        assert after == state and isinstance(effects[0], m.Fault)
    state, _ = checked(state, admit(MIDNIGHT-1, "item"))
    after, effects = checked(state, folded)
    assert after == state and isinstance(effects[0], m.Fault)
    state, _ = checked(state, settle(MIDNIGHT, OLD, "item"))
    assert not state.days[0].observers and not state.days[0].attachments


def test_vetoed_day_cannot_be_evicted_to_admit_unbounded_open_journals():
    state, _ = checked(boot(), m.Tick(MIDNIGHT+500_000))
    state, _ = checked(state, m.FlushReply(MIDNIGHT+500_000, OLD, True, True, state.days[0].traces))
    state, _ = checked(state, m.FinalReply(MIDNIGHT+500_000, OLD, "unknown"))
    # Continue a genuinely live lifetime after the old-day final veto.
    for offset in range(4):
        state, _ = checked(state, m.Tick(MIDNIGHT+offset*m.DAY_US+500_000))
    assert len(state.days) == 3 and state.days[0].veto
    assert state.retired == ""


def test_unknown_close_result_allows_only_readback_not_another_write():
    state = boot()
    state, _ = checked(state, m.ShutdownRequested(MIDNIGHT))
    state, _ = checked(state, m.FlushReply(MIDNIGHT, OLD, True, True, state.days[0].traces))
    state, effects = checked(state, m.FinalReply(MIDNIGHT, OLD, "timely"))
    assert effects == (m.StartCloseResult(OLD),)
    state, effects = checked(state, m.CloseResultReply(MIDNIGHT+1, OLD, None))
    assert state.days[0].result_reply == "unknown" and effects == ()
    state, effects = checked(state, m.CloseResultReply(MIDNIGHT+2, OLD, True))
    assert state.days[0].closed and effects == ()


def test_attachment_failure_keeps_owner_until_durable_reply():
    state = boot()
    state, _ = checked(state, m.AttachmentAdmitted(MIDNIGHT-1, "object"))
    before = state
    state, effects = checked(state, m.AttachmentSettled(MIDNIGHT, OLD, "object", success=False))
    assert state == before and effects == (m.UploadAttachment("object"),)
    state, _ = checked(state, m.AttachmentSettled(MIDNIGHT+1, OLD, "object"))
    assert not state.days[0].attachments
