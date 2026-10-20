"""Immutable observation lifecycle. The caller supplies time and executes effects.

A scheduled owner is an outstanding opportunity, not a completed counter delta.
Conservation holds for the accounted prefix even while that owner is outstanding.
Reply stamps describe acceptance; explicit slot/day identities retain the origin.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Literal, TypeAlias

from trusted_router.async_settle_shadow_v2_evidence import MAX_DAY_BUCKETS

SLOT_US = 2_000_000
DAY_US = 86_400_000_000


@dataclass(frozen=True)
class MissedRun:
    first_slot: int
    count: int
    reason: str = "publisher_busy"


@dataclass(frozen=True)
class Day:
    day: str
    first_slot: int
    cursor: int
    last_slot: int
    interval_last_slot: int | None = None
    scheduled: int = 0
    missed: int = 0
    attempted: int = 0
    lost: int = 0
    published: int = 0
    failed: int = 0
    publishers: frozenset[int] = frozenset()
    observers: frozenset[str] = frozenset()
    observer_stamp: int = -1
    observer_keys: frozenset[str] = frozenset()
    attachments: frozenset[str] = frozenset()
    attachment_stamp: int = -1
    attachment_keys: frozenset[str] = frozenset()
    # One contiguous emitted prefix; the attachment lane alone retains bodies.
    traces: range = range(0)
    frontier: int = -1
    lost_traces: int = 0
    first_lost_slot: int | None = None
    last_lost_slot: int | None = None
    flush: bool = False
    ready: bool = False
    final: bool = False
    final_reply: str | None = None
    result: bool = False
    result_reply: str | None = None
    closed: bool = False
    veto: bool = False

    @property
    def pending_missed(self) -> MissedRun | None:
        if not self.publishers:
            return None
        first = min(self.publishers)+1
        end = self.cursor if self.interval_last_slot is None else self.interval_last_slot
        return MissedRun(first, end-first+1) if end >= first else None


@dataclass(frozen=True)
class State:
    days: tuple[Day, ...] = ()
    current: str = ""
    retired: str = ""
    shutdown: bool = False
    publisher_enabled: bool = True
    first_slot: int | None = None
    accepted_stamp: int | None = None
    shutdown_stamp: int | None = None
    generation: int = 0
    job: PublisherJob | None = None
    last_actual_start: float = float("-inf")
    retired_slots: int = 0


@dataclass(frozen=True)
class Tick:
    stamp: int
    mono: float | None = None
    admit: bool = True


@dataclass(frozen=True)
class SlotOutcome:
    stamp: int
    slot: int
    outcome: Literal["missed", "lost_claim", "published", "failed"]
    trace: bytes = b""  # Input compatibility only: never retained in State.
    generation: int = 0
    actual_start_mono: float | None = None


@dataclass(frozen=True)
class PublisherFailed:
    stamp: int
    slot: int
    generation: int
    reason: str


@dataclass(frozen=True)
class TraceLost:
    stamp: int
    day: str
    slot: int


@dataclass(frozen=True)
class PolicyChanged:
    stamp: int
    generation: int


@dataclass(frozen=True)
class ObserverEnqueued:
    stamp: int
    key: str
    day: str | None = None


@dataclass(frozen=True)
class ObserverFolded:
    stamp: int
    day: str
    key: str
    success: bool = True


@dataclass(frozen=True)
class AttachmentAdmitted:
    stamp: int
    key: str
    day: str | None = None


@dataclass(frozen=True)
class AttachmentSettled:
    stamp: int
    day: str
    key: str
    drained: tuple[int, ...] | range = ()
    success: bool = True


@dataclass(frozen=True)
class FlushReply:
    stamp: int
    day: str
    success: bool
    ready: bool = False
    drained: tuple[int, ...] | range = ()


@dataclass(frozen=True)
class FinalReply:
    stamp: int
    day: str
    outcome: Literal["timely", "error", "unknown", "late"]


@dataclass(frozen=True)
class CloseResultReply:
    stamp: int
    day: str
    success: bool | None


@dataclass(frozen=True)
class ShutdownRequested:
    stamp: int


Event: TypeAlias = (Tick | TraceLost | PolicyChanged | SlotOutcome | PublisherFailed | ObserverEnqueued | ObserverFolded | AttachmentAdmitted
                    | AttachmentSettled | FlushReply | FinalReply | CloseResultReply | ShutdownRequested)


@dataclass(frozen=True)
class PublisherJob:
    slot: int
    generation: int
    earliest_start_mono: float
    latest_start_mono: float


@dataclass(frozen=True)
class StartPublisherCycle:
    job: PublisherJob

    @property
    def slot(self) -> int:
        return self.job.slot


@dataclass(frozen=True)
class EmitTrace:
    slot: int
    outcome: str
    unknown_outcome: bool = False


@dataclass(frozen=True)
class StartFlush:
    day: str


@dataclass(frozen=True)
class StartFinalClose:
    day: str


@dataclass(frozen=True)
class StartCloseResult:
    day: str


@dataclass(frozen=True)
class UploadAttachment:
    key: str


@dataclass(frozen=True)
class Fault:
    reason: str


Effect: TypeAlias = EmitTrace | StartPublisherCycle | StartFlush | StartFinalClose | StartCloseResult | UploadAttachment | Fault


def day_name(stamp: int) -> str:
    return (dt.date(1970, 1, 1) + dt.timedelta(days=stamp // DAY_US)).isoformat()


def conservation(state: State) -> bool:
    return all(d.scheduled == d.missed + d.attempted and
               d.attempted == d.lost + d.published + d.failed for d in state.days)


def final_barrier(state: State) -> bool:
    return all(not d.final or (d.cursor == d.last_slot and not
        (d.publishers or d.observers or d.attachments or d.traces) and d.ready)
        for d in state.days)


def one_final(state: State) -> bool:
    return all((not d.result or d.final_reply == "timely") and
        (d.final_reply not in ("error", "unknown", "late") or d.veto) and
        (not d.closed or (d.result and not d.veto)) for d in state.days)


def bounded(state: State) -> bool:
    return len(state.days) <= MAX_DAY_BUCKETS and len({d.day for d in state.days}) == len(state.days)


def no_post_final_owners(state: State) -> bool:
    return all(not d.final or not (d.publishers or d.observers or d.attachments or d.traces or d.lost_traces)
               for d in state.days)


def accounted_once(state: State) -> bool:
    for d in state.days:
        end = d.cursor if d.interval_last_slot is None else d.interval_last_slot
        opportunities = max(0, end-d.first_slot+1) if state.publisher_enabled else 0
        if d.scheduled+len(d.publishers) != opportunities:
            return False
        if len(set(d.traces)) != len(d.traces) or set(d.traces) & d.publishers:
            return False
    return True


def never_reopens(state: State) -> bool:
    return all(d.day > state.retired and (not d.closed or d.final) for d in state.days)


def _put(state: State, day: Day) -> State:
    return replace(state, days=tuple(sorted(
        (*(d for d in state.days if d.day != day.day), day), key=lambda d: d.day)))


def _progress(state: State, effects: list[Effect]) -> tuple[State, tuple[Effect, ...]]:
    for d in state.days:
        if d.final or d.closed or d.veto:
            continue
        closing = state.shutdown or d.day < state.current
        if (closing and d.cursor == d.last_slot and not
                (d.publishers or d.observers or d.attachments or d.traces or d.lost_traces or d.flush) and d.ready):
            d = replace(d, final=True)
            effects.append(StartFinalClose(d.day))
        elif not d.flush:
            d = replace(d, flush=True)
            effects.append(StartFlush(d.day))
        state = _put(state, d)
    return state, tuple(effects)


def _emit(d: Day, effects: list[Effect], reply: SlotOutcome | None = None) -> Day:
    end = d.cursor if d.interval_last_slot is None else d.interval_last_slot
    frontier = min(d.publishers) if d.publishers else end+1
    previous = d.first_slot if d.frontier < 0 else d.frontier
    for slot in range(previous, frontier):
        effects.append(EmitTrace(slot, reply.outcome if reply is not None and slot == reply.slot else "missed"))
    start = d.traces.start if d.traces else previous
    return replace(d, frontier=frontier, traces=range(start, frontier))


def lifetime_coverage(state: State) -> bool:
    if state.first_slot is None or state.accepted_stamp is None:
        return not state.days
    through = state.first_slot + state.retired_slots
    for d in state.days:
        end = d.cursor if d.interval_last_slot is None else d.interval_last_slot
        if d.first_slot != through:
            return False
        through = end+1
    return through == state.accepted_stamp//SLOT_US+1


def _release_failed(state: State, slot: int, generation: int, reason: str) -> tuple[State, tuple[Effect, ...]]:
    """A fault completes only the matching outstanding job, exactly once."""
    job = state.job
    fault = Fault(reason)
    if (job is None or type(slot) is not int or type(generation) is not int
            or (slot, generation) != (job.slot, job.generation)):
        return state, (fault,)
    identity = day_name(slot*SLOT_US)
    d = next((d for d in state.days if d.day == identity), None)
    if d is None:
        # Normally an outstanding owner prevents day retirement. Retain its
        # lifetime interval even if a caller has lost that day.
        first = max(slot//(DAY_US//SLOT_US)*(DAY_US//SLOT_US),
                    (state.first_slot if state.first_slot is not None else slot)+state.retired_slots)
        last = (slot//(DAY_US//SLOT_US)+1)*(DAY_US//SLOT_US)-1
        end = min(last, (state.accepted_stamp or slot*SLOT_US)//SLOT_US)
        missed = end-first
        d = Day(identity, first, end, last, scheduled=missed, missed=missed,
                publishers=frozenset({slot}))
    effects: list[Effect] = [fault]
    d = replace(d, publishers=d.publishers-{slot}, scheduled=d.scheduled+1,
                attempted=d.attempted+1, failed=d.failed+1, ready=False)
    d = _emit(d, effects, SlotOutcome(slot*SLOT_US, slot, "failed"))
    effects = [replace(e, unknown_outcome=True) if isinstance(e, EmitTrace) and e.slot == slot else e
               for e in effects]
    # The actual start is unavailable; keep the latest permitted start as the
    # conservative cadence bound for the following cycle.
    state = replace(state, job=None, last_actual_start=max(state.last_actual_start, job.latest_start_mono))
    return _progress(_put(state, d), effects)


def _tick(state: State, stamp: int, *, shutdown: bool = False,
          mono: float | None = None, admit: bool = True) -> tuple[State, list[Effect]]:
    today, slot = day_name(stamp), stamp // SLOT_US
    effects: list[Effect] = []
    if state.accepted_stamp is not None and stamp < state.accepted_stamp:
        return state, [Fault("stale_tick")]
    original = state
    if not state.days:
        state = _put(state, Day(today, slot, slot-1, (stamp//DAY_US+1)*DAY_US//SLOT_US-1))
    # Enumerate the lifetime before retiring ANY shutdown suffix. Capacity is an
    # atomic refusal: a stamp beyond representable days is never accepted.
    while state.days[-1].day < today:
        first = state.days[-1].last_slot+1
        if len(state.days) >= MAX_DAY_BUCKETS:
            old = state.days[0]
            if not old.closed:
                return original, [Fault("day_limit")]
            state = replace(state, days=state.days[1:], retired=old.day,
                            retired_slots=state.retired_slots+old.last_slot-old.first_slot+1)
        state = _put(state, Day(day_name(first*SLOT_US), first, first-1, first+DAY_US//SLOT_US-1))
    state = replace(state, current=max(state.current, today), accepted_stamp=stamp,
                    first_slot=state.first_slot if state.first_slot is not None else state.days[0].first_slot)
    at = stamp/1_000_000 if mono is None else mono
    for d in state.days:
        if d.final or d.closed or d.day > today:
            continue
        through = min(slot, d.last_slot)
        if through > d.cursor:
            count = through-d.cursor if state.publisher_enabled else 0
            start = bool(count and admit and not d.veto and d.day == today and not shutdown and stamp % SLOT_US <= 250_000
                         and state.job is None)
            missed = count-int(start)
            owners = d.publishers | {slot} if start else d.publishers
            d = replace(d, cursor=through, scheduled=d.scheduled+missed, missed=d.missed+missed,
                        publishers=frozenset(owners), ready=False)
            if start:
                job = PublisherJob(slot, state.generation, max(at, state.last_actual_start+2),
                                   at+(250_000-stamp % SLOT_US)/1_000_000)
                state = replace(state, job=job)
                effects.append(StartPublisherCycle(job))
            if state.publisher_enabled:
                d = _emit(d, effects)
        if shutdown:
            d = replace(d, interval_last_slot=d.cursor, cursor=d.last_slot)
        state = _put(state, d)
    return replace(state, shutdown=state.shutdown or shutdown,
                   shutdown_stamp=stamp if shutdown else state.shutdown_stamp,
                   generation=state.generation+int(shutdown)), effects


def step(state: State, event: Event) -> tuple[State, tuple[Effect, ...]]:
    """Total over the closed event algebra, including invalid/stale replies.

    Rejected current publisher replies release their owner as failed. Other
    rejected replies leave State unchanged and cannot replay a completed owner.
    """
    try:
        return _step(state, event)
    except (ValueError, TypeError, OverflowError, AttributeError):
        if isinstance(event, SlotOutcome):
            return _release_failed(state, event.slot, event.generation, "invalid_event")
        return state, (Fault("invalid_event"),)


def _step(state: State, event: Event) -> tuple[State, tuple[Effect, ...]]:
    if isinstance(event, PublisherFailed):
        return _release_failed(state, event.slot, event.generation, event.reason)
    stamp_day = day_name(event.stamp)
    if isinstance(event, PolicyChanged):
        return replace(state, generation=max(state.generation, event.generation)), ()
    if isinstance(event, (Tick, ShutdownRequested)):
        existing = next((d for d in state.days if d.day == stamp_day), None)
        if stamp_day < state.current and existing is None:
            return state, (Fault("gone_day"),)
        if stamp_day <= state.retired or (existing is not None and (existing.final or existing.veto)):
            return state, (Fault("frozen_day"),)
        if state.shutdown:
            return state, (Fault("shutdown"),)
        state, effects = _tick(state, event.stamp, shutdown=isinstance(event, ShutdownRequested),
                               mono=event.mono if isinstance(event, Tick) else None,
                               admit=event.admit if isinstance(event, Tick) else False)
        return _progress(state, effects)
    identity = (day_name(event.slot*SLOT_US) if isinstance(event, SlotOutcome)
                else (event.day or stamp_day) if isinstance(event, AttachmentAdmitted)
                else (event.day or stamp_day) if isinstance(event, ObserverEnqueued) else event.day)
    d = next((d for d in state.days if d.day == identity), None)
    if d is None:
        if isinstance(event, SlotOutcome):
            return _release_failed(state, event.slot, event.generation, "gone_day")
        return state, (Fault("gone_day"),)
    if d.closed or d.veto:
        if isinstance(event, SlotOutcome):
            return _release_failed(state, event.slot, event.generation, "frozen_day")
        return state, (Fault("frozen_day"),)
    effects = []
    if isinstance(event, FinalReply):
        if not d.final or d.final_reply is not None or event.outcome not in ("timely", "error", "unknown", "late"):
            return state, (Fault("unexpected_final_reply"),)
        d = replace(d, final_reply=event.outcome, veto=event.outcome != "timely", result=event.outcome == "timely")
        if d.result:
            effects.append(StartCloseResult(d.day))
    elif isinstance(event, CloseResultReply):
        if not d.result or d.final_reply != "timely":
            return state, (Fault("unexpected_result_reply"),)
        if event.success is None:
            return _put(state, replace(d, result_reply="unknown")), ()
        d = replace(d, closed=event.success, veto=not event.success,
                    result_reply="acknowledged" if event.success else "error")
    elif d.final:
        if isinstance(event, SlotOutcome):
            return _release_failed(state, event.slot, event.generation, "frozen_day")
        return state, (Fault("frozen_day"),)
    elif isinstance(event, SlotOutcome):
        if (type(event.slot) is not int or type(event.generation) is not int
                or state.job is None or event.slot != state.job.slot or event.generation != state.job.generation
                or event.slot not in d.publishers or event.outcome not in ("missed", "lost_claim", "published", "failed")):
            return _release_failed(state, event.slot, event.generation, "unknown_slot")
        if event.outcome != "missed":
            if event.actual_start_mono is None or not state.job.earliest_start_mono <= event.actual_start_mono <= state.job.latest_start_mono:
                return _release_failed(state, event.slot, event.generation, "invalid_start")
            state = replace(state, last_actual_start=event.actual_start_mono)
        state = replace(state, job=None)
        d = replace(d, publishers=d.publishers-{event.slot}, scheduled=d.scheduled+1,
                    missed=d.missed+(event.outcome == "missed"), attempted=d.attempted+(event.outcome != "missed"),
                    lost=d.lost+(event.outcome == "lost_claim"), published=d.published+(event.outcome == "published"),
                    failed=d.failed+(event.outcome == "failed"), ready=False)
        d = _emit(d, effects, event)
    elif isinstance(event, TraceLost):
        if event.slot not in d.traces or (d.last_lost_slot is not None and event.slot <= d.last_lost_slot):
            return state, (Fault("unknown_trace_loss"),)
        d = replace(d, lost_traces=d.lost_traces+1,
                    first_lost_slot=d.first_lost_slot if d.first_lost_slot is not None else event.slot,
                    last_lost_slot=event.slot)
    elif isinstance(event, ObserverEnqueued):
        if (len(event.key) > 512 or len(d.observers | d.observer_keys) >= 32
                or event.key in d.observers or state.shutdown or event.stamp < d.observer_stamp
                or (event.stamp == d.observer_stamp and event.key in d.observer_keys)):
            return state, (Fault("observer_admission"),)
        d = replace(d, observers=d.observers | {event.key}, ready=False, observer_stamp=event.stamp,
                    observer_keys=(d.observer_keys if event.stamp == d.observer_stamp else frozenset()) | {event.key})
    elif isinstance(event, ObserverFolded):
        if type(event.success) is not bool or event.key not in d.observers or event.stamp < d.observer_stamp:
            return state, (Fault("unknown_observer"),)
        d = replace(d, observers=d.observers-{event.key}, veto=d.veto or not event.success)
    elif isinstance(event, AttachmentAdmitted):
        if (len(event.key) > 512 or len(d.attachments | d.attachment_keys) >= 8
                or event.key in d.attachments or event.stamp < d.attachment_stamp
                or (event.stamp == d.attachment_stamp and event.key in d.attachment_keys)):
            return state, (Fault("attachment_admission"),)
        d = replace(d, attachments=d.attachments | {event.key}, ready=False, attachment_stamp=event.stamp,
                    attachment_keys=(d.attachment_keys if event.stamp == d.attachment_stamp else frozenset()) | {event.key})
        effects.append(UploadAttachment(event.key))
    elif isinstance(event, AttachmentSettled):
        if (event.key not in d.attachments or event.stamp < d.attachment_stamp
                or not set(event.drained) <= set(d.traces)):
            return state, (Fault("unknown_attachment"),)
        if not event.success:
            return state, (UploadAttachment(event.key),)
        d = replace(d, attachments=d.attachments-{event.key}, traces=_drain(d.traces, event.drained))
    elif isinstance(event, FlushReply):
        if not d.flush or not set(event.drained) <= set(d.traces):
            return state, (Fault("unexpected_flush_reply"),)
        d = replace(d, flush=False, ready=event.success and event.ready,
                    traces=_drain(d.traces, event.drained) if event.success else d.traces)
        # A failed checkpoint is retried on a later tick, never recursively.
        state = _put(state, d)
        if not event.success or not event.ready:
            return state, ()
    else:
        return state, (Fault("invalid_event"),)
    return _progress(_put(state, d), effects)


def _drain(slots: range, drained: tuple[int, ...] | range) -> range:
    if not drained:
        return slots
    if tuple(drained) != tuple(range(slots.start, slots.start+len(drained))):
        raise ValueError("nonprefix_drain")
    return range(slots.start+len(drained), slots.stop)
