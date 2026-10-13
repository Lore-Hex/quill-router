"""Pure bounded receipt allocator. Callers supply time, identities and acknowledgements."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from trusted_router.async_settle_shadow_v2_a1_evidence import (
    CAP,
    CLOSE_RESULT,
    CLOSURE,
    COUNTER,
    KINDS,
    MANIFEST,
    RETENTION,
    validate_row,
)
from trusted_router.async_settle_shadow_v2_a1_health import HEALTH, HEALTH_CLAIM
from trusted_router.async_settle_shadow_v2_a1_reconciliation import (
    event_locator,
    validate_completion,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    CERTIFICATE_RECORD,
    EVENT,
    EVENT_BYTES,
    EVENT_CAP,
    MAX_DAY_BUCKETS,
    MAX_PAGES,
    PAGE,
    PAGE_BYTES,
    PAGE_EVENTS,
    RESERVATION_BLOCK,
    SAMPLE,
    TERMINAL_CAP,
)
from trusted_router.async_settle_shadow_v2_native import event_bytes
from trusted_router.async_settle_shadow_v2_schema import (
    INT64,
    POLICY,
    day_at,
    identity,
    integer,
    require,
)
from trusted_router.detached_jws import canonical

SAMPLE_KIND, CERTIFICATE_KIND, PAGE_KIND, COUNTER_KIND, CLOSURE_KIND, CONTROL_KIND, HEALTH_KIND, CLAIM_KIND, CLOSE_RESULT_KIND = KINDS
EMPTY_CHAIN = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class Row:
    kind: str
    key: str
    body: bytes
    _validated: bool = field(default=False, init=False, compare=False, repr=False)

    @classmethod
    def build(cls, kind: str, key: str, value: dict[str, Any]) -> Row:
        """The sole stored-body serialization boundary; P1 also checks key identity."""
        schemas = {SAMPLE_KIND: SAMPLE, CERTIFICATE_KIND: CERTIFICATE_RECORD, PAGE_KIND: PAGE, COUNTER_KIND: COUNTER, CLOSURE_KIND: CLOSURE,
                   HEALTH_KIND: HEALTH, CLAIM_KIND: HEALTH_CLAIM, CONTROL_KIND: MANIFEST, CLOSE_RESULT_KIND: CLOSE_RESULT}
        schema = schemas[kind]
        if kind == CONTROL_KIND:
            schema = RETENTION if key == "retention-v2" else CAP if key.endswith("/cap-v2") else MANIFEST
        body = schema.serialize(value)
        row = cls(kind, key, body)
        if kind == COUNTER_KIND:
            # serialize already performed the complete COUNTER schema check.
            # validate_row adds only this key/type check for a counter.
            require(type(kind) is str and type(key) is str and key == value["day"]+"/"+value["boot"], "evidence_identity_gap")
            object.__setattr__(row, "_validated", True)
        else:
            row.value()
        return row

    def value(self) -> dict[str, Any]:
        """The sole stored-body parse boundary."""
        # Frozen exact bytes/key/kind cannot invalidate a successful check.
        # Cache only that fact, never a mutable decoded body or a second payload.
        if self._validated:
            return cast(dict[str, Any], json.loads(self.body))
        value = validate_row(self.kind, self.key, self.body)
        if self.kind == CLOSURE_KIND and value["final_event_seq"] == 0:
            require(value["receipt_chain_sha256"] == EMPTY_CHAIN, "empty_chain")
        object.__setattr__(self, "_validated", True)
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class PageState(Enum):
    OPEN = "open"
    SEALED = "sealed"  # appends stopped; slots/previous digest may be unfinished
    FROZEN = "frozen"
    UNKNOWN = "unknown"
    DURABLE = "durable"  # retained until reference reconciliation passes the HWM


@dataclass(frozen=True)
class Reservation:
    day: str
    boot: str
    first_seq: int
    event_seq: int
    observed_us: int

    @property
    def coordinates(self) -> dict[str, str]:
        return dict(day=self.day, boot=self.boot, first_seq=f"{self.first_seq:020}", event_seq=f"{self.event_seq:020}")


@dataclass
class Slot:
    reservation: Reservation
    body: bytes | None = None
    reconciled: bool = False
    completion_requested: bool = False
    fold_registered: bool = False
    original_page: Row | None = None


@dataclass
class ReceiptPage:
    day: str
    boot: str
    first_seq: int
    slots: list[Slot] = field(default_factory=list)
    state: PageState = PageState.OPEN
    row: Row | None = None

    def wrapper(self, previous: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(v=2, policy_version=POLICY, boot=self.boot, day=self.day,
                    first_seq=f"{self.first_seq:020}",
                    last_seq=f"{self.first_seq + max(0, len(self.slots)-1):020}",
                    previous_page_sha256=previous, events=events)

    @property
    def retained_bytes(self) -> int:
        # Null is shorter than the maximum predecessor digest. Sequence strings
        # are always maximum width, including the future last sequence.
        wrapper = len(canonical(self.wrapper("0" * 64, [])))
        return wrapper + sum(len(s.body) if s.body is not None else EVENT_BYTES for s in self.slots) + max(0, len(self.slots)-1)

    def seal(self) -> None:
        require(self.state == PageState.OPEN, "page_transition")
        self.state = PageState.SEALED

    def freeze(self, previous: str | None) -> bool:
        require(self.state == PageState.SEALED, "page_transition")
        if any(s.body is None for s in self.slots):
            return False
        events = [EVENT.parse(s.body) for s in self.slots if s.body is not None]
        key = f"{self.day}/{self.boot}/{self.first_seq:020}"
        self.row = Row.build(PAGE_KIND, key, self.wrapper(previous, events))
        self.state = PageState.FROZEN
        return True

    def unknown(self) -> None:
        require(self.state == PageState.FROZEN, "page_transition")
        self.state = PageState.UNKNOWN

    def acknowledge(self, body: bytes) -> None:
        require(self.state in (PageState.FROZEN, PageState.UNKNOWN, PageState.DURABLE), "page_transition")
        require(self.row is not None and body == self.row.body, "page_conflict")
        self.state = PageState.DURABLE


@dataclass
class Day:
    next_seq: int = 1
    hwm: int = 0
    chain: str | None = None
    receipts: int = 0
    checkpoint_seq: int = 0
    attempts: int = 0
    failures: int = 0
    recoveries: int = 0
    unknown_commits: int = 0
    unreconciled: int = 0
    losses: int = 0
    resets: int = 0
    closing: bool = False
    closed: bool = False
    sealing: bool = False


class Journal:
    def __init__(self, boot: str) -> None:
        identity(boot)
        self.boot = boot
        self.days: dict[str, Day] = {}
        self.pages: list[ReceiptPage] = []
        self.permits: dict[str, int] = {}
        self.batches: dict[Reservation, tuple[Row, ...]] = {}
        self.terminal_permits: dict[str, int] = {}
        self.losses = 0
        self.stopped = False
        self.retired_through = ""

    @property
    def retained_bytes(self) -> int:
        return sum(p.retained_bytes for p in self.pages)

    def loss(self, day: str) -> None:
        self.losses += 1
        if day in self.days:
            self.days[day].losses += 1

    def open_day(self, day: str) -> Day:
        require(day > self.retired_through, "closed_day")
        if day not in self.days:
            require(len(self.days) < MAX_DAY_BUCKETS, "day_buckets")
            self.days[day] = Day()
        return self.days[day]

    def grant(self, day: str, permits: int) -> None:
        require(permits == RESERVATION_BLOCK, "event_permits")
        self.open_day(day)
        self.permits[day] = self.permits.get(day, 0) + permits

    def reserve(self, observed_us: int) -> Reservation | None:
        """No I/O or wait. Failed reservation is sticky; no key/sequence reuse."""
        integer(observed_us)
        day = day_at(observed_us)
        try:
            state = self.open_day(day)
            require(not self.stopped and not state.closing and not state.closed, "closed_day")
            require(state.next_seq <= INT64 and self.permits.get(day, 0) > 0, "event_permits")
            page = next((p for p in reversed(self.pages) if p.day == day and p.state == PageState.OPEN), None)
            if page is not None and (len(page.slots) == PAGE_EVENTS or page.retained_bytes + EVENT_BYTES + 1 > PAGE_BYTES):
                page.seal()
                page = None
            if page is None:
                require(len(self.pages) < MAX_PAGES, "page_count")
                page = ReceiptPage(day, self.boot, state.next_seq)
                self.pages.append(page)
            reservation = Reservation(day, self.boot, page.first_seq, state.next_seq, observed_us)
            slot = Slot(reservation)
            page.slots.append(slot)
            # One admission site for both per-page and aggregate reserved bytes.
            if page.retained_bytes > PAGE_BYTES or self.retained_bytes > MAX_PAGES * PAGE_BYTES:
                page.slots.pop()
                raise ValueError("page_bytes")
            state.next_seq += 1
            self.permits[day] -= 1
            return reservation
        except ValueError:
            self.loss(day)
            return None

    def slot(self, reservation: Reservation) -> Slot:
        for page in self.pages:
            if (page.day, page.boot, page.first_seq) == (reservation.day, reservation.boot, reservation.first_seq):
                index = reservation.event_seq - page.first_seq
                if 0 <= index < len(page.slots) and page.slots[index].reservation == reservation:
                    return page.slots[index]
        raise ValueError("unknown_reservation")

    def finalize(self, reservation: Reservation, event: dict[str, Any]) -> bytes:
        """Immutable complete bytes, before exposing a locator or certificate."""
        slot = self.slot(reservation)
        body = event_bytes(event)
        require(event["seq"] == f"{reservation.event_seq:020}" and event["event_at_us"] == reservation.observed_us,
                "reservation_identity")
        if event["receipt_locator"] is not None:
            require(event["receipt_locator"] == reservation.coordinates, "reservation_identity")
        require(slot.body is None or slot.body == body, "slot_conflict")
        slot.body = body
        return body

    def complete(self, reservation: Reservation, rows: tuple[Row, ...] = ()) -> bool:
        """Queue immutable evidence for maintenance verification; never advance HWM.

        Retrying the same submission is idempotent. Storage acknowledgement is
        exclusively verify_complete's responsibility after exact-key reads.
        """
        slot = self.slot(reservation)
        require(slot.body is not None, "unfinished_slot")
        if slot.completion_requested:
            require(self.batches.get(reservation, ()) == rows, "batch_conflict")
            return True
        require(len(rows) <= 2 and len({r.kind for r in rows}) == len(rows), "batch_count")
        for row in rows:
            require(row.kind in (SAMPLE_KIND, CERTIFICATE_KIND), "batch_kind")
            row.value()
        size = self.work_bytes
        if (bool(rows) and sum(bool(b) for b in self.batches.values()) >= 32) or size + sum(len(r.body) for r in rows) > 256*1024:
            self.loss(reservation.day)
            return False
        self.batches[reservation] = rows
        slot.completion_requested = True
        return True

    @property
    def work_bytes(self) -> int:
        return (sum(len(r.body) for batch in self.batches.values() for r in batch)
                + sum(len(s.original_page.body) for p in self.pages for s in p.slots if s.original_page))

    def verify_complete(self, reservation: Reservation, durable: list[Row]) -> bool:
        """Pure reference reconciliation over rows read by the maintenance shell.

        A caller's complete() is only a request. Exact page/event and referenced
        sample/certificate identities must all match before the barrier releases.
        """
        slot = self.slot(reservation)
        require(slot.completion_requested and slot.body is not None, "completion_not_requested")
        found = {(r.kind, r.key): r for r in durable}
        page = found.get((PAGE_KIND, f"{reservation.day}/{reservation.boot}/{reservation.first_seq:020}"))
        require(page is not None, "reference_missing")
        assert page is not None
        expected = next((p.row for p in self.pages if p.day == reservation.day
                         and p.first_seq == reservation.first_seq), None)
        require(page == expected, "reference_conflict")
        for row in self.batches[reservation]:
            require(found.get((row.kind, row.key)) == row, "reference_conflict")
        assert slot.body is not None
        event = EVENT.parse(slot.body)
        if slot.original_page is not None:
            require(found.get((PAGE_KIND, slot.original_page.key)) == slot.original_page, "reference_conflict")
        if event["phase"] in ("settle", "refund", "completion"):
            if event["booking"]["outcome"] in ("pending", "unknown"):
                return False  # A separately verified completion can release this barrier.
            key = event["authorization_day"] + "/" + event["authorization_id"]
            if event["proof_kind"] in ("compared", "observed_binding", "certificate"):
                kind = CERTIFICATE_KIND if event["proof_kind"] == "certificate" else SAMPLE_KIND
                backing = found.get((kind, key))
                require(backing is not None, "reference_missing")
                assert backing is not None
                value = backing.value()
                if kind == SAMPLE_KIND:
                    reference = (event["retry_original"] if event["phase"] == "completion" or event["retry_original"] is not None
                                 else dict(day=reservation.day, boot=reservation.boot, event_seq=f"{reservation.event_seq:020}"))
                    if reference is None and event["phase"] == "completion":
                        reference = dict(day=reservation.day, boot=reservation.boot, event_seq=event["completion_of"])
                    backing_candidates: tuple[dict[str, Any], ...] = (event,)
                    if event["phase"] == "completion":
                        originals = [e for r in found.values() if r.kind == PAGE_KIND
                                     and r.value()["day"] == reference["day"]
                                     and r.value()["boot"] == reference["boot"]
                                     for e in r.value()["events"] if e["seq"] == reference["event_seq"]]
                        require(len(originals) == 1, "retry_original_gap")
                        original_event = originals[0]
                        # An old unknown sample backs only a completion that
                        # preserves its original class. A later upgrade needs
                        # the later sample matching that completion itself.
                        if all(event[k] == original_event[k] for k in ("partition", "comparison")):
                            backing_candidates += (original_event,)
                    require(value["original_terminal_receipt"] == reference
                            and value["payload_hash"] == event["payload_sha256"]
                            and any(value["booking"]["outcome"] == candidate["booking"]["outcome"]
                                    and value["classification"] == candidate["comparison"]
                                    for candidate in backing_candidates), "reference_conflict")
                else:
                    require(value["certificate_sha256"] == event["certificate_sha256"]
                        and value["claims"]["original_receipt"] == event["original_receipt"], "reference_conflict")
                    locator = event["original_receipt"]
                    key = f'{locator["day"]}/{locator["boot"]}/{locator["first_seq"]}'
                    original = found.get((PAGE_KIND, key))
                    require(original is not None, "reference_missing")
                    assert original is not None
                    matches = [e for e in original.value()["events"] if e["seq"] == locator["event_seq"]]
                    require(len(matches) == 1 and hashlib.sha256(EVENT.serialize(matches[0])).hexdigest() == locator["event_sha256"],
                            "reference_conflict")
                    require(matches[0]["identity"] == event["identity"] and matches[0]["decision"] == event["decision"],
                            "reference_conflict")
        original_day = reservation.day
        if event["phase"] == "completion":
            reference = event["retry_original"] or dict(day=reservation.day, boot=reservation.boot,
                                                       event_seq=event["completion_of"])
            originals = [r for r in found.values() if r.kind == PAGE_KIND
                         and r.value()["day"] == reference["day"] and r.value()["boot"] == reference["boot"]
                         and any(e["seq"] == reference["event_seq"] for e in r.value()["events"])]
            require(len(originals) == 1, "retry_original_gap")
            original_value = originals[0].value()
            validate_completion(original_value, event_locator(original_value, reference["event_seq"]),
                                page.value(), event_locator(page.value(), event["seq"]))
            original_slot = next((s for p in self.pages for s in p.slots
                if (s.reservation.day, s.reservation.boot, f"{s.reservation.event_seq:020}") ==
                   (reference["day"], reference["boot"], reference["event_seq"])), None)
            if original_slot is not None:
                require(original_slot.completion_requested and original_slot.body is not None, "completion_order")
                assert original_slot.body is not None
                for row in self.batches.get(original_slot.reservation, ()):
                    require(found.get((row.kind, row.key)) == row, "reference_conflict")
                original_slot.reconciled = True
                original_day = original_slot.reservation.day
                self.batches.pop(original_slot.reservation, None)
        slot.reconciled = True
        del self.batches[reservation]
        self.advance(original_day)
        if original_day != reservation.day:
            self.advance(reservation.day)
        return True

    def complete_event(self, reservation: Reservation, original_page: Row, event: dict[str, Any]) -> bytes:
        page = original_page.value()
        require(original_page.kind == PAGE_KIND, "completion_writer")
        completion = ReceiptPage(reservation.day, reservation.boot, reservation.event_seq)
        completion.slots.append(Slot(reservation))
        proposed = completion.wrapper(None, [event])
        validate_completion(page, event_locator(page, event.get("completion_of")),
                            proposed, event_locator(proposed, event.get("seq")))
        slot = self.slot(reservation)
        require(slot.original_page in (None, original_page), "completion_conflict")
        require(self.work_bytes + (len(original_page.body) if slot.original_page is None else 0) <= 256*1024, "batch_bound")
        body = self.finalize(reservation, event)
        slot.original_page = original_page
        return body

    def freeze(self, day: str) -> list[Row]:
        previous = self.days[day].chain
        for page in self.pages:
            if page.day == day and page.state == PageState.OPEN:
                page.seal()
        result = []
        for page in self.pages:
            if page.day != day:
                continue
            if page.state == PageState.OPEN:
                page.seal()
            if page.state == PageState.SEALED and not page.freeze(previous):
                break
            assert page.row is not None
            previous = page.row.digest
            if page.state in (PageState.FROZEN, PageState.UNKNOWN):
                result.append(page.row)
        return result

    def acknowledge(self, rows: list[Row]) -> None:
        for row in rows:
            page = next((p for p in self.pages if p.row == row), None)
            require(page is not None, "page_identity")
            assert page is not None
            page.acknowledge(row.body)
        for day in self.days:
            self.advance(day)

    def advance(self, day: str) -> None:
        state = self.days[day]
        for page in list(self.pages):
            if page.day != day:
                continue
            if (page.state != PageState.DURABLE or page.first_seq != state.hwm + 1
                    or not all(s.reconciled for s in page.slots)):
                break
            assert page.row is not None
            state.hwm += len(page.slots)
            state.receipts += len(page.slots)
            state.chain = page.row.digest
            self.pages.remove(page)

    def checkpoint(self, day: str) -> int:
        state = self.days[day]
        require(not state.closed, "closed_day")
        state.checkpoint_seq += 1
        state.attempts += 1
        return state.checkpoint_seq

    def failed(self, day: str, *, unknown: bool) -> None:
        state = self.days[day]
        state.failures += 1
        if unknown:
            state.unknown_commits += 1
            state.unreconciled += 1
            for page in self.pages:
                if page.day == day and page.state == PageState.FROZEN:
                    page.unknown()

    def reconciled_commit(self, day: str) -> None:
        state = self.days[day]
        require(state.unreconciled > 0, "commit_transition")
        state.unreconciled -= 1

    def can_close(self, day: str, *, inflight: int, pending: int, interval_end_us: int, folded_us: int) -> bool:
        state = self.days[day]
        return (day == min(self.days) and state.closing and not state.closed and inflight == pending == 0
                and folded_us >= interval_end_us and state.unreconciled == 0
                and state.hwm == state.next_seq - 1 and self.losses == state.losses == state.resets == 0)

    def closed(self, day: str) -> None:
        state = self.days[day]
        require(state.closing and not any(p.day == day for p in self.pages), "closure_transition")
        state.closed = True
        self.retired_through = max(self.retired_through, day)
        del self.days[day]
        self.permits.pop(day, None)
        self.terminal_permits.pop(day, None)


def reserve_cap(value: dict[str, Any], *, terminal: bool, event: bool) -> dict[str, Any]:
    CAP(value)
    result = dict(value)
    for name, requested, maximum in (("terminal_reserved", terminal, TERMINAL_CAP), ("event_reserved", event, EVENT_CAP)):
        if requested:
            require(result[name] + RESERVATION_BLOCK <= maximum, "daily_cap")
            result[name] += RESERVATION_BLOCK
    CAP(result)
    return result
