"""Lifespan-owned v2 observation. No request handler installs or starts this service."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import math
import signal
import threading
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from time import sleep
from typing import Any, TypeVar, cast

from trusted_router import async_settle_shadow_lifecycle as lifecycle
from trusted_router.async_settle_shadow_attachments import Lane, Transport
from trusted_router.async_settle_shadow_evidence import HIST_BOUNDS, Counters
from trusted_router.async_settle_shadow_health_state import (
    CYCLE_SECONDS,
    JITTER_US,
    SLOT_US,
    Slots,
    health_basis,
    prediction,
    validate_startup_observation,
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
from trusted_router.async_settle_shadow_v2_a1_evidence import validate_close_result
from trusted_router.async_settle_shadow_v2_a1_health import (
    PUBLISHER_OPERATIONS,
    PUBLISHER_SLOT_TRACE,
    TRACE_RULES,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    CHECKPOINT_FIELDS,
    COUNTS,
    EVENT,
    RESERVATION_BLOCK,
)
from trusted_router.async_settle_shadow_v2_native import validate_bucket
from trusted_router.async_settle_shadow_v2_schema import INT64, POLICY, day_at, require, sha256
from trusted_router.config import Settings
from trusted_router.detached_jws import canonical
from trusted_router.services.async_settle_shadow_admission import Observer
from trusted_router.storage_gcp_async_admission import read_admission
from trusted_router.storage_gcp_shadow_attachments import GCSAttachments
from trusted_router.storage_gcp_shadow_io import (
    SessionCheckoutTimeout,
    SessionUnavailable,
    observe_spanner_rpcs,
    rpc_scope,
)
from trusted_router.storage_gcp_shadow_observation import (
    FLUSH_SECONDS,
    OBSERVER_READ_RPC_SECONDS,
    Budget,
    ContentionLoss,
    ObservationStore,
    UnknownCommit,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)
# Only code-produced release reasons may appear in logs. The evidence schema
# still accepts bounded free text, which is retained unchanged in the trace.
PUBLISHER_RELEASE_REASONS = frozenset({
    "invalid_event", "gone_day", "frozen_day", "unknown_slot", "invalid_start",
    "invalid_publisher_reply", "publisher_unknown_completion", "publisher_watchdog",
    "publisher_worker_busy", "publisher_worker_exception", "publisher_submit_failed",
})
# One slot lets a delayed owner callback arrive without extending any RPC budget.
PUBLISHER_WATCHDOG_GRACE = SLOT_US / 1_000_000


class _ObserverQuiesced(Exception):
    pass


class HealthObserver(Observer):
    """V2 owns queued outcomes until they are accounted and folded."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.owners: dict[str | None, tuple[str, int]] = {}
        self.outcome = type("Outcome", (), {"owner": None})()
        self.service: Service | None = None
        self.scheduled_owner: tuple[str, int] | None = None
        observer = self
        class Ticks(dict[str | None, float]):
            def __setitem__(self, key: str | None, value: float) -> None:
                require(observer.scheduled_owner is not None, "observer_schedule_owner")
                assert observer.scheduled_owner is not None
                owner = observer.scheduled_owner
                if observer.service is not None:
                    svc = observer.service
                    if not svc.state.days:
                        svc.tick(owner[1])
                    effects = svc.accept(lifecycle.ObserverEnqueued(owner[1], f"{key}:{value}"))
                    if any(isinstance(e, lifecycle.Fault) for e in effects):
                        raise _ObserverQuiesced
                observer.owners[key] = owner
                observer.scheduled_owner = None
                super().__setitem__(key, value)
        self.inflight_ticks = Ticks()

    def _count(self, field: str) -> None:
        owner = getattr(self.outcome, "owner", None)
        scheduled = field in ("health_reads", "workspace_reads")
        if scheduled:
            stamp = int(self.evidence_wall()*1_000_000)
            owner = self.scheduled_owner = (day_at(stamp), stamp)
            if self.service is not None:
                self.service.assert_owner()
                if any(d.day == owner[0] and (d.final or d.veto) for d in self.service.state.days):
                    raise _ObserverQuiesced
        if owner is None:
            super()._count(field)
            return
        day, stamp = owner
        self.counts[field] += 1
        bucket = (self._daily_bucket(stamp/1_000_000) if scheduled else
                  self.daily_counts.setdefault(day, dict(max_consecutive_failures=0,
                      degraded_seconds=0., started_at_us=stamp)))
        bucket[field] = bucket.get(field, 0) + 1

    def _owned(self, key: str | None, started: float, read: Callable[[], Any]) -> None:
        # The executor returns data only. Cache installs, evidence folding and
        # owner release all execute in finish on the event-loop thread.
        try:
            value, error = read(), None
        except Exception as caught:
            value, error = None, caught
        svc = self.service
        if svc is not None and svc.loop is not None:
            svc.loop.call_soon_threadsafe(self.finish, key, started, value, error)
        else:
            self.finish(key, started, value, error)

    def finish(self, key: str | None, started: float, value: Any, error: Exception | None) -> None:
        svc = self.service
        if svc is not None:
            svc.assert_owner()
        with self.lock:
            if self.inflight_ticks.get(key) != started:
                return
            self.outcome.owner = self.owners.get(key)
            if error is not None:
                self._failure(key, started, "read_failures")
            elif key is None:
                self.install_health(started, value)
            else:
                self.install_workspace(key, started, value)
            if self.inflight_ticks.get(key) == started:
                self.inflight_ticks.pop(key)
                owner = self.owners.pop(key, None)
                if owner is not None:
                    bucket = self.daily_counts[owner[0]]
                    bucket["max_consecutive_failures"] = max(bucket["max_consecutive_failures"], self.failures[key])
                    if svc is not None:
                        stamp = int(svc.store.wall()*1_000_000)
                        svc.fold(stamp)
                        svc.accept(lifecycle.ObserverFolded(stamp, owner[0], f"{key}:{started}"))
            self.outcome.owner = None

    def _health(self, started: float) -> None:
        self._owned(None, started, self.health_read)

    def _workspace(self, key: str, started: float) -> None:
        self._owned(key, started, partial(self.read, key))

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
                svc = self.service
                if svc is not None and svc.startup_retention and svc.startup_observation is None and value is not None:
                    try:
                        # This is the install producer, not a later cache snapshot.
                        basis = health_basis(self, svc.journal.boot, at=self.clock())
                        if basis["source_state"] != "installed":
                            return
                        raw = canonical(basis)
                        installed = int(svc.store.wall()*1_000_000)*1000
                        original = dict(health_basis_raw=raw, installed_at_ns=installed,
                                        contender=bool(svc.settings.async_settle_shadow_health_region)
                                        and svc.settings.async_settle_shadow_health_region
                                        == svc.settings.effective_serving_region)
                        if original["contender"]:
                            if svc.startup_publication is None:
                                return
                            original.update(svc.startup_publication)
                        validate_startup_observation(original)
                        if original["health_basis_raw"] and value["read_at_ns"] <= installed:
                            svc.startup_observation = original
                    except Exception:  # noqa: S110 - record collection cannot alter an install
                        pass


class StickyLoss:
    """One-way CPython boolean store; unlike Event.set(), takes no lock."""
    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


