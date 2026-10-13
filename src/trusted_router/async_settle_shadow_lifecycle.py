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
    traces: tuple[int, ...] = ()
    trace_payloads: tuple[tuple[int, bytes], ...] = ()
    flush: bool = False
    ready: bool = False
    final: bool = False
    final_reply: str | None = None
    result: bool = False
    result_reply: str | None = None
    closed: bool = False
    veto: bool = False


@dataclass(frozen=True)
class State:
    days: tuple[Day, ...] = ()
    current: str = ""
    retired: str = ""
    shutdown: bool = False
    publisher_enabled: bool = True


@dataclass(frozen=True)
class Tick:
    stamp: int


@dataclass(frozen=True)
class SlotOutcome:
    stamp: int
    slot: int
    outcome: Literal["missed", "lost_claim", "published", "failed"]
    trace: bytes = b""


@dataclass(frozen=True)
class ObserverEnqueued:
    stamp: int
    key: str


@dataclass(frozen=True)
class ObserverFolded:
    stamp: int
    day: str
    key: str


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
    drained: tuple[int, ...] = ()
    success: bool = True


@dataclass(frozen=True)
class FlushReply:
    stamp: int
    day: str
    success: bool
    ready: bool = False
    drained: tuple[int, ...] = ()


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


Event: TypeAlias = (Tick | SlotOutcome | ObserverEnqueued | ObserverFolded | AttachmentAdmitted
                    | AttachmentSettled | FlushReply | FinalReply | CloseResultReply | ShutdownRequested)


@dataclass(frozen=True)
class StartPublisherCycle:
    slot: int


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


Effect: TypeAlias = StartPublisherCycle | StartFlush | StartFinalClose | StartCloseResult | UploadAttachment | Fault


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
    return all(not d.final or not (d.publishers or d.observers or d.attachments or d.traces or d.trace_payloads)
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
                (d.publishers or d.observers or d.attachments or d.traces or d.flush) and d.ready):
            d = replace(d, final=True)
            effects.append(StartFinalClose(d.day))
        elif not d.flush:
            d = replace(d, flush=True)
            effects.append(StartFlush(d.day))
        state = _put(state, d)
    return state, tuple(effects)


