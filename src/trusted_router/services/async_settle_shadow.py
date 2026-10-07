"""Dormant router observation lifecycle. Nothing here can finalize money."""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

from trusted_router.async_settle_shadow_binding import LIFETIME, ShadowSigner
from trusted_router.async_settle_shadow_compare import Booking, Context, compare
from trusted_router.async_settle_shadow_evidence import Counters, day_at, dimensions, sample
from trusted_router.async_settle_shadow_projection import (
    prewarm_catalog,
    project,
    snapshot_material,
)
from trusted_router.async_settle_shadow_wire import LOCAL_BYTES
from trusted_router.billing_snapshot import BillingSnapshot
from trusted_router.config import Settings
from trusted_router.detached_jws import canonical
from trusted_router.services.async_settle_shadow_admission import Observer
from trusted_router.storage_models import GatewayAuthorization, generation_id_for_authorization


@dataclass
class Capture:
    runtime: Runtime
    body: Any
    kind: str
    received: float
    started: float
    authorization: GatewayAuthorization | None = None
    endpoint: Any = None
    endpoints: tuple[Any, ...] | None = None
    document: dict[str, Any] | None = None
    # Replay can recover signed S0, but cannot attest the original S1 booking view.
    prices_match_booking: bool = True
    dropped: bool = False


_CAPTURE: contextvars.ContextVar[Capture | None] = contextvars.ContextVar("async_shadow_capture", default=None)


def capture_authorization(authorization: GatewayAuthorization) -> None:
    capture = _CAPTURE.get()
    if capture is not None and not capture.dropped and capture.runtime.opted(authorization.workspace_id):
        with capture.runtime.counters.request_access(capture.received) as acquired:
            if not acquired:
                capture.dropped = True
                return
            if capture.authorization is None:
                capture.runtime.counters.retain(capture.received)
            capture.authorization = authorization


def capture_prices(authorization: GatewayAuthorization, endpoint: Any, document: Any, catalog: Any,
                   *, prices_match_booking: bool = True) -> None:
    capture = _CAPTURE.get()
    if capture is None or capture.authorization is None or capture.dropped:
        return
    try:
        ids = tuple(authorization.candidate_endpoint_ids)
        if not ids or len(ids) > 128:
            return
        capture.endpoint = endpoint
        # Catalog ModelEndpoint values are frozen dataclasses. Keep the exact
        # references used for this booking, not a later background catalog read.
        capture.endpoints = tuple(endpoint if identity == endpoint.id else catalog[identity] for identity in ids)
        capture.document = document
        capture.prices_match_booking = prices_match_booking
    except Exception:
        capture.endpoints = None