class Port:
    """Nonblocking request facade. No method calls storage or starts maintenance.

    Contention, saturation or invalid input returns None/False and poisons close.
    complete queues verification, not a durable acknowledgement. Only maintenance
    grants permits, writes retained batches, reads references and advances HWM.

    Lock inventory: every method tries service.lock; capture_prediction also
    tries observer.lock and bases.lock. Journal containers are plain bounded
    lists/dicts. No method touches the maintenance, trace-export, session or
    transport locks/queues. StickyLoss.set has no acquisition.
    """
    def __init__(self, service: Service) -> None:
        self.service = service
        self.lost = StickyLoss()
        self.fold_cells: dict[str, tuple[Any, ...]] = {}
        self.fold_obligations: dict[Reservation, str] = {}
        self.fold_owners: set[Reservation] = set()

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
            require(svc.policy_enabled() and not svc.journal.stopped, "quiesced")
            require(day in svc.counters, "day_not_ready")
            require(not svc.journal.days[day].sealing, "closed_day")
            if terminal:
                require(svc.journal.terminal_permits.get(day, 0) > 0, "terminal_permits")
                svc.journal.terminal_permits[day] -= 1
            return svc.journal.reserve(observed_us)
        return self.call(action)

    def finalize(self, reservation: Reservation, event: dict[str, Any], *, fold_owed: bool = False) -> bytes | None:
        def action() -> bytes:
            require(not self.service.journal.days[reservation.day].sealing, "closed_day")
            slot = self.service.journal.slot(reservation)
            register = fold_owed and not slot.fold_registered
            if register:
                require(not self.service.journal.stopped, "quiesced")
                require(not self.service.journal.days[reservation.day].closing, "closed_day")
                require(len(self.fold_obligations) < 32, "fold_capacity")
            body = self.service.journal.finalize(reservation, event)
            if register:
                self.fold_obligations[reservation] = "pending"
                slot.fold_registered = True
            return body
        return self.call(action)

    def complete(self, reservation: Reservation, rows: tuple[Row, ...] = ()) -> bool:
        def action() -> bool:
            require(not self.service.journal.days[reservation.day].sealing, "closed_day")
            return self.service.journal.complete(reservation, rows)
        return bool(self.call(action))

    def complete_event(self, reservation: Reservation, original_page: Row, event: dict[str, Any]) -> bytes | None:
        def action() -> bytes:
            require(not self.service.journal.days[reservation.day].sealing, "closed_day")
            return self.service.journal.complete_event(reservation, original_page, event)
        return self.call(action)

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
            result["basis_sha256"] = svc.bases.append(day, value, blocking=False)
            return result
        return self.call(action)

    def fold_counts(self, day: str, bucket: dict[str, Any], *, obligation: Reservation | None = None) -> bool:
        """Fold one P1 BUCKET delta atomically, including eligible demand.

        Unknown demand remains unknown; no cumulative field can be decremented.
        P4 supplies each attempt exactly once after its immutable result exists.
        """
        def action() -> bool:
            if obligation is not None:
                require(obligation.day == day and self.fold_obligations.get(obligation) == "pending", "fold_obligation")
            validate_bucket(bucket)
            svc = self.service
            require(not svc.journal.days[day].closed and not svc.journal.days[day].sealing, "closed_day")
            body = svc.counters[day]
            cached = self.fold_cells[day]
            require(cached[0] is body, "counter_identity")
            _, index, legacy_index, size = cached
            dims = bucket["dimensions"]
            key = (bucket["phase"], dims["adapter"], dims["route_type"], dims["streamed"])
            target = index.get(key)
            fresh = target is None
            if fresh:
                require(len(index) < 144, "evidence_size")
                target = dict(phase=bucket["phase"], dimensions=dict(dims),
                              **dict.fromkeys(COUNTS, 0), eligible_demand=0)
            legacy = legacy_index[(dims["adapter"] or "unknown", dims["route_type"] or "unknown", dims["streamed"])]
            updates = []
            for obj in (target, body):
                values = {k: obj[k]+bucket[k] for k in COUNTS}
                values["eligible_demand"] = (None if obj["eligible_demand"] is None or bucket["eligible_demand"] is None
                                             else obj["eligible_demand"]+bucket["eligible_demand"])
                updates.append((obj, values))
            values = {k: legacy[k]+bucket[k] for k in ("authorize_attempts", "authorize_fresh", "authorize_replay", "settle_attempts", "refund_attempts")}
            terminals, eligible = bucket["settle_attempts"]+bucket["refund_attempts"], bucket["eligible_demand"] or 0
            values.update(observed_attempts=legacy["observed_attempts"]+terminals,
                          observed_eligible=legacy["observed_eligible"]+eligible,
                          observed_ineligible=legacy["observed_ineligible"]+bucket["B"],
                          observed_unknown=legacy["observed_unknown"]+terminals-eligible-bucket["B"])
            updates.append((legacy, values))
            for obj, values in updates:
                for k, value in values.items():
                    require(value is None or 0 <= value <= INT64)
                    size += len(str(value)) if value is not None else 4
                    size -= len(str(obj[k])) if obj[k] is not None else 4
            if fresh:
                size += len(canonical(target)) + bool(index)
            require(size <= 65536, "evidence_size")
            # All checks precede mutation. Partition equations and bucket/root
            # sums are additive; None demand is absorbing. No decrement occurs.
            for obj, values in updates:
                obj.update(values)
            if fresh:
                body["buckets"].append(target)
                index[key] = target
            self.fold_cells[day] = (body, index, legacy_index, size)
            if obligation is not None:
                self.fold_obligations[obligation] = "folded"
            return True
        result = bool(self.call(action))
        if not result and obligation is not None:
            self.fail_fold(obligation)
        return result

    def fail_fold(self, obligation: Reservation) -> bool:
        """Non-waiting sticky failure; the owner folds it into the model."""
        self.lost.set()
        def action() -> bool:
            require(self.fold_obligations.get(obligation) == "pending", "fold_obligation")
            self.fold_obligations[obligation] = "failed"
            return True
        return bool(self.call(action))


@dataclass(frozen=True)
class WorkReply:
    value: Any = None
    error: Exception | None = None


def maintain(operation: Callable[[], Any]) -> WorkReply:
    """Evidence worker: evaluate captured I/O, returning data without State."""
    try:
        return WorkReply(operation())
    except Exception as error:
        return WorkReply(error=error)


@dataclass(frozen=True)
class PublisherReply:
    trace: dict[str, Any]
    rpc_durations: tuple[int, ...]
    claimed: bool
    generation: int = 0
    actual_start_mono: float | None = None
    reason: str | None = None
    originals: dict[str, Any] | None = None
    seen_claim_slot: int | None = None


def failed_publisher_reply(boot: str, job: lifecycle.PublisherJob,
                           reason: str = "publisher_unknown_completion") -> PublisherReply:
    """Unknown completion; no claim, RPC timing, or publication is inferred."""
    # Durable released-job signature, even when Python logs are unavailable:
    # cycle_us=0, rpc_count=0, unknown_outcome=true, claim_at_ns=null.
    # These are missing measurements, not proof that the worker did no I/O:
    # rejected replies and the watchdog also use this signature.
    trace = dict(v=2, policy_version=POLICY, contract_revision="a1",
        day=day_at(job.slot*SLOT_US), boot=boot, slot=job.slot,
        claim_start_us=job.slot*SLOT_US, claim_at_ns=None, observed_at_ns=None,
        publisher_heartbeat_ns=None, body_sha256=None, ack_at_us=None, cycle_us=0,
        outcome="failed", unknown_outcome=True, late_cycle=False, rpc_count=0, rpc_elapsed_us=0,
        release_reason=reason)
    return PublisherReply(trace, (), False, job.generation)


def follower_stagger(boot: str, slot: int, incumbent_present: bool = True) -> float:
    hashed = int.from_bytes(hashlib.sha256(f"{boot}:{slot}".encode()).digest()[:8], "big")
    # Healthy incumbent: server claim stamp B+3.6ms, observation B+54ms
    # (after claim commit), publication ack B+126ms. Discover the committed
    # claim at 60/90/120ms; waiting for publication is unnecessary. These
    # measurements justify a margin, not a bound on a contended claim commit.
    # Cold candidates retain their hash spread within the observation window.
    # Hashing reduces collisions; without a roster it cannot bound their count.
    return (60 + 30 * (hashed % 3) if incumbent_present else 30 * (hashed % 8)) / 1000


# Claim-row protocol (one row: CLAIM_KIND "local/publish"; one health row:
# HEALTH_KIND "local/fleet"). The semantics the publisher must keep:
#   S1 one winner per slot: a claim lands only over a claim for an earlier slot,
#      and a non-owner never replaces a claim newer than slot-2 (claim()).
#   S2 publication is fenced by the won claim of the same slot and boot, with
#      strictly increasing slots and observations (publish()).
#   S3 the observation lies in [boundary, boundary+250 ms] and after the claim.
#   S4 liveness: when the holder stops publishing, some follower claims within a
#      bounded number of slots.
#   S5 an aborted or lost claim is never retried within its slot.
# Only the holder may write the claim row every slot. A recent acknowledged
# publication is a fast path; otherwise storage must name this boot before it
# renews. K=3 permits recovery after two failed cycles, but a boot with three
# consecutive unpublished slots yields. A holder can recover even if failed
# claim reads left its row three slots old. Followers start at age four, so
# renewal and takeover windows never overlap, including unknown commits.
# Takeover uses 64 fixed hash buckets, one bucket per slot, without widening.
# For 8-20 boots this is 0.125-0.3125 candidates per slot in expectation, not a
# cardinality guarantee (there is no roster). Even a lone survivor gets a turn
# within 67 slots of the final claim. S1's transaction fence handles collisions;
# S5 forbids retry. A continuously failing holder yields after K, so S4 is
# bounded by K+TAKEOVER_MIN_AGE+TAKEOVER_SPREAD-1 slots after last publication.
HOLDER_FAILURE_SLOTS = 3
TAKEOVER_SPREAD = 64
TAKEOVER_MIN_AGE = HOLDER_FAILURE_SLOTS + 1


