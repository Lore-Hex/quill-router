"""Lifespan-owned v2 observation. No request handler installs or starts this service."""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import signal
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar, cast

from trusted_router.async_settle_shadow_attachments import Lane, Transport
from trusted_router.async_settle_shadow_evidence import HIST_BOUNDS, Counters
from trusted_router.async_settle_shadow_health_state import (
    CYCLE_SECONDS,
    JITTER_US,
    SLOT_US,
    Slots,
    health_basis,
    prediction,
)
from trusted_router.async_settle_shadow_journal import (
    CERTIFICATE_KIND,
    CLOSE_RESULT_KIND,
    CLOSURE_KIND,
    COUNTER_KIND,
    EMPTY_CHAIN,
    PAGE_KIND,
    SAMPLE_KIND,
    Journal,
    Reservation,
    Row,
)
from trusted_router.async_settle_shadow_v2_a1_evidence import COUNTER, validate_close_result
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_OPERATIONS,
    PUBLISHER_SLOT_TRACE,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    BUCKET,
    CHECKPOINT_FIELDS,
    COUNTS,
    EVENT,
    RESERVATION_BLOCK,
)
from trusted_router.async_settle_shadow_v2_schema import POLICY, day_at, require, sha256
from trusted_router.config import Settings
from trusted_router.services.async_settle_shadow_admission import Observer
from trusted_router.storage_gcp_async_admission import read_admission
from trusted_router.storage_gcp_shadow_attachments import GCSAttachments
from trusted_router.storage_gcp_shadow_io import observe_spanner_rpcs, rpc_scope
from trusted_router.storage_gcp_shadow_observation import (
    FLUSH_SECONDS,
    Budget,
    ObservationStore,
    UnknownCommit,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _ObserverQuiesced(Exception):
    pass


class HealthObserver(Observer):
    """V2 owns queued outcomes until they are accounted and folded."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.owners: dict[str | None, tuple[str, int]] = {}
        self.outcome = threading.local()
        self.scheduled_owner: tuple[str, int] | None = None
        observer = self
        class Ticks(dict[str | None, float]):
            def __setitem__(self, key: str | None, value: float) -> None:
                require(observer.scheduled_owner is not None, "observer_schedule_owner")
                assert observer.scheduled_owner is not None
                observer.owners[key] = observer.scheduled_owner
                observer.scheduled_owner = None
                super().__setitem__(key, value)
        self.inflight_ticks = Ticks()

    def _count(self, field: str) -> None:
        owner = getattr(self.outcome, "owner", None)
        scheduled = field in ("health_reads", "workspace_reads")
        if scheduled:
            stamp = int(self.evidence_wall()*1_000_000)
            owner = self.scheduled_owner = (day_at(stamp), stamp)
        if owner is None:
            super()._count(field)
            return
        day, stamp = owner
        self.counts[field] += 1
        bucket = (self._daily_bucket(stamp/1_000_000) if scheduled else
                  self.daily_counts.setdefault(day, dict(max_consecutive_failures=0,
                      degraded_seconds=0., started_at_us=stamp)))
        bucket[field] = bucket.get(field, 0) + 1

    def _owned(self, key: str | None, started: float, read: Callable[[], None]) -> None:
        with self.lock:
            self.outcome.owner = self.owners.get(key)
        try:
            read()
        finally:
            with self.lock:
                if self.inflight_ticks.get(key) == started:
                    self.inflight_ticks.pop(key)
                    owner = self.owners.pop(key, None)
                    if owner is not None:
                        bucket = self.daily_counts[owner[0]]
                        bucket["max_consecutive_failures"] = max(bucket["max_consecutive_failures"], self.failures[key])
                self.outcome.owner = None

    def _health(self, started: float) -> None:
        self._owned(None, started, lambda: super(HealthObserver, self)._health(started))

    def _workspace(self, key: str, started: float) -> None:
        self._owned(key, started, lambda: super(HealthObserver, self)._workspace(key, started))

    async def run(self) -> None:
        enabled = self.enabled
        def scheduling_enabled() -> bool:
            if not enabled():
                raise _ObserverQuiesced
            return True
        self.enabled = scheduling_enabled
        try:
            await super().run()
        except _ObserverQuiesced:
            pass
        finally:
            self.enabled = enabled

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()  # Only the scheduling loop; submitted reads remain owned.
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.executor is not None:
            await asyncio.to_thread(self.executor.shutdown, wait=True)
        await super().close()

    def install_health(self, started: float, value: dict[str, Any] | None) -> None:
        if self.stopped:
            return
        with self.lock:
            if self.clock() - started > .25:
                self._failure(None, started, "late_installs")
            elif self._success(None, started):
                self.health = dict(value) if value is not None else None


class Port:
    """Nonblocking request facade. No method calls storage or starts maintenance.

    Contention, saturation or invalid input returns None/False and poisons close.
    complete queues verification, not a durable acknowledgement. Only maintenance
    grants permits, writes retained batches, reads references and advances HWM.
    """
    def __init__(self, service: Service) -> None:
        self.service = service
        self.lost = threading.Event()

    def call(self, action: Callable[[], T]) -> T | None:
        svc = self.service
        if not svc.lock.acquire(blocking=False):
            self.lost.set()
            return None
        try:
            return action()
        except (ValueError, KeyError):
            self.lost.set()
            return None
        finally:
            svc.lock.release()

    def reserve(self, observed_us: int, *, terminal: bool = False) -> Reservation | None:
        def action() -> Reservation | None:
            svc, day = self.service, day_at(observed_us)
            require(svc.enabled() and not svc.journal.stopped, "quiesced")
            require(day in svc.counters, "day_not_ready")
            if terminal:
                require(svc.journal.terminal_permits.get(day, 0) > 0, "terminal_permits")
                svc.journal.terminal_permits[day] -= 1
            return svc.journal.reserve(observed_us)
        return self.call(action)

    def finalize(self, reservation: Reservation, event: dict[str, Any]) -> bytes | None:
        return self.call(lambda: self.service.journal.finalize(reservation, event))

    def complete(self, reservation: Reservation, rows: tuple[Row, ...] = ()) -> bool:
        return bool(self.call(lambda: self.service.journal.complete(reservation, rows)))

    def complete_event(self, reservation: Reservation, original_page: Row, event: dict[str, Any]) -> bytes | None:
        return self.call(lambda: self.service.journal.complete_event(reservation, original_page, event))

    def capture_prediction(self, workspace: str, observed_us: int) -> dict[str, Any] | None:
        """P4 captures the prediction and its immutable basis at one cache instant."""
        def action() -> dict[str, Any]:
            svc = self.service
            day = day_at(observed_us)
            require(day in svc.counters and not svc.journal.days[day].sealing, "closed_day")
            now = svc.store.clock()
            svc.basis_tokens = min(10., svc.basis_tokens + max(0., now-svc.basis_tick)*2)
            svc.basis_tick = now
            require(svc.basis_tokens >= 1, "basis_rate")
            svc.basis_tokens -= 1
            require(svc.observer.lock.acquire(blocking=False), "cache_busy")
            try:
                at = svc.observer.clock()
                value = health_basis(svc.observer, svc.journal.boot, at=at)
                result = prediction(svc.observer, workspace, at=at)
            finally:
                svc.observer.lock.release()
            result["basis_sha256"] = svc.bases.append(day, value)
            return result
        return self.call(action)

    def fold_counts(self, day: str, bucket: dict[str, Any]) -> bool:
        """Fold one P1 BUCKET delta atomically, including eligible demand.

        Unknown demand remains unknown; no cumulative field can be decremented.
        P4 supplies each attempt exactly once after its immutable result exists.
        """
        def action() -> bool:
            BUCKET(bucket)
            svc = self.service
            require(not svc.journal.days[day].closed and not svc.journal.days[day].sealing, "closed_day")
            body = copy.deepcopy(svc.counters[day])
            target = next((v for v in body["buckets"] if (v["phase"], v["dimensions"]) ==
                           (bucket["phase"], bucket["dimensions"])), None)
            if target is None:
                target = dict(phase=bucket["phase"], dimensions=copy.deepcopy(bucket["dimensions"]),
                              **dict.fromkeys(COUNTS, 0), eligible_demand=0)
                body["buckets"].append(target)
            for key in COUNTS:
                target[key] += bucket[key]
                body[key] += bucket[key]
            for obj in (target, body):
                obj["eligible_demand"] = (None if obj["eligible_demand"] is None or bucket["eligible_demand"] is None
                                          else obj["eligible_demand"] + bucket["eligible_demand"])
            dims = bucket["dimensions"]
            legacy = next(v for v in body["counts"] if all(v[k] == dims[k] for k in dims))
            for key in ("authorize_attempts", "authorize_fresh", "authorize_replay", "settle_attempts", "refund_attempts"):
                legacy[key] += bucket[key]
            terminals = bucket["settle_attempts"] + bucket["refund_attempts"]
            legacy["observed_attempts"] += terminals
            eligible = bucket["eligible_demand"] or 0
            legacy["observed_eligible"] += eligible
            legacy["observed_ineligible"] += bucket["B"]
            legacy["observed_unknown"] += terminals - eligible - bucket["B"]
            # Validate the prospective checkpoint without creating an attempt.
            check = dict(body, sequence=max(1, body["sequence"]), checkpoint_seq=max(1, body["sequence"]),
                         checkpoint_attempts=max(1, body["checkpoint_attempts"]),
                         flushed_at_us=body["started_at_us"])
            COUNTER(check)
            svc.counters[day] = body
            return True
        return bool(self.call(action))


class Service:
    def __init__(self, settings: Settings, store: ObservationStore, transport: Transport | None = None) -> None:
        self.settings, self.store = settings, store
        self.workspaces = settings.async_settle_shadow_workspace_ids
        self.legacy = Counters(settings.effective_serving_region, settings.source_revision)
        self.journal = Journal(self.legacy.instance)
        if transport is None and settings.async_settle_shadow_attachment_bucket:
            transport = GCSAttachments(settings.async_settle_shadow_attachment_bucket)
        self.bases = Lane("basis", self.journal.boot, transport)
        self.traces = Lane("trace", self.journal.boot, transport)
        self.basis_tokens, self.basis_tick = 10., store.clock()
        self.observer = HealthObserver(self.workspaces, partial(read_admission, store.workspace_database), store.read_shadow_health,
            cap=settings.async_settle_pilot_cap_micro, enabled=self.enabled, policy=POLICY,
            clock=store.clock, wall=store.wall)
        cast(Any, self.observer).lock = threading.RLock()
        self.lock = threading.RLock()
        self.maintenance_lock = threading.Lock()
        self.port = Port(self)
        self.close_failures: dict[str, int] = {}
        self.final_attempted: set[str] = set()
        self.final_veto: set[str] = set()
        self.handoff: tuple[str, list[Row], Row, Row] | None = None
        self.publishing: set[str] = set()
        self.slots = Slots()
        self.last_claim_start = float("-inf")
        self.stopped = asyncio.Event()
        self.evidence_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-evidence")
        self.publisher_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-health")
        self.task: asyncio.Task[None] | None = None
        self.publisher_job: asyncio.Future[Any] | None = None
        self.flush_job: asyncio.Future[Any] | None = None
        self.counters: dict[str, dict[str, Any]] = {}
        self.unknown_batches: set[Reservation] = set()
        self.unknown: tuple[str, list[Row], Row, Row | None] | None = None
        self.next_allocate = float("-inf")
        self.next_flush = float("-inf")
        self.previous_closure: str | None = None
        self.last_closure_ack: dict[str, Any] | None = None
        self.folded_us = 0
        self.inflight = self.pending = 0  # P4 owns acquisition/release around its shadow work.
        self.closing_reason: str | None = None
        self.interval_end_us: int | None = None
        self.unclosed = False
        self.termination_deadline: float | None = None
        self.signal_handler: Any = None
        self.previous_signal_handler: Any = None
        self.close_task: asyncio.Task[Any] | None = None
        self.close_finished = False
        self.close_done = asyncio.Event()
        self.ensure_day(int(store.wall()*1_000_000))

    def enabled(self) -> bool:
        return (self.settings.async_settle_shadow_policy == POLICY
                and self.settings.async_settle_shadow_workspace_ids == self.workspaces
                and bool(self.workspaces) and not self.settings.async_settle_enabled)

    def contender(self) -> bool:
        return (self.enabled() and bool(self.settings.async_settle_shadow_health_region)
                and self.settings.effective_serving_region == self.settings.async_settle_shadow_health_region)

    def ensure_day(self, now_us: int) -> str:
        day = day_at(now_us)
        self.journal.open_day(day)
        if day not in self.counters:
            # Reuse v1's initializer for retained fields, never re-declare them.
            self.legacy.clock = lambda: now_us / 1_000_000
            base = copy.deepcopy(self.legacy._day())
            base.update(v=2, policy_version=POLICY, contract_revision="a1", day=day, boot=self.journal.boot,
                **dict.fromkeys(COUNTS + CHECKPOINT_FIELDS, 0), eligible_demand=0,
                event_seq=0, durable_event_hwm=0, checkpoint_seq=0, receipt_count=0,
                receipt_chain_sha256=None, loss_count=0, reset_count=0, diagnostics={}, buckets=[],
                publisher=dict(**dict.fromkeys(set(PUBLISHER_OPERATIONS.fields)-{"cycle_us", "rpc_us"}, 0),
                    cycle_us=dict(bounds_us=list(HIST_BOUNDS), counts=[0]*9),
                    rpc_us=dict(bounds_us=list(HIST_BOUNDS), counts=[0]*9)))
            self.counters[day] = base
        return day

    def operation(self, day: str, name: str, count: int = 1) -> None:
        with self.lock:
            self.counters[day]["publisher"][name] += count

    def schedule_cycle(self, slot: int, claim_us: int) -> None:
        # Called under the service lock BEFORE executor submission.
        day = self.ensure_day(claim_us)
        self.operation(day, "scheduled_slots")
        self.publishing.add(day)

    def publish_cycle(self, slot: int, claim_us: int) -> None:
        day = day_at(claim_us)
        with self.lock:
            if day not in self.publishing:
                self.schedule_cycle(slot, claim_us)
        started = self.store.clock()
        budget = Budget(started + CYCLE_SECONDS, self.store.clock)
        attempted = False
        outcome = "missed_slots"
        unknown = False
        value = None
        ack_us = None
        rpc_durations: list[int] = []
        actual_us = claim_us
        self.store.last_claim = None
        def record(elapsed: int) -> None:
            rpc_durations.append(elapsed)
            self.histogram(day, "rpc_us", elapsed)
        try:
            with self.lock:
                actual_us = int(self.store.wall()*1_000_000)
                if (not self.enabled() or self.stopped.is_set() or actual_us // SLOT_US != slot
                        or actual_us % SLOT_US > JITTER_US or self.store.clock()-self.last_claim_start < 2):
                    return
                budget.remaining(CYCLE_SECONDS)
                self.operation(day, "claim_attempts")
                self.last_claim_start = self.store.clock()
                attempted = True
            outcome = "failures"
            with rpc_scope(budget.deadline, CYCLE_SECONDS, self.store.clock), observe_spanner_rpcs(
                    record):
                if not self.store.claim(self.journal.boot, slot, actual_us, budget):
                    outcome = "lost_claims"
                    return
                self.operation(day, "claims")
                self.operation(day, "scans")
                value = self.store.observe(self.journal.boot, self.settings.effective_serving_region,
                                           self.settings.source_revision, slot, budget)
                if not self.enabled() or not self.store.publish(value, budget):
                    return
                budget.remaining(CYCLE_SECONDS)
                ack_us = int(self.store.wall()*1_000_000)
                outcome = "publications"
        except Exception as error:
            unknown = isinstance(error, UnknownCommit)
        finally:
            elapsed = max(0, round((self.store.clock()-started)*1_000_000))
            late = attempted and elapsed > round(CYCLE_SECONDS*1_000_000)
            if late:
                outcome = "failures"
            with self.lock:
                self.operation(day, outcome)
                if unknown:
                    self.operation(day, "unknown_outcomes")
                if late:
                    self.operation(day, "late_cycles")
                if attempted:
                    self.histogram(day, "cycle_us", elapsed)
                claim = self.store.last_claim
                trace = dict(v=2, policy_version=POLICY, contract_revision="a1", day=day, boot=self.journal.boot,
                    slot=slot, claim_start_us=actual_us if attempted else None,
                    claim_at_ns=claim["claim_at_ns"] if claim else None,
                    observed_at_ns=value["observed_at_ns"] if value else None,
                    publisher_heartbeat_ns=value["publisher_heartbeat_ns"] if value else None,
                    body_sha256=sha256(value) if value else None, ack_at_us=ack_us,
                    cycle_us=elapsed if attempted else None,
                    outcome={"missed_slots": "missed", "lost_claims": "lost_claim", "publications": "published", "failures": "failed"}[outcome],
                    unknown_outcome=unknown, late_cycle=late, rpc_count=len(rpc_durations), rpc_elapsed_us=sum(rpc_durations))
                try:
                    PUBLISHER_SLOT_TRACE(trace)
                    self.traces.append(day, trace)
                except ValueError:
                    self.journal.loss(day)
                finally:
                    self.publishing.discard(day)

    def missed_slot(self, slot: int, *, scheduled: bool = False) -> None:
        day = self.ensure_day(slot*SLOT_US)
        if not scheduled:
            self.operation(day, "scheduled_slots")
        self.operation(day, "missed_slots")
        trace = dict(v=2, policy_version=POLICY, contract_revision="a1", day=day, boot=self.journal.boot,
            slot=slot, claim_start_us=None, claim_at_ns=None, observed_at_ns=None,
            publisher_heartbeat_ns=None, ack_at_us=None, cycle_us=None, body_sha256=None,
            outcome="missed", unknown_outcome=False, late_cycle=False, rpc_count=0, rpc_elapsed_us=0)
        try:
            PUBLISHER_SLOT_TRACE(trace)
            self.traces.append(day, trace)
        except ValueError:
            self.journal.loss(day)

    def histogram(self, day: str, name: str, elapsed: int) -> None:
        with self.lock:
            histogram = self.counters[day]["publisher"][name]
            histogram["counts"][sum(elapsed > bound for bound in histogram["bounds_us"])] += 1

    def fold(self, now_us: int) -> None:
        daily, overflow, _ = self.observer.snapshot_daily_counts()
        for day, values in daily.items():
            if day <= self.journal.retired_through:
                for current in self.journal.days:
                    self.journal.loss(current)
                continue
            if day not in self.counters:
                self.ensure_day(int(values["started_at_us"]))
            counter = self.counters[day]
            for key, value in values.items():
                if key in counter["admission_observer"]:
                    counter["admission_observer"][key] = max(counter["admission_observer"][key], math.ceil(value))
            if overflow:
                self.journal.loss(day)
        self.folded_us = now_us

    def counter_row(self, day: str, now_us: int, *, closed: bool) -> Row:
        state = self.journal.days[day]
        body = copy.deepcopy(self.counters[day])
        body.update(sequence=state.checkpoint_seq, checkpoint_seq=state.checkpoint_seq,
            flushed_at_us=now_us, closed=closed, event_seq=state.next_seq-1,
            durable_event_hwm=state.hwm, receipt_count=state.receipts, receipt_chain_sha256=state.chain,
            checkpoint_attempts=state.attempts, checkpoint_failures=state.failures,
            checkpoint_recoveries=state.failures if closed else state.recoveries,
            unknown_commits=state.unknown_commits, unreconciled_commits=state.unreconciled,
            irrecoverable_losses=state.losses, loss_count=state.losses, reset_count=state.resets)
        return Row.build(COUNTER_KIND, day + "/" + self.journal.boot, body)

    def closure_row(self, day: str, counter: Row, end_us: int, reason: str) -> Row:
        state = self.journal.days[day]
        return Row.build(CLOSURE_KIND, counter.key, dict(v=2, policy_version=POLICY, contract_revision="a1",
            authorization_obligations="receipt_chain",
            day=day, boot=self.journal.boot, router_revision=self.settings.source_revision,
            region=self.settings.effective_serving_region, reason=reason, interval_end_us=end_us,
            final_event_seq=state.next_seq-1, final_checkpoint_seq=state.checkpoint_seq,
            durable_event_hwm=state.hwm, receipt_chain_sha256=state.chain or EMPTY_CHAIN,
            counter_sha256=counter.digest, retained_work=0, pending_comparisons=0,
            unresolved_commits=0, loss_count=state.losses, observer_fold_through_us=self.folded_us,
            previous_day_closure_sha256=self.previous_closure))

    def acknowledged(self, day: str, pages: list[Row], closure: Row | None) -> None:
        self.journal.acknowledge(pages)
        if closure is not None:
            self.previous_closure = closure.digest
            self.journal.closed(day)
            self.final_attempted.discard(day)
            self.counters.pop(day)
            self.legacy.days.pop(day, None)
            self.observer.acknowledge_daily_counts(day, day_at(self.folded_us))

    def reconcile_work(self, budget: Budget) -> None:
        with self.lock:
            batches = list(self.journal.batches.items())
        cache: dict[tuple[str, str], Row | None] = {}
        def read(kind: str, key: str) -> Row | None:
            identity = (kind, key)
            if identity in cache:
                return cache[identity]
            value = self.store.read(kind, key, budget)
            # A later retained batch can create a previously absent reference.
            if value is not None:
                cache[identity] = value
            return value
        for reservation, rows in batches:
            with self.lock:
                if reservation not in self.journal.batches:
                    continue  # A durable completion released its original in this pass.
            try:
                if rows and reservation not in self.unknown_batches:
                    try:
                        self.store.retain(rows, budget)
                    except UnknownCommit:
                        with self.lock:
                            self.unknown_batches.add(reservation)
                            self.journal.failed(reservation.day, unknown=True)
                        return
                page = read(PAGE_KIND, f"{reservation.day}/{reservation.boot}/{reservation.first_seq:020}")
                if page is None:
                    continue
                durable = [page]
                for row in rows:
                    found = read(row.kind, row.key)
                    if found != row:
                        break
                    durable.append(found)
                else:
                    with self.lock:
                        slot = self.journal.slot(reservation)
                        assert slot.body is not None
                        event = EVENT.parse(slot.body)
                        original = slot.original_page
                    if original is None and event["phase"] == "completion":
                        reference = event["retry_original"] or dict(day=reservation.day, boot=reservation.boot,
                                                                   event_seq=event["completion_of"])
                        with self.lock:
                            original = next((p.row for p in self.journal.pages
                                if p.day == reference["day"] and p.boot == reference["boot"] and p.row is not None
                                and any(e["seq"] == reference["event_seq"] for e in p.row.value()["events"])), None)
                        if original is None:
                            continue  # No exact page coordinate: unresolved, never guessed/scanned.
                    if original is not None:
                        found = read(original.kind, original.key)
                        if found != original:
                            continue
                        durable.append(found)
                    if event.get("proof_kind") in ("compared", "observed_binding", "certificate"):
                        kind = CERTIFICATE_KIND if event["proof_kind"] == "certificate" else SAMPLE_KIND
                        key = event["authorization_day"]+"/"+event["authorization_id"]
                        if not any(r.kind == kind and r.key == key for r in durable):
                            found = read(kind, key)
                            if found is not None:
                                durable.append(found)
                    if event.get("original_receipt") is not None:
                        locator = event["original_receipt"]
                        key = f'{locator["day"]}/{locator["boot"]}/{locator["first_seq"]}'
                        found = read(PAGE_KIND, key)
                        if found is not None:
                            durable.append(found)
                    if reservation in self.unknown_batches and not self.store.reconcile(
                            list(rows), budget, writer_key=reservation.day+"/"+reservation.boot):
                        continue
                    with self.lock:
                        if reservation in self.unknown_batches:
                            self.journal.reconciled_commit(reservation.day)
                            self.unknown_batches.remove(reservation)
                        self.journal.verify_complete(reservation, durable)
            except Exception:
                with self.lock:
                    self.journal.failed(reservation.day, unknown=False)
                return

    def flush(self, deadline: float) -> None:
        # Maintenance is serialized independently of request-facing state.
        with self.maintenance_lock:
            self._flush(deadline)

    def _flush(self, deadline: float) -> None:
        budget = Budget(deadline, self.store.clock)
        self.bases.step(budget)
        self.traces.step(budget)
        with self.lock:
            now_us = int(self.store.wall()*1_000_000)
            today = day_at(now_us)
            if self.closing_reason is None:
                self.ensure_day(now_us)
            self.fold(now_us)
            if self.port.lost.is_set():
                for day in self.journal.days:
                    if not self.journal.days[day].losses:
                        self.journal.loss(day)
            unknown = self.unknown
        if unknown is not None:
            day, pages, counter, closure = unknown
            try:
                if not self.store.reconcile([*pages, counter, *([closure] if closure else [])], budget):
                    return
                with self.lock:
                    self.journal.reconciled_commit(day)
                    if closure is None:
                        self.acknowledged(day, pages, None)
                    self.unknown = None
            except Exception:
                return
        if self.handoff is not None:
            day, pages, closure, result = self.handoff
            if self.store.reconcile([result], budget):
                with self.lock:
                    self.acknowledged(day, pages, closure)
                    self.handoff = None
            else:
                return
        with self.lock:
            days = sorted(self.counters)
        for day in days:
            with self.lock:
                if day in self.final_attempted:
                    continue
                state = self.journal.days[day]
                reason = self.closing_reason or ("utc_boundary" if day < today else None)
                if reason:
                    state.closing = True
                end_us = self.interval_end_us or now_us
                if reason == "utc_boundary":
                    import datetime as dt
                    end_us = int(dt.datetime.combine(dt.date.fromisoformat(day)+dt.timedelta(days=1), dt.time(), dt.UTC).timestamp()*1_000_000)
                with self.observer.lock:
                    self.fold(now_us)
                    observer_inflight = len(self.observer.inflight_ticks)
                pages = self.journal.freeze(day)
                closed = bool(reason and not self.bases.pending(day) and not self.traces.pending(day)
                    and self.journal.can_close(day, inflight=self.inflight + (day in self.publishing)
                    + observer_inflight,
                    pending=self.pending, interval_end_us=end_us, folded_us=self.folded_us))
                state.sealing = closed
                self.journal.checkpoint(day)
                counter = self.counter_row(day, now_us, closed=closed)
                closure = self.closure_row(day, counter, end_us, reason) if closed and reason else None
            started = self.store.clock()
            attempt_us = int(self.store.wall()*1_000_000)
            flush_remaining = max(0, int((deadline-started)*1_000_000))
            shutdown_remaining = (max(0, int((self.termination_deadline-started)*1_000_000))
                                  if self.termination_deadline is not None else None)
            if closure is not None:
                with self.lock:
                    self.final_attempted.add(day)
            try:
                self.store.flush(pages, counter, closure, budget)
                elapsed = max(0, round((self.store.clock()-started)*1_000_000))
                if closure is not None:
                    observed = int(self.store.wall()*1_000_000)
                    result = Row.build(CLOSE_RESULT_KIND, counter.key, dict(v=2, policy_version=POLICY,
                        contract_revision="a1", day=day, boot=self.journal.boot,
                        router_revision=self.settings.source_revision, closure_sha256=sha256(closure.value()),
                        final_checkpoint_seq=counter.value()["checkpoint_seq"], attempt_started_at_us=attempt_us,
                        observed_at_us=observed, physical_commit_ack_at_us=observed, elapsed_us=elapsed,
                        outcome="acknowledged", flush_remaining_us=flush_remaining,
                        shutdown_remaining_us=shutdown_remaining, handoff_attempt_limit=1))
                    validate_close_result(result.value(), closure.value(), termination_applies=self.termination_deadline is not None)
                    # Freeze only after the original reply. No readback path enters here.
                    self.last_closure_ack = result.value()
                    self.handoff = (day, pages, closure, result)
                    try:
                        self.store.close_result(result, budget)
                    except Exception:
                        return  # Exactly one write; later exact read can establish landing.
                    self.handoff = None
                with self.lock:
                    self.acknowledged(day, pages, closure)
                self.reconcile_work(budget)
            except Exception as error:
                with self.lock:
                    # An acknowledged close has already retired its day.
                    if day in self.journal.days:
                        uncertain = isinstance(error, UnknownCommit)
                        self.journal.failed(day, unknown=uncertain)
                        if not uncertain:
                            self.journal.days[day].sealing = False
                        if uncertain:
                            self.unknown = (day, pages, counter, closure)
                    if closure:
                        self.final_veto.add(day)
                        self.close_failures[day] = self.close_failures.get(day, 0) + 1
                        logger.warning("shadow_v2_final_close_failed day=%s boot=%s unknown=%s",
                                       day, self.journal.boot, isinstance(error, UnknownCommit))
                return

    def maintain(self) -> None:
        deadline = self.store.clock()+FLUSH_SECONDS
        now_us = int(self.store.wall()*1_000_000)
        with self.lock:
            if self.journal.stopped:
                return
            day = self.ensure_day(now_us)
            terminal = self.journal.terminal_permits.get(day, 0) < RESERVATION_BLOCK
            event = self.journal.permits.get(day, 0) < RESERVATION_BLOCK
            allocate = self.store.clock() >= self.next_allocate and (terminal or event)
            if allocate:
                self.next_allocate = self.store.clock()+5
        if allocate:
            try:
                self.store.allocate(day, terminal=terminal, event=event, budget=Budget(deadline, self.store.clock))
                with self.lock:
                    if event:
                        self.journal.grant(day, RESERVATION_BLOCK)
                    if terminal:
                        self.journal.terminal_permits[day] = self.journal.terminal_permits.get(day, 0)+RESERVATION_BLOCK
            except Exception:
                with self.lock:
                    self.counters[day]["diagnostics"]["cap_unavailable"] = self.counters[day]["diagnostics"].get("cap_unavailable", 0)+1
        self.flush(deadline)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.stopped.is_set() and self.enabled():
            now = self.store.clock()
            now_us = int(self.store.wall()*1_000_000)
            with self.lock:
                self.ensure_day(now_us)
            if self.contender():
                busy = self.publisher_job is not None and not self.publisher_job.done()
                slot, missed = self.slots.tick(now_us, busy=busy)
                with self.lock:
                    for missed_slot in range(now_us//SLOT_US - missed + (slot is None), now_us//SLOT_US + (slot is None)):
                        self.missed_slot(missed_slot)
                    if slot is not None:
                        self.schedule_cycle(slot, now_us)
                        try:
                            self.publisher_job = loop.run_in_executor(self.publisher_executor, self.publish_cycle, slot, now_us)
                        except RuntimeError:
                            self.missed_slot(slot, scheduled=True)
                            self.publishing.discard(day_at(now_us))
            if now >= self.next_flush and (self.flush_job is None or self.flush_job.done()):
                self.next_flush = now+5
                self.flush_job = loop.run_in_executor(self.evidence_executor, self.maintain)
            await asyncio.sleep(.01)
        if not self.stopped.is_set():
            await self.close("opt_out", from_loop=True)

    def trap_sigterm(self) -> None:
        """Chain the host's SIGTERM handler; start the grace clock at the signal.

        Uvicorn installs its callable handler before lifespan startup. Preserve
        its ingress/money shutdown behavior; P3 quiesces only its own port/jobs.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        previous = signal.getsignal(signal.SIGTERM)
        if not callable(previous):
            return
        loop = asyncio.get_running_loop()
        def stop(signum: int, frame: Any) -> None:
            deadline = self.store.clock()+9
            self.termination_deadline = min(self.termination_deadline or deadline, deadline)
            self.stopped.set()
            self.slots.stopped = self.journal.stopped = True
            if self.close_task is None:
                self.close_task = loop.create_task(self.close())
            previous(signum, frame)
        self.previous_signal_handler, self.signal_handler = previous, stop
        signal.signal(signal.SIGTERM, stop)

    def start(self) -> None:
        if self.enabled() and self.task is None:
            self.trap_sigterm()
            self.observer.start()
            self.task = asyncio.create_task(self.run())

    async def close(self, reason: str = "shutdown", *, from_loop: bool = False,
                    deadline: float | None = None) -> None:
        if self.close_finished:
            return
        if self.close_task is not None and self.close_task is not asyncio.current_task():
            await self.close_done.wait()
            return
        self.close_task = asyncio.current_task()
        deadline = min(deadline or self.store.clock()+9, self.termination_deadline or self.store.clock()+9)
        if reason == "shutdown":
            self.termination_deadline = deadline
        self.stopped.set()
        self.slots.stopped = True
        with self.lock:
            self.journal.stopped = True
            self.closing_reason = reason
            self.interval_end_us = int(self.store.wall()*1_000_000)
        try:
            async with asyncio.timeout(max(0, deadline-self.store.clock())):
                if self.task is not None and not from_loop:
                    await asyncio.shield(self.task)
                await self.observer.close()
                for job in (self.publisher_job, self.flush_job):
                    if job is not None:
                        await asyncio.shield(job)
                # Joining the reader executor is evidence-only; no money task is cancelled.
                if self.observer.executor is not None:
                    await asyncio.to_thread(self.observer.executor.shutdown, wait=True)
                # Let P3 reservations finish while preserving one full final-flush
                # budget. This never waits on, cancels or claims a money task.
                while self.store.clock() < deadline-FLUSH_SECONDS:
                    with self.lock:
                        owned = self.inflight or self.pending or any(
                            slot.body is None or not slot.completion_requested
                            for page in self.journal.pages for slot in page.slots)
                    if not owned:
                        break
                    await asyncio.sleep(.01)
                # At most three days; pages/reference reconciliation and closure
                # need separate passes. Leave unresolved writers open on no progress.
                for _ in range(6):
                    before = (len(self.journal.days), len(self.journal.pages), len(self.journal.batches), self.unknown is not None, self.handoff is not None)
                    await asyncio.shield(asyncio.get_running_loop().run_in_executor(
                        self.evidence_executor, self.flush, min(deadline, self.store.clock()+FLUSH_SECONDS)))
                    after = (len(self.journal.days), len(self.journal.pages), len(self.journal.batches), self.unknown is not None, self.handoff is not None)
                    if not self.journal.days:
                        break
                    if before == after:
                        lanes = [lane for lane in (self.bases, self.traces) if lane.queue and lane.transport is not None]
                        if not lanes:
                            break
                        wake = min(lane.next_operation for lane in lanes)
                        if wake >= deadline:
                            break
                        await asyncio.sleep(max(0, wake-self.store.clock()))
        except (TimeoutError, ValueError):
            self.unclosed = True
        finally:
            self.close_finished = True
            self.close_done.set()
            self.unclosed |= bool(self.journal.days)
            if self.signal_handler is not None and signal.getsignal(signal.SIGTERM) is self.signal_handler:
                signal.signal(signal.SIGTERM, self.previous_signal_handler)
            self.publisher_executor.shutdown(wait=False, cancel_futures=True)
            self.evidence_executor.shutdown(wait=False, cancel_futures=True)


def install(app: Any, settings: Settings, backend: Any) -> None:
    if (settings.async_settle_shadow_policy != POLICY or not settings.async_settle_shadow_workspace_ids
            or settings.async_settle_enabled or backend is None or not hasattr(backend, "_database")):
        return
    service = Service(settings, ObservationStore(backend._database))
    app.state.async_settle_shadow_observation = service
    @app.on_event("startup")
    async def start() -> None:
        service.start()
    @app.on_event("shutdown")
    async def stop() -> None:
        await service.close()