def _tick(state: State, stamp: int, *, shutdown: bool = False) -> tuple[State, list[Effect]]:
    today, slot = day_name(stamp), stamp // SLOT_US
    effects: list[Effect] = []
    if not state.days:
        first = slot
        d = Day(today, first, first-1, (stamp//DAY_US+1)*DAY_US//SLOT_US-1)
        state = _put(state, d)
    if today > state.current:
        state = replace(state, current=today)
    for d in state.days:
        if d.final or d.closed or d.veto or d.day > today:
            continue
        through = min(slot, d.last_slot)
        if through > d.cursor:
            gaps = tuple(range(d.cursor+1, through+1)) if state.publisher_enabled else ()
            start = bool(gaps and d.day == today and not shutdown and stamp % SLOT_US <= 250_000
                         and not any(x.publishers for x in state.days))
            missed = gaps[:-1] if start else gaps
            owners = d.publishers | {slot} if start else d.publishers
            d = replace(d, cursor=through, scheduled=d.scheduled+len(missed), missed=d.missed+len(missed),
                        traces=d.traces+missed, publishers=frozenset(owners), ready=False)
            if start:
                effects.append(StartPublisherCycle(slot))
        if shutdown:
            # End the active interval at this stamp. Later calendar slots were
            # never opportunities; the cursor sentinel prohibits future admission.
            d = replace(d, interval_last_slot=d.cursor, cursor=d.last_slot)
        state = _put(state, d)
    if not any(d.day == today for d in state.days) and not shutdown:
        # Enumerate intervening days too: a delayed scheduler must not skip the
        # new day's midnight-to-wake opportunities or an entire intervening day.
        first = max(d.last_slot for d in state.days)+1
        opened = day_name(first*SLOT_US)
        # Retire only terminal days, retaining a watermark against reopening.
        while len(state.days) >= MAX_DAY_BUCKETS:
            old = state.days[0]
            if not old.closed:
                return state, [*effects, Fault("day_limit")]
            state = replace(state, days=state.days[1:], retired=max(state.retired, old.day))
        d = Day(opened, first, first-1, first+DAY_US//SLOT_US-1)
        state = _put(state, d)
        state, more = _tick(state, stamp, shutdown=shutdown)
        effects.extend(more)
    return replace(state, shutdown=state.shutdown or shutdown), effects


def step(state: State, event: Event) -> tuple[State, tuple[Effect, ...]]:
    """Total over the closed event algebra, including invalid/stale replies.

    Rejected events return the identical input state. A reply cannot manufacture
    an owner, replay a completed owner, retry a final transaction, or reopen a day.
    """
    try:
        return _step(state, event)
    except (ValueError, TypeError, OverflowError, AttributeError):
        return state, (Fault("invalid_event"),)


def _step(state: State, event: Event) -> tuple[State, tuple[Effect, ...]]:
    stamp_day = day_name(event.stamp)
    if isinstance(event, (Tick, ShutdownRequested)):
        existing = next((d for d in state.days if d.day == stamp_day), None)
        if stamp_day < state.current and existing is None:
            return state, (Fault("gone_day"),)
        if stamp_day <= state.retired or (existing is not None and (existing.final or existing.veto)):
            return state, (Fault("frozen_day"),)
        if state.shutdown:
            return state, (Fault("shutdown"),)
        state, effects = _tick(state, event.stamp, shutdown=isinstance(event, ShutdownRequested))
        return _progress(state, effects)
    identity = (day_name(event.slot*SLOT_US) if isinstance(event, SlotOutcome)
                else (event.day or stamp_day) if isinstance(event, AttachmentAdmitted)
                else stamp_day if isinstance(event, ObserverEnqueued) else event.day)
    d = next((d for d in state.days if d.day == identity), None)
    if d is None:
        return state, (Fault("gone_day"),)
    if d.closed or d.veto:
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
        return state, (Fault("frozen_day"),)
    elif isinstance(event, SlotOutcome):
        if event.slot not in d.publishers or event.outcome not in ("missed", "lost_claim", "published", "failed"):
            return state, (Fault("unknown_slot"),)
        d = replace(d, publishers=d.publishers-{event.slot}, scheduled=d.scheduled+1,
                    missed=d.missed+(event.outcome == "missed"), attempted=d.attempted+(event.outcome != "missed"),
                    lost=d.lost+(event.outcome == "lost_claim"), published=d.published+(event.outcome == "published"),
                    failed=d.failed+(event.outcome == "failed"), traces=d.traces+(event.slot,),
                    trace_payloads=d.trace_payloads+((event.slot, event.trace),), ready=False)
    elif isinstance(event, ObserverEnqueued):
        if (event.key in d.observers or state.shutdown or event.stamp < d.observer_stamp
                or (event.stamp == d.observer_stamp and event.key in d.observer_keys)):
            return state, (Fault("observer_admission"),)
        d = replace(d, observers=d.observers | {event.key}, ready=False, observer_stamp=event.stamp,
                    observer_keys=(d.observer_keys if event.stamp == d.observer_stamp else frozenset()) | {event.key})
    elif isinstance(event, ObserverFolded):
        if event.key not in d.observers or event.stamp < d.observer_stamp:
            return state, (Fault("unknown_observer"),)
        d = replace(d, observers=d.observers-{event.key})
    elif isinstance(event, AttachmentAdmitted):
        if (event.key in d.attachments or event.stamp < d.attachment_stamp
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
        d = replace(d, attachments=d.attachments-{event.key}, traces=tuple(s for s in d.traces if s not in event.drained),
                    trace_payloads=tuple(p for p in d.trace_payloads if p[0] not in event.drained))
    elif isinstance(event, FlushReply):
        if not d.flush or not set(event.drained) <= set(d.traces):
            return state, (Fault("unexpected_flush_reply"),)
        d = replace(d, flush=False, ready=event.success and event.ready,
                    traces=tuple(s for s in d.traces if s not in event.drained) if event.success else d.traces,
                    trace_payloads=tuple(p for p in d.trace_payloads if p[0] not in event.drained) if event.success else d.trace_payloads)
        # A failed checkpoint is retried on a later tick, never recursively.
        state = _put(state, d)
        if not event.success or not event.ready:
            return state, ()
    else:
        return state, (Fault("invalid_event"),)
    return _progress(_put(state, d), effects)