class Runtime:
    def __init__(self, settings: Settings, async_runtime: Any, store: Any = None,
                 observer: Observer | None = None) -> None:
        import threading
        self.settings, self.async_runtime, self.store, self.observer = settings, async_runtime, store, observer
        self.signer = ShadowSigner(async_runtime.signer) if async_runtime.signer else None
        self.counters = Counters(settings.primary_region, settings.release or "unknown")
        self.lock = threading.RLock()
        self.tokens, self.refilled = 10., time.monotonic()
        self.pending = self.queued_bytes = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="settle-shadow")
        self.permit_day, self.permits, self.next_allocate = "", 0, 0.
        self.last_flush = 0.
        self.observer_flushed: dict[str, int] = {}
        if settings.async_settle_shadow_workspace_ids and not settings.async_settle_enabled:
            try:
                prewarm_catalog()
            except Exception:  # noqa: S110 - optional cache warming cannot affect startup
                pass

    def opted(self, workspace: str) -> bool:
        return workspace in self.settings.async_settle_shadow_workspace_ids and not self.settings.async_settle_enabled

    def authorize(self, authorization: GatewayAuthorization, additions: dict[str, Any], *, replay: bool,
                  header: bool, route: str | None, streamed: bool, endpoints: list[Any]) -> None:
        if not self.opted(authorization.workspace_id):
            return
        with self.counters.request_access(time.time()) as acquired:
            if not acquired:
                return
            self._authorize(authorization, additions, replay=replay, header=header,
                            route=route, streamed=streamed, endpoints=endpoints)

    def _authorize(self, authorization: GatewayAuthorization, additions: dict[str, Any], *, replay: bool,
                   header: bool, route: str | None, streamed: bool, endpoints: list[Any]) -> None:
        started = time.thread_time_ns()
        dims = dimensions(authorization.provider, route, streamed)
        try:
            with self.counters.lock:
                self.counters.increment(dims, "authorize_attempts")
                self.counters.increment(dims, "authorize_replay" if replay else "authorize_fresh")
            if replay or not header:
                if not header:
                    self.counters.increment(dims, "header_absent")
                self.counters.reason(dims, "authorize", "replay" if replay else "header_absent", "exclusions")
                return
            snapshot = additions.get("billing_snapshot")
            if snapshot is None or self.signer is None:
                self.counters.reason(dims, "authorize", "snapshot_unavailable", "exclusions")
                return
            self.counters.increment(dims, "requested_eligible")
            if len(endpoints) > 128 or len(canonical(snapshot)) > LOCAL_BYTES:
                self.counters.reason(dims, "authorize", "snapshot_size", "exclusions")
                return
            issued = int(time.time())
            claims = dict(authorization_id=authorization.id, generation_id=generation_id_for_authorization(authorization.id),
                workspace_id=authorization.workspace_id, key_id=authorization.key_hash,
                invocation_nonce=authorization.invocation_nonce, reservation_id=authorization.credit_reservation_id,
                billing_authority=authorization.settlement, journal_region=self.async_runtime.region,
                epoch=self.async_runtime.epoch, route_type=route, streamed=streamed, settle_origin="typed",
                snapshot_version=1, snapshot_hash=additions["billing_snapshot_hash"], async_eligible=False,
                iss=self.signer.trusted.iss, aud="router-shadow", iat=issued, exp=issued+LIFETIME)
            additions["billing_shadow_binding"] = self.signer.sign(claims, issued)
            self.counters.increment(dims, "snapshot_sent")
        except Exception:
            self.counters.reason(dims, "authorize", "snapshot_unavailable", "exclusions")
        finally:
            self.counters.histogram("authorize_shadow_hist", (time.thread_time_ns()-started)//1000)

    def admit(self, size: int) -> str | None:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(10., self.tokens + max(0, now-self.refilled)*2)
            self.refilled = now
            if self.tokens < 1:
                return "rate_limit"
            self.tokens -= 1
            if self.pending >= 32 or self.queued_bytes + size > 256*1024:
                return "queue_full"
            self.pending += 1
            self.queued_bytes += size
            return None

    def submit(self, capture: Capture, request: Any, result: Any, background: Any) -> None:
        with self.counters.request_access(capture.received) as acquired:
            if not acquired:
                return
            if not self.lock.acquire(blocking=False):
                self.counters.defer("drop", capture.received)
                return
            try:
                with self.counters.day(capture.received):
                    self._submit(capture, request, result, background)
            finally:
                self.lock.release()

    def _submit(self, capture: Capture, request: Any, result: Any, background: Any) -> None:
        auth = capture.authorization
        if auth is None or not self.opted(auth.workspace_id):
            return
        dims = dimensions(capture.endpoint.provider if capture.endpoint else auth.provider,
                          capture.body.route_type, capture.body.streamed)
        with self.counters.lock:
            self.counters.increment(dims, capture.kind + "_attempts")
            self.counters.increment(dims, "observed_attempts")
            self.counters.increment(dims, "observed_unknown")
        if result is None:
            # An exception response does not retain FastAPI's BackgroundTasks.
            # Count the attempt without reserving queue capacity or starting
            # evidence work against a money outcome that was not confirmed.
            self.counters.increment(dims, "booking_unknown")
            self.counters.reason(dims, capture.kind, "booking_unknown", "exclusions")
            return
        if background is None:
            self.counters.reason(dims, capture.kind, "queue_full")
            return
        # Bound retained request-local facts as well as transport bytes. Catalog
        # endpoints are shared immutable references, not copied snapshots.
        ids = auth.candidate_endpoint_ids
        native = (auth.id, auth.workspace_id, auth.key_hash, auth.credit_reservation_id, auth.invocation_nonce)
        labels = (auth.model_id, auth.provider, capture.body.route_type, capture.body.selected_endpoint,
                  capture.body.endpoint, capture.body.service_tier)
        if any(value is not None and len(value) > limit for values, limit in ((native, 64), (labels, 128)) for value in values):
            self.counters.reason(dims, capture.kind, "identity", "rejections")
            return
        if any(value is not None and value.bit_length() > 63 for value in (
                capture.body.actual_input_tokens, capture.body.actual_output_tokens,
                capture.body.input_tokens, capture.body.output_tokens,
                capture.body.cache_read_input_tokens, capture.body.cache_creation_input_tokens,
                capture.body.reasoning_tokens, capture.body.additional_cost_microdollars)):
            self.counters.reason(dims, capture.kind, "integer", "rejections")
            return
        if len(auth.pricing_snapshot or "") > LOCAL_BYTES:
            self.counters.reason(dims, capture.kind, "snapshot_size")
            return
        if len(ids) > 128 or any(len(identity) > 128 for identity in ids):
            self.counters.reason(dims, capture.kind, "snapshot_size")
            return
        size = 2 * 12289 + 8192 + sum(len(identity) for identity in ids) + 4 * len(auth.pricing_snapshot or "")
        refused = self.admit(size)
        if refused:
            self.counters.reason(dims, capture.kind, refused)
            return
        try:
            values = []
            for key, value in request.headers.raw:
                if key.lower() == b"x-tr-settlement-shadow":
                    values.append(value[:12289].decode("latin1"))
                    if len(values) == 2:
                        break
            headers = tuple(values)
            from trusted_router.schemas import GatewaySettleRequest
            body = capture.body
            capture.body = GatewaySettleRequest.model_construct(**{
                name: getattr(body, name) for name in (
                    "authorization_id", "actual_input_tokens", "actual_output_tokens", "input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens", "route_type",
                    "streamed", "service_tier", "usage_estimated", "additional_cost_microdollars", "selected_endpoint", "endpoint")})
            capture.authorization = GatewayAuthorization(
                id=auth.id, workspace_id=auth.workspace_id, key_hash=auth.key_hash,
                model_id=auth.model_id, provider=auth.provider, usage_type=auth.usage_type,
                estimated_microdollars=auth.estimated_microdollars, credit_reservation_id=auth.credit_reservation_id,
                created_at=auth.created_at, endpoint_id=auth.endpoint_id,
                candidate_endpoint_ids=list(ids), invocation_nonce=auth.invocation_nonce, settlement=auth.settlement)
            if headers:
                self.counters.increment(dims, "envelope_present")
            elapsed = int((time.monotonic()-capture.started)*1e6)
            data = result.get("data", {}) if isinstance(result, dict) else {}
            outcome = dict(data={name: data.get(name) for name in ("settled", "already_settled", "finalization_outcome")})
            self.counters.retain(capture.received)
            try:
                background.add_task(self.work, capture, headers, outcome, elapsed, dims, size)
            except Exception:
                self.counters.release(capture.received)
                raise
        except Exception:
            with self.lock:
                self.pending -= 1
                self.queued_bytes -= size
            self.counters.reason(dims, capture.kind, "worker_error")

    async def work(self, capture: Capture, headers: tuple[str, ...], result: Any, elapsed: int,
                   dims: tuple[str, str, bool | None], size: int) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self.executor, partial(self.process, capture, headers, result, elapsed, dims))
        except Exception:
            self.counters.reason(dims, "worker", "worker_error")
        finally:
            self.counters.release(capture.received)
            with self.lock:
                self.pending -= 1
                self.queued_bytes -= size

    def process(self, capture: Capture, headers: tuple[str, ...], result: Any, elapsed: int,
                dims: tuple[str, str, bool | None]) -> None:
        with self.counters.day(capture.received):
            self._process(capture, headers, result, elapsed, dims)

    def _process(self, capture: Capture, headers: tuple[str, ...], result: Any, elapsed: int,
                 dims: tuple[str, str, bool | None]) -> None:
        auth = capture.authorization
        if auth is None or not self.opted(auth.workspace_id):
            return
        deadline = time.monotonic() + 1
        self.counters.increment(dims, "comparison_attempts")
        failure_reason = "store_unavailable"
        persisted = False
        try:
            if self.store is None:
                raise ValueError("store_unavailable")
            day = day_at(time.time())
            if day != self.permit_day:
                self.permit_day, self.permits = day, 0
            if not self.permits and time.monotonic() >= self.next_allocate:
                self.next_allocate = time.monotonic()+5
                self.permits = self.store.reserve(day, deadline)
            if not self.permits:
                self.counters.reason(dims, "worker", "daily_cap")
                return
            data = result.get("data", {}) if isinstance(result, dict) else {}
            finalized = bool(data.get("settled") or data.get("already_settled") or data.get("finalization_outcome") == "refunded")
            try:
                booking = self.store.booking(auth.id, deadline) if finalized else Booking(outcome="pending")
            except Exception:
                outcome = data.get("finalization_outcome")
                booking = Booking(None, outcome if outcome in {"settled", "refunded"} else "unknown", finalized)
                self.counters.increment(dims, "booking_unknown")
                self.counters.reason(dims, "worker", "store_unavailable")
            booking_us = int((time.monotonic()-capture.started)*1e6) if booking.confirmed else None
            def rebuild() -> BillingSnapshot:
                if capture.endpoints is None:
                    raise ValueError("rebuild unavailable")
                rebuilt = project(capture.endpoints, auth.created_at, capture.document)
                if len(snapshot_material(rebuilt)[0]) > LOCAL_BYTES:
                    raise ValueError("snapshot_size")
                return rebuilt
            ctx = Context(auth, capture.body, capture.kind,
                          capture.endpoint.id if capture.endpoint else None,
                          self.async_runtime.region, self.async_runtime.epoch, int(capture.received), booking,
                          rebuild if capture.endpoints else None, capture.endpoints is not None and capture.prices_match_booking,
                          "stage_d_document" if capture.document else "catalog_at_authorize_time" if capture.endpoints else "unknown")
            failure_reason = "worker_error"
            cpu_started = time.thread_time_ns()
            compared = compare(headers, ctx, [self.signer.trusted] if self.signer else [])
            comparator_us = (time.thread_time_ns()-cpu_started)//1000
            self.counters.outcome(dims, compared)
            for reason in sorted(compared.reasons):
                if reason != "catalog_change":
                    self.counters.reason(dims, capture.kind, reason,
                        "rejections" if reason in {"header_duplicate", "header_size", "base64", "json_encoding", "json_duplicate", "json_shape", "integer", "proof_signature", "proof_expired", "hash", "identity", "raw_usage", "go_failure"} else "exclusions")
            # Expired observations are counters only, on the receipt day. Check
            # server-owned age as well: a malformed proof can fail before expiry
            # verification, and queued work must not recreate a retired partition.
            created_at = dt.datetime.fromisoformat(auth.created_at.replace("Z", "+00:00")).timestamp()
            retired = day_at(created_at) < day_at(time.time() - 30*86400)
            if "proof_expired" in compared.reasons or retired:
                if "proof_expired" not in compared.reasons:
                    self.counters.reason(dims, capture.kind, "proof_expired", "rejections")
                return
            if not booking.confirmed:
                field = "booking_pending" if booking.outcome == "pending" else "booking_unknown"
                self.counters.increment(dims, field)
                self.counters.reason(dims, capture.kind, field, "exclusions")
                return
            if day_at(time.time()) != self.permit_day:
                self.counters.reason(dims, "worker", "daily_cap")
                return
            self.permits -= 1  # Lost/unknown writes consume permits permanently.
            admission = self.observer.peek(auth.workspace_id) if self.observer else None
            with self.counters.lock:
                counter = self.counters._day()
                self.counters.add(counter, counter["admission_observer"], "prediction_" + (admission["prediction"] if admission else "unknown"))
            row = sample(ctx, compared, observed_us=int(capture.received*1e6), router_us=elapsed,
                         comparator_us=comparator_us, booking_us=booking_us, instance=self.counters.instance,
                         revision=self.counters.revision, admission=admission)
            started = time.monotonic()
            failure_reason = "store_unavailable"
            outcome = self.store.insert_sample(row["authorization_day"]+"/"+auth.id, row, deadline)
            failure_reason = "worker_error"
            self.counters.histogram("evidence_write_hist", int((time.monotonic()-started)*1e6))
            if outcome == "inserted" and compared.classification in {"exact", "explained-by-catalog-change"} and admission and admission["prediction"] != "unknown":
                with self.counters.lock:
                    counter = self.counters._day()
                    counter["first_evidence_at_us"] = counter["first_evidence_at_us"] or int(capture.received * 1e6)
            if outcome in {"inserted", "duplicate", "conflict"}:
                self.counters.increment(dims, {"inserted": "samples_inserted", "duplicate": "duplicate_samples", "conflict": "conflicting_samples"}[outcome])
                persisted = True
            if outcome == "winner_polarity":
                self.counters.reason(dims, "worker", "winner_polarity", "exclusions")
            elif outcome == "conflict":
                with self.counters.lock:
                    self.counters._day()["last_mismatch_at_us"] = int(time.time()*1e6)
            elif not persisted:
                raise ValueError("unknown persistence outcome")
        except Exception as error:
            if isinstance(error, ValueError) and str(error) == "evidence_size":
                failure_reason = "evidence_size"
            self.counters.reason(dims, "worker", failure_reason)
        finally:
            if not persisted:
                self.counters.increment(dims, "comparison_dropped")
            self.flush(deadline)

    def flush(self, deadline: float, closed: bool = False) -> None:
        if self.store is None or not closed and time.monotonic()-self.last_flush < 5:
            return
        self.last_flush = time.monotonic()
        try:
            # Persist and acknowledge older days before registering the current
            # writer. Active money/queued tasks retain their receipt day until
            # completion; the next timer then closes it. Failed closes stay in
            # the bounded retention set and are never silently acknowledged.
            for identity, body in self.counters.snapshot(retiring_only=True):
                self.store.flush(identity, body, deadline)
                self.counters.acknowledge(identity, body)
            # Register even an idle serving instance. The external inventory,
            # not the set of successful writers, remains the roster authority.
            with self.counters.lock:
                self.counters._day()
                if any(day < day_at(self.counters.clock()) for day in self.counters.days):
                    # In-flight old-day work still owns its writer. Accumulate
                    # new-day counters in memory, but don't open that durable
                    # writer until the prior close is acknowledged. No waiting
                    # or I/O is added to the request path.
                    return
            if self.observer is not None:
                with self.counters.lock:
                    counter = self.counters._day()
                    observer_counts = counter["admission_observer"]
                    for key in ("workspace_reads", "health_reads", "read_failures", "missed_ticks"):
                        value = self.observer.counts[key]
                        self.counters.add(counter, observer_counts, key, max(0, value - self.observer_flushed.get(key, 0)))
                        self.observer_flushed[key] = value
            for identity, body in self.counters.snapshot(closed):
                self.store.flush(identity, body, deadline)
                self.counters.acknowledge(identity, body)
        except Exception:
            self.counters.reason(dimensions(None, None, None), "worker", "store_unavailable")

    async def maintain_counters(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            # Only one outstanding flush; it shares the bounded single worker
            # with observations, never waits in an HTTP handler, and never
            # accumulates a timer retry queue.
            await asyncio.get_running_loop().run_in_executor(
                self.executor, partial(self.flush, time.monotonic()+1))
            try:
                await asyncio.wait_for(stopped.wait(), timeout=5)
            except TimeoutError:
                pass


async def observe_entry(request: Any, body: Any, settings: Settings, background: Any,
                        callback: Any, *, kind: str) -> Any:
    runtime = getattr(request.app.state, "async_settle_shadow", None)
    if runtime is None or not settings.async_settle_shadow_workspace_ids or settings.async_settle_enabled:
        return await callback()
    capture = Capture(runtime, body, kind, time.time(), time.monotonic())
    token = _CAPTURE.set(capture)
    result = None
    try:
        result = await callback()
        return result
    finally:
        _CAPTURE.reset(token)
        try:
            try:
                if capture.authorization is not None and not capture.dropped and runtime.opted(capture.authorization.workspace_id):
                    runtime.submit(capture, request, result, background)
            finally:
                if capture.authorization is not None:
                    runtime.counters.release_request(capture.received)
        except Exception:  # noqa: S110 - preserve the original outcome, no untrusted logs
            # The caller's original result/error/cancellation always wins.
            pass


def install(app: Any, settings: Settings, backend: Any) -> None:
    if not settings.async_settle_shadow_workspace_ids or settings.async_settle_enabled:
        return
    store = observer = None
    if backend is not None and hasattr(backend, "_database"):
        from trusted_router.storage_gcp_async_admission import read_admission, read_health
        from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
        store = EvidenceStore(backend._database)
        configured_workspaces = settings.async_settle_shadow_workspace_ids
        observer = Observer(configured_workspaces,
                            partial(read_admission, backend._database), partial(read_health, backend._database),
                            cap=settings.async_settle_pilot_cap_micro,
                            enabled=lambda: settings.async_settle_shadow_workspace_ids == configured_workspaces and not settings.async_settle_enabled)
    runtime = app.state.async_settle_shadow = Runtime(settings, app.state.async_settle, store, observer)
    stopped = asyncio.Event()
    maintenance: asyncio.Task[None] | None = None
    @app.on_event("startup")
    async def start() -> None:
        nonlocal maintenance
        if observer is not None:
            observer.start()
        maintenance = asyncio.create_task(runtime.maintain_counters(stopped))
    @app.on_event("shutdown")
    async def stop() -> None:
        stopped.set()
        if maintenance is not None:
            await maintenance
        if observer is not None:
            await observer.close()
        await asyncio.get_running_loop().run_in_executor(runtime.executor, partial(runtime.flush, time.monotonic()+1, True))
        runtime.executor.shutdown(wait=False, cancel_futures=True)