def takeover_rank(boot: str, claim_slot: int) -> int:
    hashed = hashlib.sha256(f"takeover:{boot}:{claim_slot}".encode()).digest()[:8]
    return int.from_bytes(hashed, "big") % TAKEOVER_SPREAD


def takeover_due(boot: str, slot: int, claim: dict[str, Any] | None) -> bool:
    """Warm followers take over a stale claim in hash-ranked waves, one per slot.

    Every follower reads the same stale claim, so ranks are fixed for the whole
    takeover. Rank r gets age TAKEOVER_MIN_AGE+r, repeating every spread
    slots. Missed turns never accumulate into a later fleet-wide wave (S4).
    """
    if claim is None:
        return True
    age = slot - claim["slot"] - TAKEOVER_MIN_AGE
    return age >= 0 and age % TAKEOVER_SPREAD == takeover_rank(boot, claim["slot"])


def own_publication(body: dict[str, Any] | None, boot: str, slot: int) -> bool:
    """The fleet health row is this boot's publication for slot-1 or slot-2."""
    return bool(body is not None and body.get("publisher_boot_id") == boot
                and body.get("slot") in (slot-1, slot-2))


def publish_cycle(store: ObservationStore, boot: str, region: str, revision: str,
                  job: lifecycle.PublisherJob, claim_us: int, admission: Service,
                  incumbent: bool = False, incumbent_present: bool = False,
                  warm: bool = False) -> PublisherReply:
    """Worker I/O only. All inputs are captured by the owner before submission.

    ``warm`` records whether this boot has installed fleet health. An existing
    claim always requires spread takeover, even for a newly started follower;
    only an absent claim row permits immediate cold election.
    """
    started = store.clock()
    slot = job.slot
    day = day_at(slot*SLOT_US)
    reason = None
    budget = Budget(job.latest_start_mono + CYCLE_SECONDS, store.clock)
    claimed = False
    attempted = False
    outcome = "missed_slots"
    unknown = False
    value = None
    ack_us = None
    rpc_durations: list[int] = []
    actual_us = claim_us
    seen_claim_slot = None
    store.last_claim = None
    def record(elapsed: int) -> None:
        rpc_durations.append(elapsed)
    def attempt() -> None:
        nonlocal actual_us, attempted, outcome, claimed, value, ack_us, started, budget, reason, seen_claim_slot
        if not store.session_ready():
            reason = "session_unavailable"
            return
        # Establish scopes before the cadence wait: setup time can fit inside
        # the mandatory two-second cadence instead of shifting every next start.
        # Admission still follows both setup and waiting, with revocation checks.
        with rpc_scope(budget.deadline, CYCLE_SECONDS, store.clock), observe_spanner_rpcs(record):
            delay = job.earliest_start_mono - store.clock()
            if delay > 0 and job.earliest_start_mono <= job.latest_start_mono:
                sleep(delay)
            if not admission.admission_lock.acquire(timeout=budget.remaining(CYCLE_SECONDS)):
                raise TimeoutError("publisher_admission")
            try:
                generation = admission.generation
                actual_us = int(store.wall()*1_000_000)
                started = store.clock()
                reason = ("revoked" if generation != job.generation or admission.revocation_epoch
                    or admission.admission_job != job else
                    "outside_window" if not (job.earliest_start_mono <= started <= job.latest_start_mono
                        and actual_us // SLOT_US == slot and actual_us % SLOT_US <= JITTER_US) else None)
                if reason is not None:
                    return
                budget = Budget(started + CYCLE_SECONDS, store.clock)
            finally:
                admission.admission_lock.release()
            # Only a won claim or an unknown commit skips the follower delay.
            # A definite loser remains a follower; neither hint proves ownership.
            previous_attempt = store.claim_attempt if store.claim_unknown else store.won
            # A known abort can retain the recent-publication fast path. Only
            # an unknown commit needs storage reconciliation before that path.
            fast_incumbent = incumbent and not store.claim_unknown
            holder = (not fast_incumbent and previous_attempt is not None
                      and previous_attempt[0] == boot and slot-HOLDER_FAILURE_SLOTS <= previous_attempt[1] < slot)
            if not fast_incumbent and not holder:
                # A warm takeover is already spread across slots. Keep its
                # follower delay in the 60-120ms band; the cold 0-210ms spread
                # can otherwise consume the observation window before claiming.
                stagger = follower_stagger(boot, slot, incumbent_present or warm)
                sleep(stagger)
                # The admitted start and budget include the stagger. Recheck the
                # owner before any I/O; a retired job remains a missed slot.
                if not admission.admission_lock.acquire(timeout=budget.remaining(CYCLE_SECONDS)):
                    raise TimeoutError("publisher_admission")
                try:
                    if (admission.generation != job.generation or admission.revocation_epoch
                            or admission.admission_job != job):
                        reason = "revoked"
                        return
                    if (int(store.wall()*1_000_000) // SLOT_US != slot
                            or not incumbent_present and store.clock() > job.latest_start_mono):
                        reason = "outside_window"
                        return
                finally:
                    admission.admission_lock.release()
            if not store.session_ready():
                reason = "session_unavailable"
                return
            attempted, outcome = True, "failures"
            if not fast_incumbent:
                current = store.read_shadow_claim(budget)
                seen_claim_slot = current["slot"] if current is not None else None
                # Followers pay one lock-free read and no Commit RPC. A local
                # attempt/won hint is never authority, especially after timeout.
                owner = current is not None and current["owner_boot_id"] == boot
                renew = False
                if owner and current is not None and slot-HOLDER_FAILURE_SLOTS <= current["slot"] < slot:
                    publication = store.read_shadow_publication(budget)
                    published = store.last_publication
                    if publication is not None and publication["publisher_boot_id"] == boot:
                        if published is None or published[0] != boot or publication["slot"] > published[1]:
                            published = store.last_publication = (boot, publication["slot"])
                    # Publication evidence outranks any claim/tenure anchor:
                    # missed slots consume the allowance too. Only a boot with
                    # no publication uses the start of its initial claim tenure.
                    since = (published[1] + 1 if published is not None and published[0] == boot
                             else store.unpublished_since)
                    if since is None:
                        since = current["slot"]
                    store.unpublished_since = since
                    renew = slot < since + HOLDER_FAILURE_SLOTS and store.clock() <= job.latest_start_mono
                if not renew and (owner or incumbent_present
                        or current is not None and current["slot"] > slot-2
                        or (warm or current is not None) and not takeover_due(boot, slot, current)
                        or store.clock() > job.latest_start_mono):
                    outcome = "lost_claims"
                    return
            if not store.claim(boot, slot, actual_us, budget):
                outcome = "lost_claims"
                return
            claimed = True
            assert store.last_claim is not None
            value = store.observe(boot, region,
                revision, slot, budget, claim_at_ns=store.last_claim["claim_at_ns"])
            if not store.publish(value, budget):
                return
            budget.remaining(CYCLE_SECONDS)
            ack_us = int(store.wall()*1_000_000)
            outcome = "publications"
    try:
        attempt()
    except Exception as error:
        if isinstance(error, (SessionUnavailable, SessionCheckoutTimeout)) and not rpc_durations:
            attempted, outcome, reason = False, "missed_slots", "session_unavailable"
        unknown = isinstance(error, UnknownCommit)
        if isinstance(error, ContentionLoss) and not claimed:
            outcome = "lost_claims"
    elapsed = max(0, round((store.clock()-started)*1_000_000))
    late = attempted and elapsed > round(CYCLE_SECONDS*1_000_000)
    if late:
        outcome = "failures"
    claim = store.last_claim
    observation_failure = (value is not None and claim is not None
                           and value["observed_at_ns"] < claim["claim_at_ns"])
    if observation_failure:
        # A refused observation is not valid trace evidence. Preserve the real
        # claim and measurements; the owner/lifecycle accounts an ordinary failure.
        value, ack_us, outcome = None, None, "failures"
    trace = dict(v=2, policy_version=POLICY, contract_revision="a1", day=day, boot=boot,
        slot=slot, claim_start_us=actual_us if attempted else None,
        claim_at_ns=claim["claim_at_ns"] if claim else None,
        observed_at_ns=value["observed_at_ns"] if value else None,
        publisher_heartbeat_ns=value["publisher_heartbeat_ns"] if value else None,
        body_sha256=sha256(value) if value else None, ack_at_us=ack_us,
        cycle_us=elapsed if attempted else None,
        outcome={"missed_slots": "missed", "lost_claims": "lost_claim", "publications": "published", "failures": "failed"}[outcome],
        unknown_outcome=unknown, late_cycle=late, rpc_count=len(rpc_durations), rpc_elapsed_us=sum(rpc_durations))
    if observation_failure:
        trace["observation_failure"] = "claim_observed_order"
    originals = None
    if admission.startup_retention and outcome == "publications" and claim is not None:
        claim_raw = store.startup_claim_raw
        publication_raw = store.startup_publication_raw
        if claim_raw is not None and publication_raw is not None and value is not None:
            interval_ns = value["observed_at_ns"] - claim["claim_at_ns"]
            if type(interval_ns) is not int or not 0 <= interval_ns <= 500_000_000 or interval_ns % 1000:
                return PublisherReply(trace, tuple(rpc_durations), claimed, job.generation, started, reason, None)
            originals = dict(claim_raw=claim_raw, publication_raw=publication_raw,
                publication_elapsed_us=interval_ns // 1000, claim_at_ns=claim["claim_at_ns"],
                published_at_ns=value["observed_at_ns"] if value else None)
    return PublisherReply(trace, tuple(rpc_durations), claimed, job.generation, started, reason, originals,
                          seen_claim_slot)


class Service:
    def __init__(self, settings: Settings, store: ObservationStore, transport: Transport | None = None) -> None:
        self.settings, self.store = settings, store
        self.startup_retention = False
        self.startup_observation: dict[str, Any] | None = None
        self.startup_publication: dict[str, Any] | None = None
        self.startup_close: Callable[..., None] | None = None
        self.startup_preparation_started: int | None = None
        self.startup_close_phases: dict[str, tuple[int, int]] = {}
        self.workspaces = settings.async_settle_shadow_workspace_ids
        self.legacy = Counters(settings.effective_serving_region, settings.source_revision)
        self.journal = Journal(self.legacy.instance)
        if transport is None and settings.async_settle_shadow_attachment_bucket:
            transport = GCSAttachments(settings.async_settle_shadow_attachment_bucket)
        self.bases = Lane("basis", self.journal.boot, transport)
        self.traces = Lane("trace", self.journal.boot, transport)
        self.basis_tokens, self.basis_tick = 10., store.clock()
        self.observer = HealthObserver(self.workspaces, partial(read_admission, store.workspace_database,
            deadline_seconds=OBSERVER_READ_RPC_SECONDS), store.read_shadow_health,
            cap=settings.async_settle_pilot_cap_micro, enabled=self.enabled, policy=POLICY,
            clock=store.clock, wall=store.wall)
        cast(Any, self.observer).lock = threading.RLock()
        self.observer.service = self
        self.lock = threading.RLock()
        self.owner_thread = threading.get_ident()
        # Workers linearize ordinary generation admission under this lock.
        self.admission_lock = threading.RLock()
        self.generation = 0
        self.admission_job: lifecycle.PublisherJob | None = None
        self.incumbent_slot: int | None = None
        self.seen_claim_slot: int | None = None
        self.fleet_seen = False
        # Terminal epoch: every publisher job is issued in epoch zero. Once a
        # signal revokes it, no later owner transition can reopen admission.
        self.revocation_epoch = 0
        self.signal_stamp: float | None = None
        self.policy_pin = self.pin()
        self.publisher_faults = 0
        self._state = lifecycle.State()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port = Port(self)
        self.close_failures: dict[str, int] = {}
        self.final_attempted: set[str] = set()
        self.final_plans: dict[str, tuple[list[Row], Row, Row]] = {}
        self.final_veto: set[str] = set()
        self.handoff: tuple[str, list[Row], Row, Row] | None = None
        self.publishing: set[str] = set()
        self.slots = Slots()
        self.last_claim_start = float("-inf")
        self.stopped = asyncio.Event()
        self.evidence_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-evidence")
        self.session_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-session")
        self.session_job: Future[bool] | None = None
        self.publisher_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-health")
        self.task: asyncio.Task[None] | None = None
        self.publisher_job: asyncio.Future[Any] | None = None
        self.publisher_worker: Future[None] | None = None
        self.publisher_worker_active = threading.Event()
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

    def pin(self) -> tuple[Any, ...]:
        # The effective policy includes the canary expiry: when it passes, the
        # pin changes, the generation is revoked and run() closes as opt_out.
        return (self.settings.effective_async_settle_shadow_policy(self.store.wall()),
                self.settings.async_settle_shadow_workspace_ids,
                self.settings.async_settle_enabled, self.settings.async_settle_shadow_health_region,
                self.settings.effective_serving_region)

    def enabled(self) -> bool:
        if threading.get_ident() == self.owner_thread and self.policy_pin != self.pin():
            self.policy_pin = self.pin()
            with self.admission_lock:
                self.generation += 1
            self._state, _ = lifecycle.step(self.state, lifecycle.PolicyChanged(0, self.generation))
        return self.policy_enabled()

    def policy_enabled(self) -> bool:
        """Request-side pin check: owner lifecycle alone publishes revocations."""
        return (self.settings.effective_async_settle_shadow_policy(self.store.wall()) == POLICY
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
            # Prepare fixed projections off the request path, once per day.
            legacy_index = {(v["adapter"], v["route_type"], v["streamed"]): v for v in base["counts"]}
            self.port.fold_cells[day] = (base, {}, legacy_index, len(canonical(base)))
        return day

    def _counter_cells(self, day: str, target: Any, values: dict[Any, int]) -> None:
        """Keep the fold byte budget coherent when maintenance changes cells."""
        with self.lock:
            body, index, legacy, size = self.port.fold_cells[day]
            for key, value in values.items():
                if isinstance(target, dict) and key not in target:
                    size += len(canonical(key))+1+bool(target)+len(str(value))
                else:
                    size += len(str(value))-len(str(target[key]))
                target[key] = value
            self.port.fold_cells[day] = (body, index, legacy, size)

    def operation(self, day: str, name: str, count: int = 1) -> None:
        with self.lock:
            target = self.counters[day]["publisher"]
            self._counter_cells(day, target, {name: target[name]+count})

    def tick(self, stamp: int, *, publisher: bool | None = None) -> tuple[lifecycle.Effect, ...]:
        self.assert_owner()
        self.enabled()  # Publish any changed admission generation before dispatch.
        effects: tuple[lifecycle.Effect, ...] = ()
        job = self.state.job
        if job is not None and self.store.clock() > job.latest_start_mono+CYCLE_SECONDS+PUBLISHER_WATCHDOG_GRACE:
            effects = self.accept(lifecycle.PublisherFailed(stamp, job.slot, job.generation, "publisher_watchdog"))
        if not self.state.days:
            enabled = self.contender() if publisher is None else publisher
            days = []
            for day in self.counters:
                start = stamp if day == day_at(stamp) else self.counters[day]["started_at_us"]
                slot = start//SLOT_US
                days.append(lifecycle.Day(day, slot, slot-1, (start//lifecycle.DAY_US+1)*lifecycle.DAY_US//SLOT_US-1))
            self._state = lifecycle.State(days=tuple(days), publisher_enabled=enabled, generation=self.generation)
        return effects + self.accept(lifecycle.Tick(stamp, self.store.clock(), self.enabled() and not self.stopped.is_set()
                                                     and (publisher is True or self.contender())))

    def schedule_cycle(self, slot: int, claim_us: int) -> None:
        self.assert_owner()
        self.tick(claim_us, publisher=True)
        if any(slot in d.publishers for d in self.state.days):
            self.publishing.add(day_at(claim_us))

    def publish_cycle(self, slot: int, claim_us: int) -> None:
        """Offline owner driver; executors call the module-level worker."""
        self.assert_owner()
        self.schedule_cycle(slot, claim_us)
        job = self.state.job
        if job is None or job.slot != slot:
            return
        reply = publish_cycle(self.store, self.journal.boot, self.settings.effective_serving_region,
            self.settings.source_revision, job, claim_us, self, self.is_incumbent(slot), self.has_incumbent(slot),
            self.warm())
        self.publisher_reply(reply)

    def is_incumbent(self, slot: int) -> bool:
        # An acknowledged incumbent runs immediately on consecutive slots,
        # without follower discovery or re-election. S1 and RPC budgets remain
        # unchanged; the storage fence still permits only one winner per slot.
        self.assert_owner()
        if self.incumbent_slot in (slot-1, slot-2):
            return True
        # Durable incumbency: the installed fleet health is this boot's own
        # publication, which publish() wrote only under this boot's won claim
        # (S2). The owner may have rejected that reply; storage still names the
        # holder, and treating it as a follower let the claim age out.
        return own_publication(self._installed_health_body(), self.journal.boot, slot)

    def _installed_health_body(self) -> dict[str, Any] | None:
        health = self.observer.health
        body = health.get("body") if isinstance(health, dict) else None
        return body if isinstance(body, dict) else None

    def warm(self) -> bool:
        """This boot has installed fleet health, so a stale claim is a takeover."""
        self.assert_owner()
        self.fleet_seen = self.fleet_seen or self._installed_health_body() is not None
        return self.fleet_seen

    def has_incumbent(self, slot: int) -> bool:
        """Recent health or a fresh claim snapshot protects the incumbent."""
        self.assert_owner()
        health = self.observer.health
        return (self.is_incumbent(slot) or self.seen_claim_slot == slot-1) or bool(health is not None
            and health["body"]["slot"] in (slot-1, slot-2))

    def publisher_reply(self, reply: PublisherReply, *,
                        job: lifecycle.PublisherJob | None = None) -> tuple[lifecycle.Effect, ...]:
        self.assert_owner()
        current = self.state.job
        def invalid(group: str, error: Exception | None = None, *, release: bool = True) -> tuple[lifecycle.Effect, ...]:
            # This diagnostic includes only field/type labels, a fixed group
            # and rule, and an allowlisted outcome. No reply measurements or
            # arbitrary field names/messages are interpolated.
            fields = PUBLISHER_SLOT_TRACE.fields.keys() | PUBLISHER_SLOT_TRACE.optional_fields.keys()
            value = reply.trace if isinstance(reply, PublisherReply) and isinstance(reply.trace, dict) else {}
            message = str(error) if error is not None else group + "_checks"
            safe_messages = {"evidence_schema_gap", "native-json-domain", "a1-server-slot",
                "evidence_size", "integer", "json_encoding", "json_shape", "json_depth", "duplicate_key"} | TRACE_RULES
            if error is not None and message not in safe_messages:
                message = (repr(error.args[0]) if isinstance(error, KeyError) and error.args
                    and type(error.args[0]) is str and error.args[0] in fields else "redacted_non_schema_message")
            field_types = {key: type(value[key]).__name__ if key in value else "missing"
                           for key in sorted(fields)}
            # Extra field names can themselves contain payloads. Report every
            # extra field's key/value TYPES, with an ordinal instead of its name.
            extras = [(type(key).__name__, type(item).__name__)
                      for key, item in value.items() if key not in fields]
            outcome = value.get("outcome")
            outcome = outcome if type(outcome) is str and outcome in (
                "missed", "lost_claim", "published", "failed") else "invalid_or_missing"
            durations = reply.rpc_durations if isinstance(reply, PublisherReply) else None
            logger.warning("shadow_v2_publisher_reply_rejected group=%s exception_type=%s "
                "exception_message=%s outcome=%s trace_types=%s extra_field_types=%s "
                "reply_type=%s trace_type=%s claimed_type=%s generation_type=%s "
                "rpc_durations_type=%s rpc_duration_element_types=%s",
                group, type(error).__name__ if error is not None else "none", message,
                outcome, field_types, extras, type(reply).__name__,
                type(reply.trace).__name__ if isinstance(reply, PublisherReply) else "missing",
                type(reply.claimed).__name__ if isinstance(reply, PublisherReply) else "missing",
                type(reply.generation).__name__ if isinstance(reply, PublisherReply) else "missing",
                type(durations).__name__,
                [type(item).__name__ for item in durations] if isinstance(durations, (tuple, list)) else [])
            if release and current is not None:
                return self.accept(lifecycle.PublisherFailed(int(self.store.wall()*1_000_000),
                    current.slot, current.generation, "invalid_publisher_reply"))
            self.publisher_faults += 1
            return (lifecycle.Fault("invalid_publisher_reply"),)
        # The dispatch identity survives even a completely malformed result.
        # A late callback must never complete the replacement job.
        if job is not None and job != current:
            return invalid("identity", release=False)
        job = current
        if (isinstance(reply, PublisherReply) and isinstance(reply.trace, dict)
                and type(reply.trace.get("slot")) is int and type(reply.generation) is int
                and (job is None or (reply.trace["slot"], reply.generation) != (job.slot, job.generation))):
            return invalid("identity", release=False)
        if (not isinstance(reply, PublisherReply) or not isinstance(reply.trace, dict)
                or type(reply.generation) is not int or type(reply.claimed) is not bool
                or not isinstance(reply.rpc_durations, tuple)
                or any(type(value) is not int or value < 0 for value in reply.rpc_durations)):
            return invalid("type")
        trace = reply.trace
        # Validate the entire immutable identity before counters or ownership.
        if (job is None or trace.get("slot") != job.slot or reply.generation != job.generation
                or trace.get("day") != day_at(job.slot*SLOT_US)):
            return invalid("identity")
        day, slot = day_at(job.slot*SLOT_US), job.slot
        try:
            PUBLISHER_SLOT_TRACE(trace)
        except (ValueError, TypeError, KeyError) as error:
            return invalid("contract", error)
        if trace["outcome"] == "failed" and reply.actual_start_mono is None:
            # Unknown worker/submission failures have no observed start. The
            # release transition owns their trace and accounting exactly once.
            return self.accept(lifecycle.PublisherFailed(int(self.store.wall()*1_000_000),
                slot, reply.generation, trace.get("release_reason", "publisher_unknown_completion")))
        effects = self.accept(lifecycle.SlotOutcome(int(self.store.wall()*1_000_000), slot,
            trace["outcome"], generation=reply.generation, actual_start_mono=reply.actual_start_mono), trace=trace)
        if any(isinstance(e, lifecycle.Fault) for e in effects):
            return effects
        # One failed cycle does not revoke a recently acknowledged incumbent.
        # is_incumbent bounds this grace to two slots; stale callbacks never get
        # here, and followers cannot promote themselves from a claim alone.
        if trace["outcome"] == "published":
            self.incumbent_slot = slot
        self.seen_claim_slot = reply.seen_claim_slot
        if reply.originals is not None and trace["outcome"] == "published":
            self.startup_publication = reply.originals
        if trace["outcome"] != "missed" and reply.actual_start_mono is not None:
            self.last_claim_start = reply.actual_start_mono
        with self.lock:
            for elapsed in reply.rpc_durations:
                self.histogram(day, "rpc_us", elapsed)
            if trace["cycle_us"] is not None:
                self.histogram(day, "cycle_us", trace["cycle_us"])
            for name, count in (("claims", reply.claimed), ("scans", reply.claimed),
                                ("unknown_outcomes", trace["unknown_outcome"]), ("late_cycles", trace["late_cycle"])):
                self.operation(day, name, int(count))
            self.publishing.discard(day)
        return effects

    def missed_slot(self, slot: int, *, scheduled: bool = False) -> None:
        if not scheduled:
            self.tick(slot*SLOT_US+JITTER_US+1, publisher=True)
            return
        day = self.ensure_day(slot*SLOT_US)
        trace = dict(v=2, policy_version=POLICY, contract_revision="a1", day=day, boot=self.journal.boot,
            slot=slot, claim_start_us=None, claim_at_ns=None, observed_at_ns=None,
            publisher_heartbeat_ns=None, ack_at_us=None, cycle_us=None, body_sha256=None,
            outcome="missed", unknown_outcome=False, late_cycle=False, rpc_count=0, rpc_elapsed_us=0)
        try:
            PUBLISHER_SLOT_TRACE(trace)
            self.traces.append(day, trace)
        except ValueError:
            self.journal.loss(day)
            self.accept(lifecycle.TraceLost(slot*SLOT_US, day, slot))

    def histogram(self, day: str, name: str, elapsed: int) -> None:
        with self.lock:
            histogram = self.counters[day]["publisher"][name]
            index = sum(elapsed > bound for bound in histogram["bounds_us"])
            self._counter_cells(day, histogram["counts"], {index: histogram["counts"][index]+1})

    def fold(self, now_us: int) -> None:
        daily, overflow, _ = self.observer.snapshot_daily_counts()
        for day, values in daily.items():
            if day <= self.journal.retired_through:
                for current in self.journal.days:
                    self.journal.loss(current)
                continue
            if day not in self.counters:
                if self.state.days and not any(d.day == day for d in self.state.days):
                    for current in self.journal.days:
                        self.journal.loss(current)
                    continue
                self.ensure_day(int(values["started_at_us"]))
            if any(d.day == day and d.final for d in self.state.days):
                continue
            counter = self.counters[day]
            for key, value in values.items():
                if key in counter["admission_observer"]:
                    self._counter_cells(day, counter["admission_observer"],
                        {key: max(counter["admission_observer"][key], math.ceil(value))})
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
            self.bases.retire(day)
            self.traces.retire(day)
            self.journal.closed(day)
            self.final_attempted.discard(day)
            self.final_plans.pop(day, None)
            self.counters.pop(day)
            self.port.fold_cells.pop(day, None)
            self.legacy.days.pop(day, None)
            self.observer.acknowledge_daily_counts(day, day_at(self.folded_us))

    def reconcile_work(self, budget: Budget) -> Generator[Callable[[], Any], Any, None]:
        self.assert_owner()
        with self.lock:
            batches = list(self.journal.batches.items())
        cache: dict[tuple[str, str], Row | None] = {}
        def read(kind: str, key: str) -> Generator[Callable[[], Any], Any, Row | None]:
            identity = (kind, key)
            if identity in cache:
                return cache[identity]
            value = yield partial(self.store.read, kind, key, budget)
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
                        (yield partial(self.store.retain, rows, budget))
                    except UnknownCommit:
                        with self.lock:
                            self.unknown_batches.add(reservation)
                            self.journal.failed(reservation.day, unknown=True)
                        return
                page = yield from read(PAGE_KIND, f"{reservation.day}/{reservation.boot}/{reservation.first_seq:020}")
                if page is None:
                    continue
                durable = [page]
                for row in rows:
                    found = yield from read(row.kind, row.key)
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
                        found = yield from read(original.kind, original.key)
                        if found != original:
                            continue
                        durable.append(found)
                    if event.get("proof_kind") in ("compared", "observed_binding", "certificate"):
                        kind = CERTIFICATE_KIND if event["proof_kind"] == "certificate" else SAMPLE_KIND
                        key = event["authorization_day"]+"/"+event["authorization_id"]
                        if not any(r.kind == kind and r.key == key for r in durable):
                            found = yield from read(kind, key)
                            if found is not None:
                                durable.append(found)
                    if event.get("original_receipt") is not None:
                        locator = event["original_receipt"]
                        key = f'{locator["day"]}/{locator["boot"]}/{locator["first_seq"]}'
                        found = yield from read(PAGE_KIND, key)
                        if found is not None:
                            durable.append(found)
                    if reservation in self.unknown_batches and not (yield partial(self.store.reconcile,
                            list(rows), budget, writer_key=reservation.day+"/"+reservation.boot)):
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

    @property
    def _state(self) -> lifecycle.State:
        self.assert_owner()
        return self.__state

    @_state.setter
    def _state(self, value: lifecycle.State) -> None:
        self.assert_owner()
        self.__state = value

    @property
    def state(self) -> lifecycle.State:
        self.assert_owner()
        return self._state

    def assert_owner(self) -> None:
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("lifecycle_off_owner_thread")

    def accept(self, event: lifecycle.Event, *, trace: dict[str, Any] | None = None) -> tuple[lifecycle.Effect, ...]:
        self.assert_owner()
        with self.lock:
            before = self.state
            self._state, effects = lifecycle.step(before, event)
            if isinstance(event, (lifecycle.SlotOutcome, lifecycle.PublisherFailed)):
                self.publisher_faults += sum(isinstance(e, lifecycle.Fault) for e in effects)
            if before.job is not None and self.state.job is None:
                for effect in effects:
                    if isinstance(effect, lifecycle.Fault):
                        logger.warning("shadow_v2_publisher_released slot=%s generation=%s reason=%s",
                                       before.job.slot, before.job.generation,
                                       effect.reason if effect.reason in PUBLISHER_RELEASE_REASONS else "other")
                self.publishing.discard(day_at(before.job.slot*SLOT_US))
                if self.publisher_job is not None and not self.publisher_job.done():
                    self.publisher_job.set_result(None)
                if isinstance(event, lifecycle.PublisherFailed) and self.publisher_worker is not None:
                    self.publisher_worker.cancel()  # Only queued work can be cancelled.
            with self.admission_lock:
                self.generation = self.state.generation
                self.admission_job = self.state.job
            if isinstance(event, lifecycle.PublisherFailed) and before.job is not None and self.state.job is None:
                self.incumbent_slot = None
            for effect in effects:
                if isinstance(effect, lifecycle.StartFinalClose):
                    self.journal.days[effect.day].sealing = True
            for d in self.state.days:
                if d.closed or d.day <= self.journal.retired_through:
                    continue
                self.ensure_day(d.first_slot*SLOT_US)
                values = self.counters[d.day]["publisher"]
                self._counter_cells(d.day, values, dict(scheduled_slots=d.scheduled+len(d.publishers), missed_slots=d.missed,
                    claim_attempts=d.attempted, lost_claims=d.lost, publications=d.published, failures=d.failed))
            for effect in effects:
                if isinstance(effect, lifecycle.EmitTrace):
                    emitted = trace if trace is not None and effect.slot == trace["slot"] else None
                    if effect.unknown_outcome:
                        assert before.job is not None
                        reason = next(e.reason for e in effects if isinstance(e, lifecycle.Fault))
                        emitted = failed_publisher_reply(self.journal.boot, before.job, reason).trace
                        day = day_at(effect.slot*SLOT_US)
                        self.operation(day, "unknown_outcomes")
                        self.histogram(day, "cycle_us", 0)
                    if emitted is not None:
                        day = day_at(effect.slot*SLOT_US)
                        try:
                            self.traces.append(day, emitted)
                        except ValueError:
                            self.journal.loss(day)
                            self.accept(lifecycle.TraceLost(event.stamp, day, effect.slot))
                    else:
                        self.missed_slot(effect.slot, scheduled=True)
            for effect in effects:
                if isinstance(effect, lifecycle.StartFinalClose):
                    day = effect.day
                    reason = self.closing_reason or ("shutdown" if self.state.shutdown else "utc_boundary")
                    end_us = self.interval_end_us or event.stamp
                    if reason == "utc_boundary":
                        end_us = (next(d.last_slot for d in self.state.days if d.day == day)+1)*SLOT_US
                    self.journal.checkpoint(day)
                    counter = self.counter_row(day, event.stamp, closed=True)
                    closure = self.closure_row(day, counter, end_us, reason)
                    self.final_plans[day] = (self.journal.freeze(day), counter, closure)
        return effects

    def drive(self, work: Generator[Callable[[], Any], Any, None]) -> None:
        self.assert_owner()
        value = None
        error = None
        while True:
            try:
                operation = work.throw(error) if error is not None else work.send(value)
            except StopIteration:
                return
            try:
                value, error = operation(), None
            except Exception as caught:
                value, error = None, caught

    async def drive_async(self, work: Generator[Callable[[], Any], Any, None]) -> None:
        self.assert_owner()
        loop = asyncio.get_running_loop()
        value = None
        error = None
        while True:
            try:
                operation = work.throw(error) if error is not None else work.send(value)
            except StopIteration:
                return
            reply = loop.create_future()
            def deliver(result: Any, caught: Exception | None, reply: Any = reply) -> None:
                if not reply.done():
                    reply.set_result((result, caught))
            def execute(operation: Any = operation, deliver: Any = deliver) -> None:
                result = maintain(operation)
                loop.call_soon_threadsafe(deliver, result.value, result.error)
            self.evidence_executor.submit(execute)
            value, error = await reply

    def flush(self, deadline: float) -> None:
        self.drive(self._flush(min(deadline, self.termination_deadline or deadline)))

    def upload(self, lane: Lane, budget: Budget) -> Generator[Callable[[], Any], Any, None]:
        self.assert_owner()
        with self.lock:
            objects = tuple(lane.queue)
            for item in objects:
                d = next((d for d in self.state.days if d.day == item.day), None)
                if d is not None and item.key not in d.attachments:
                    self.accept(lifecycle.AttachmentAdmitted(int(self.store.wall()*1_000_000), item.key, item.day))
        if objects and not any(objects[0].key in d.attachments for d in self.state.days):
            return  # No UploadAttachment effect was admitted for this object.
        operations = lane.operations["put"]+lane.operations["get"]
        yield from lane.work(budget)
        if objects and operations != lane.operations["put"]+lane.operations["get"] and objects[0] in lane.queue:
            self.accept(lifecycle.AttachmentSettled(int(self.store.wall()*1_000_000),
                objects[0].day, objects[0].key, success=False))
        with self.lock:
            for item in objects:
                if item not in lane.queue:
                    d = next(d for d in self.state.days if d.day == item.day)
                    drained = tuple(e["slot"] for e in lane.schema.parse(item.body)["entries"]) if lane.stream == "trace" else ()
                    self.accept(lifecycle.AttachmentSettled(int(self.store.wall()*1_000_000), item.day, item.key, drained))

    def sync_fold_obligations(self) -> None:
        """Owner-thread bridge; workers only mutate bounded Port records."""
        self.assert_owner()
        with self.lock:
            if not self.port.fold_obligations:
                return
            stamp = int(self.store.wall()*1_000_000)
            if not self.state.days:
                self.tick(stamp, publisher=False)
            for reservation, status in list(self.port.fold_obligations.items()):
                key = f"fold:{reservation.boot}:{reservation.event_seq}"
                if reservation not in self.port.fold_owners:
                    effects = self.accept(lifecycle.ObserverEnqueued(stamp, key, reservation.day))
                    if any(isinstance(e, lifecycle.Fault) for e in effects):
                        self.port.lost.set()
                        continue
                    self.port.fold_owners.add(reservation)
                if status != "pending":
                    effects = self.accept(lifecycle.ObserverFolded(stamp, reservation.day, key, status == "folded"))
                    if any(isinstance(e, lifecycle.Fault) for e in effects):
                        self.port.lost.set()
                        continue
                    del self.port.fold_obligations[reservation]
                    self.port.fold_owners.remove(reservation)

    def _flush(self, deadline: float, *, finals_only: bool = False) -> Generator[Callable[[], Any], Any, None]:
        self.assert_owner()
        preparation_started = round(self.store.clock()*1_000_000) if self.startup_close is not None else 0
        budget = Budget(deadline, self.store.clock)
        self.sync_fold_obligations()
        if self.closing_reason is not None and not self.state.shutdown:
            if self.interval_end_us is None:
                self.interval_end_us = int(self.store.wall()*1_000_000)
            if not self.state.days:
                self.tick(self.interval_end_us, publisher=False)
            self.accept(lifecycle.ShutdownRequested(self.interval_end_us or int(self.store.wall()*1_000_000)))
        if not self.state.shutdown:
            stamp = int(self.store.wall()*1_000_000)
            effects = self.tick(stamp)
            for effect in effects:
                if isinstance(effect, lifecycle.StartPublisherCycle):
                    self.submit_publisher(effect.slot, stamp)
        export_started = round(self.store.clock()*1_000_000) if self.startup_close is not None else 0
        yield from self.upload(self.bases, budget)
        yield from self.upload(self.traces, budget)
        with self.lock:
            now_us = int(self.store.wall()*1_000_000)
            today = day_at(now_us)
            self.fold(now_us)
            if self.port.lost.is_set():
                for day in self.journal.days:
                    if not self.journal.days[day].sealing and not self.journal.days[day].losses:
                        self.journal.loss(day)
            unknown = self.unknown
        if unknown is not None:
            day, pages, counter, closure = unknown
            try:
                if not (yield partial(self.store.reconcile, [*pages, counter, *([closure] if closure else [])], budget)):
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
            if (yield partial(self.store.reconcile, [result], budget)):
                with self.lock:
                    self.accept(lifecycle.CloseResultReply(int(self.store.wall()*1_000_000), day, True))
                    self.acknowledged(day, pages, closure)
                    self.handoff = None
            else:
                self.accept(lifecycle.CloseResultReply(int(self.store.wall()*1_000_000), day, None))
                return
        with self.lock:
            days = sorted(self.counters)
        for day in days:
            with self.lock:
                decision = next((d for d in self.state.days if d.day == day), None)
                if decision is None or (not decision.final and not decision.flush):
                    continue  # No StartFlush/StartFinalClose effect is outstanding.
                if day in self.final_attempted or (finals_only and not decision.final):
                    continue
                state = self.journal.days[day]
                reason = self.closing_reason or ("utc_boundary" if day < today else None)
                if reason:
                    state.closing = True
                    if self.startup_close is not None and day not in self.startup_close_phases:
                        self.startup_close_phases[day] = (
                            self.startup_preparation_started if self.startup_preparation_started is not None
                            else preparation_started, export_started)
                end_us = self.interval_end_us or now_us
                if reason == "utc_boundary":
                    import datetime as dt
                    end_us = int(dt.datetime.combine(dt.date.fromisoformat(day)+dt.timedelta(days=1), dt.time(), dt.UTC).timestamp()*1_000_000)
                if decision is not None and decision.final:
                    pages, counter, final_closure = self.final_plans[day]
                    closure = final_closure
                else:
                    with self.observer.lock:
                        self.fold(now_us)
                    pages = self.journal.freeze(day)
                    self.journal.checkpoint(day)
                    counter = self.counter_row(day, now_us, closed=False)
                    closure = None
            started = self.store.clock()
            attempt_us = int(self.store.wall()*1_000_000)
            flush_remaining = max(0, int((deadline-started)*1_000_000))
            shutdown_remaining = (max(0, int((self.termination_deadline-started)*1_000_000))
                                  if self.termination_deadline is not None else None)
            if closure is not None:
                with self.lock:
                    self.final_attempted.add(day)
            close_started = round(started*1_000_000) if self.startup_close is not None else 0
            spans = None
            if self.startup_close is not None:
                phase_start, phase_export = self.startup_close_phases.get(day, (preparation_started, export_started))
                spans = dict(preparation=[phase_start, phase_export],
                    export_wait=[phase_export, close_started])
            close_original = closure
            result_original = None
            completed = False
            try:
                try:
                    yield partial(self.store.flush, pages, counter, closure, budget)
                finally:
                    elapsed = max(0, round((self.store.clock()-started)*1_000_000))
                    if spans is not None:
                        spans["close"] = [close_started, close_started + elapsed]
                if closure is not None:
                    observed = int(self.store.wall()*1_000_000)
                    result = Row.build(CLOSE_RESULT_KIND, counter.key, dict(v=2, policy_version=POLICY,
                        contract_revision="a1", day=day, boot=self.journal.boot,
                        router_revision=self.settings.source_revision, closure_sha256=sha256(closure.value()),
                        final_checkpoint_seq=counter.value()["checkpoint_seq"], attempt_started_at_us=attempt_us,
                        observed_at_us=observed, physical_commit_ack_at_us=observed, elapsed_us=elapsed,
                        outcome="acknowledged", flush_remaining_us=flush_remaining,
                        shutdown_remaining_us=shutdown_remaining, handoff_attempt_limit=1))
                    result_original = result
                    validate_close_result(result.value(), closure.value(), termination_applies=self.termination_deadline is not None)
                    # Freeze only after the original reply. No readback path enters here.
                    effects = self.accept(lifecycle.FinalReply(observed, day, "timely"))
                    if not any(isinstance(e, lifecycle.StartCloseResult) for e in effects):
                        return
                    self.last_closure_ack = result.value()
                    self.handoff = (day, pages, closure, result)
                    result_started = round(self.store.clock()*1_000_000) if self.startup_close is not None else 0
                    try:
                        try:
                            yield partial(self.store.close_result, result, budget)
                        finally:
                            if spans is not None:
                                spans["result"] = [result_started, round(self.store.clock()*1_000_000)]
                    except Exception:
                        self.accept(lifecycle.CloseResultReply(int(self.store.wall()*1_000_000), day, None))
                        return  # Exactly one write; later exact read can establish landing.
                    completed = True
                    self.handoff = None
                    self.accept(lifecycle.CloseResultReply(int(self.store.wall()*1_000_000), day, True))
                with self.lock:
                    self.acknowledged(day, pages, closure)
                yield from self.reconcile_work(budget)
                if closure is None:
                    with self.lock:
                        ready = bool(reason and not self.bases.pending(day) and not self.traces.pending(day)
                            and self.journal.can_close(day, inflight=self.inflight + (day in self.publishing)
                            + len(self.observer.inflight_ticks)
                            + sum(r.day == day for r in self.port.fold_obligations), pending=self.pending,
                            interval_end_us=end_us, folded_us=self.folded_us))
                        decision = next(d for d in self.state.days if d.day == day)
                        drained = decision.traces if not self.traces.pending(day) else ()
                        effects = self.accept(lifecycle.FlushReply(int(self.store.wall()*1_000_000), day, True, ready, drained))
                    if any(isinstance(e, lifecycle.StartFinalClose) for e in effects):
                        yield from self._flush(deadline, finals_only=True)
            except Exception as error:
                with self.lock:
                    # An acknowledged close has already retired its day.
                    if day in self.journal.days:
                        uncertain = isinstance(error, UnknownCommit)
                        self.journal.failed(day, unknown=uncertain)
                        if not uncertain and closure is None:
                            self.journal.days[day].sealing = False
                        if uncertain:
                            self.unknown = (day, pages, counter, closure)
                    if closure is None:
                        self.accept(lifecycle.FlushReply(int(self.store.wall()*1_000_000), day, False))
                    if closure:
                        decision = next(d for d in self.state.days if d.day == day)
                        if decision.final_reply is None:
                            self.accept(lifecycle.FinalReply(int(self.store.wall()*1_000_000), day,
                                "unknown" if isinstance(error, UnknownCommit) else
                                "late" if self.store.clock() >= deadline else "error"))
                        self.final_veto.add(day)
                        self.close_failures[day] = self.close_failures.get(day, 0) + 1
                        logger.warning("shadow_v2_final_close_failed day=%s boot=%s unknown=%s",
                                       day, self.journal.boot, isinstance(error, UnknownCommit))
                return
            finally:
                if close_original is not None and self.startup_close is not None and spans is not None:
                    spans["final_pass"] = [close_started, round(self.store.clock()*1_000_000)]
                    try:
                        self.startup_close(day=day, interval_end_us=end_us, spans=spans,
                            complete=completed, outcome="graceful", closure=close_original,
                            result=result_original)
                    except Exception:  # noqa: S110 - observations never alter P3 progress
                        pass
                    self.startup_close_phases.pop(day, None)

    def maintain(self) -> None:
        self.drive(self.maintenance())

    def maintenance(self) -> Generator[Callable[[], Any], Any, None]:
        self.assert_owner()
        deadline = self.store.clock()+FLUSH_SECONDS
        now_us = int(self.store.wall()*1_000_000)
        day = day_at(now_us)
        if day not in self.counters:
            yield from self._flush(deadline)  # Drain older admitted days at capacity.
            return
        with self.lock:
            if self.journal.stopped:
                return
            terminal = self.journal.terminal_permits.get(day, 0) < RESERVATION_BLOCK
            event = self.journal.permits.get(day, 0) < RESERVATION_BLOCK
            allocate = self.store.clock() >= self.next_allocate and (terminal or event)
            if allocate:
                self.next_allocate = self.store.clock()+5
        if allocate:
            try:
                yield partial(self.store.allocate, day, terminal=terminal, event=event, budget=Budget(deadline, self.store.clock))
                with self.lock:
                    if event:
                        self.journal.grant(day, RESERVATION_BLOCK)
                    if terminal:
                        self.journal.terminal_permits[day] = self.journal.terminal_permits.get(day, 0)+RESERVATION_BLOCK
            except Exception:
                with self.lock:
                    target = self.counters[day]["diagnostics"]
                    self._counter_cells(day, target, {"cap_unavailable": target.get("cap_unavailable", 0)+1})
        yield from self._flush(deadline)

    def submit_publisher(self, slot: int, stamp: int) -> None:
        self.assert_owner()
        if not any(slot in d.publishers for d in self.state.days):
            return
        self.publishing.add(day_at(slot*SLOT_US))
        self.enabled()
        job = self.state.job
        if job is None or job.slot != slot:
            return
        args = (self.store, self.journal.boot, self.settings.effective_serving_region,
                self.settings.source_revision, job, stamp, self, self.is_incumbent(slot), self.has_incumbent(slot),
                self.warm())
        if (self.publisher_worker is not None and not self.publisher_worker.cancelled()
                and self.publisher_worker_active.is_set()):
            # A watchdog cannot stop a running thread. Keep the one-worker
            # executor bounded while its old call remains outstanding.
            self.accept(lifecycle.PublisherFailed(stamp, job.slot, job.generation, "publisher_worker_busy"))
            return
        fallback = failed_publisher_reply(self.journal.boot, job, "publisher_worker_exception")
        if self.loop is None:
            try:
                result = publish_cycle(*args)
            except BaseException:
                self.publisher_reply(fallback, job=job)
                raise
            self.publisher_reply(result, job=job)
            return
        loop = self.loop
        reply = loop.create_future()
        self.publisher_job = reply
        worker_active = threading.Event()
        worker_active.set()
        self.publisher_worker_active = worker_active
        def deliver(result: PublisherReply) -> None:
            try:
                self.publisher_reply(result, job=job)
            finally:
                if not reply.done():
                    reply.set_result(None)
        def execute() -> None:
            result = fallback
            try:
                result = publish_cycle(*args)
            except BaseException:
                logger.exception("shadow_v2_publisher_failed slot=%s generation=%s", job.slot, job.generation)
                raise
            finally:
                # Delivery can run before the executor marks its Future done.
                worker_active.clear()
                loop.call_soon_threadsafe(deliver, result)
        try:
            self.publisher_worker = self.publisher_executor.submit(execute)
        except Exception:
            worker_active.clear()
            deliver(failed_publisher_reply(self.journal.boot, job, "publisher_submit_failed"))
            raise

    async def run(self) -> None:
        self.assert_owner()
        self.loop = asyncio.get_running_loop()
        while not self.stopped.is_set() and self.enabled():
            if self.store.session_recovery_due() and (self.session_job is None or self.session_job.done()):
                self.session_job = self.session_executor.submit(self.store.prepare_session, self.journal.boot)
            now = self.store.clock()
            now_us = int(self.store.wall()*1_000_000)
            for effect in self.tick(now_us):
                if isinstance(effect, lifecycle.StartPublisherCycle):
                    self.submit_publisher(effect.slot, now_us)
            if now >= self.next_flush and (self.flush_job is None or self.flush_job.done()):
                self.next_flush = now+5
                self.flush_job = self.loop.create_task(self.drive_async(self.maintenance()))
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
        def shutdown_on_owner() -> None:
            self.assert_owner()
            if self.close_task is None:
                self.close_task = loop.create_task(self.close())

        def stop(signum: int, frame: Any) -> None:
            # A terminal latch, not a read/modify/write counter: nested signals
            # and interrupted State assignments cannot roll it back. No locks,
            # lifecycle access or asyncio task mutation in the signal handler.
            self.revocation_epoch = 1
            stamp = self.store.clock()
            self.signal_stamp = min(self.signal_stamp if self.signal_stamp is not None else stamp, stamp)
            deadline = self.signal_stamp+9
            self.termination_deadline = min(self.termination_deadline or deadline, deadline)
            loop.call_soon_threadsafe(shutdown_on_owner)
            previous(signum, frame)
        self.previous_signal_handler, self.signal_handler = previous, stop
        signal.signal(signal.SIGTERM, stop)

    def start(self) -> None:
        if self.enabled() and self.task is None:
            self.loop = asyncio.get_running_loop()
            self.trap_sigterm()
            stamp = int(self.store.wall()*1_000_000)
            for effect in self.tick(stamp):
                if isinstance(effect, lifecycle.StartPublisherCycle):
                    self.submit_publisher(effect.slot, stamp)
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
        self.startup_preparation_started = round(self.store.clock()*1_000_000) if self.startup_close is not None else 0
        deadline = min(deadline or self.store.clock()+9, self.termination_deadline or self.store.clock()+9)
        if reason == "shutdown":
            self.termination_deadline = deadline
        with self.lock:
            # Cut off new deferred registrations and admit every accepted owner
            # before ShutdownRequested freezes lifecycle admission. Workers may
            # still discharge those owners while close drains their outcomes.
            self.journal.stopped = True
            self.sync_fold_obligations()
            stamp = int(self.store.wall()*1_000_000)
            if not self.state.days:
                self.tick(stamp, publisher=False)
            self.accept(lifecycle.ShutdownRequested(stamp))
            self.stopped.set()
            self.slots.stopped = True
            self.closing_reason = reason
            self.interval_end_us = stamp
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
                        owned = self.inflight or self.pending or "pending" in self.port.fold_obligations.values() or any(
                            slot.body is None or not slot.completion_requested
                            for page in self.journal.pages for slot in page.slots)
                    if not owned:
                        break
                    await asyncio.sleep(.01)
                # At most three days; pages/reference reconciliation and closure
                # need separate passes. Leave unresolved writers open on no progress.
                for _ in range(6):
                    before = (len(self.journal.days), len(self.journal.pages), len(self.journal.batches), self.unknown is not None, self.handoff is not None, self.state)
                    await asyncio.shield(self.drive_async(self._flush(min(deadline, self.store.clock()+FLUSH_SECONDS))))
                    after = (len(self.journal.days), len(self.journal.pages), len(self.journal.batches), self.unknown is not None, self.handoff is not None, self.state)
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
            self.session_executor.shutdown(wait=False, cancel_futures=True)
            self.publisher_executor.shutdown(wait=False, cancel_futures=True)
            self.evidence_executor.shutdown(wait=False, cancel_futures=True)


def install(app: Any, settings: Settings, backend: Any) -> None:
    if (settings.effective_async_settle_shadow_policy() != POLICY or not settings.async_settle_shadow_workspace_ids
            or settings.async_settle_enabled or backend is None or not hasattr(backend, "_database")):
        return
    store = ObservationStore(backend._database)
    service = Service(settings, store)
    store.prepare_session(service.journal.boot)
    app.state.async_settle_shadow_observation = service
    @app.on_event("startup")
    async def start() -> None:
        service.start()
    @app.on_event("shutdown")
    async def stop() -> None:
        await service.close()
