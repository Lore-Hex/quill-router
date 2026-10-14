"""Opted-cohort HTTP observation; all verification and storage run off-request."""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from queue import Empty, Full, Queue
from types import SimpleNamespace
from typing import Any, NamedTuple, TypeVar

from trusted_router.async_settle_shadow_binding import ShadowSigner
from trusted_router.async_settle_shadow_cohort import (
    VerifiedTerminal,
    receipt_bucket,
    sample_retry,
    verify_terminal,
)
from trusted_router.async_settle_shadow_compare import Booking, Context
from trusted_router.async_settle_shadow_evidence import dimensions, sample
from trusted_router.async_settle_shadow_journal import SAMPLE_KIND, Reservation, Row
from trusted_router.async_settle_shadow_projection import project, snapshot_material
from trusted_router.async_settle_shadow_v2_contract import (
    DEPENDENT,
    FEATURES,
    HEADER_BYTES,
    LOCAL_BYTES,
    RequestedFacts,
)
from trusted_router.async_settle_shadow_v2_evidence import EVENT, PREDICTION, committed_identity
from trusted_router.async_settle_shadow_v2_schema import POLICY, require, sha256
from trusted_router.billing_snapshot import Eligibility, exclusion
from trusted_router.config import Settings
from trusted_router.detached_jws import canonical
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.services.async_settle_shadow_admission import unknown
from trusted_router.services.async_settle_shadow_cohort import (
    AuthorizeInput,
    authorize,
    has_v2_capability,
)
from trusted_router.services.async_settle_shadow_health import Port
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_gcp_shadow_observation import Budget
from trusted_router.storage_models import GatewayAuthorization, generation_id_for_authorization

T = TypeVar("T")

# Fail installation if the pinned feature contract changes without updating the
# literal request projection below. Preserve the strict import allowlist.
assert set(FEATURES + DEPENDENT) == {
    "custom_model", "user_model", "native_batch", "fusion", "polyphemus", "liberty",
    "tool_cost", "search_cost", "image_cost", "video_cost", "request_fee", "private_tier_basis",
}


_ORDINARY_PARAMETERS = frozenset({"temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
                    "stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias", "logprobs",
                    "top_logprobs", "response_format", "reasoning_effort", "stream"})


def receipt_dimensions(adapter: str | None, route: str | None, streamed: bool | None) -> dict[str, Any]:
    """The v2 schema represents unobserved dimensions with null."""
    values = dimensions(adapter, route, streamed)
    return dict(zip(("adapter", "route_type", "streamed"),
                    (None if v == "unknown" else v for v in values), strict=True))


def authorization_identity(auth: GatewayAuthorization, runtime: Any, typed: bool) -> dict[str, Any]:
    return dict(authorization_id=auth.id, generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id, key_id=auth.key_hash, invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id, billing_authority=auth.settlement,
        journal_region=runtime.effective_journal_region, epoch=runtime.epoch,
        settle_origin="typed" if typed else "legacy")


def map_requested_facts(*, body: Any, authorization: GatewayAuthorization, endpoints: list[Any],
                        typed: bool, federated: bool, runtime: Any, revision: str,
                        observed_us: int, validated_snapshot: bool = False, server_owned: bool = False) -> AuthorizeInput:
    """Ruling 5: one mapping of handler-owned facts and complete returned candidates.

    Missing members and unsupported feature representations are unresolved.
    Ordinary program facts follow the actual resolved authorization and route;
    adapter eligibility requires every candidate's supported pricing projection.
    """
    auth = authorization
    dims = receipt_dimensions(auth.provider, body.route_type, body.stream)
    identity = authorization_identity(auth, runtime, typed)
    facts = candidates = None
    server_facts = None
    complete = supported = False
    reason: str | None = "missing_context"
    try:
        require(body.route_type is not None and type(body.stream) is bool, "missing_context")
        require(body.requested_parameters is not None, "missing_context")
        require(not body.model_extra and "input_modalities" not in body.model_fields_set, "missing_context")
        require(not set(body.requested_parameters) - _ORDINARY_PARAMETERS, "unknown_parameters")
        # A compound/alias route has no complete program witness at this seam.
        require(body.model == auth.model_id and not body.models, "missing_context")
        require(type(auth.additional_cost_reservation_microdollars) is int, "missing_context")
        # Selection reads handler-owned scalars; the EVENT owns their strict
        # domain validation. Do not validate them again through a billing DTO.
        factory = SimpleNamespace if server_owned else Eligibility
        requested: Any = factory(typed=typed, usage_type=auth.usage_type.value,
            authority="federated" if federated else auth.settlement,
            route_type=body.route_type, streamed=body.stream, service_tier=body.service_tier,
            app_markup=auth.app_markup_basis_points, custom_markup=auth.custom_model_markup_basis_points,
            receipt_fee=auth.receipt_fee_basis_points, request_fee=0,
            custom_model=bool(auth.custom_model_id), user_model=bool(auth.user_provided_model_id),
            tool_cost=bool(auth.additional_cost_reservation_microdollars), search_cost=False,
            image_cost=body.route_type == "images", video_cost=body.route_type == "videos",
            partner=not body.model.startswith(("openai/", "anthropic/")), liberty=auth.model_id.startswith("trustedrouter/liberty"),
            native_batch=auth.native_batch_eligible, fusion=auth.model_id.startswith("trustedrouter/fusion"),
            polyphemus=auth.model_id.startswith("trustedrouter/polyphemus"), private_tier_basis=False)
        projection = [dict(endpoint_id=e.id, adapter=e.provider if e.provider in {"openai", "anthropic"} else "other",
                           features=["request_fee"] if e.request_price_microdollars else []) for e in endpoints]
        complete = [e.id for e in endpoints] == list(auth.candidate_endpoint_ids)
        # Pricing support is proved from the exact returned objects, never a later catalog.
        try:
            if not validated_snapshot:
                project(tuple(endpoints), auth.created_at)
            supported = complete
        except (ValueError, TypeError, KeyError):
            supported = False
        candidates = None if server_owned else canonical(projection)
        fact_values = dict(settle_origin="typed" if typed else "legacy",
            usage_class=requested.usage_type, billing_authority=requested.authority,
            route=requested.route_type, streamed=requested.streamed, service_tier=requested.service_tier,
            app_markup=requested.app_markup, custom_markup=requested.custom_markup,
            receipt_requested=body.inference_receipt, receipt_fee=requested.receipt_fee,
            commercial_program="partner" if requested.partner else "ordinary", features=dict(custom_model=requested.custom_model, user_model=requested.user_model,
                native_batch=requested.native_batch, fusion=requested.fusion, polyphemus=requested.polyphemus,
                liberty=requested.liberty, tool_cost=requested.tool_cost, search_cost=requested.search_cost,
                image_cost=requested.image_cost, video_cost=requested.video_cost,
                request_fee=bool(requested.request_fee), private_tier_basis=requested.private_tier_basis),
            unknown_features=[])
        reason = exclusion(requested)
        if body.inference_receipt:
            reason = "receipt_fee"
        if reason is None and projection and all(p["adapter"] == "other" for p in projection):
            reason = "unsupported_adapter"
        if reason is None and projection and all("request_fee" in p["features"] for p in projection):
            reason = "request_fee"
        if server_owned:
            server_facts = fact_values
        else:
            facts = RequestedFacts.from_object(fact_values)
    except (ValueError, AttributeError, TypeError, KeyError) as error:
        reason = "unknown_parameters" if str(error) == "unknown_parameters" else "missing_context"
    return AuthorizeInput(observed_us, identity, revision, dims, facts, candidates, complete, supported, reason,
                          server_facts, projection if server_facts is not None else None, validated_snapshot)


class TerminalFacts(NamedTuple):
    """Bounded scalar handoff; strict DTO validation runs on the worker."""
    authorization_id: str
    route_type: Any
    streamed: Any
    usage_estimated: Any
    service_tier: Any
    actual_input_tokens: Any
    actual_output_tokens: Any
    input_tokens: Any
    output_tokens: Any
    cache_read_input_tokens: Any
    cache_creation_input_tokens: Any
    reasoning_tokens: Any
    additional_cost_microdollars: Any


def compact_terminal_facts(body: GatewaySettleRequest) -> TerminalFacts:
    """Retain only bounded comparator inputs, never request extras or metadata."""
    values = TerminalFacts(body.authorization_id, body.route_type, body.streamed,
        body.usage_estimated, body.service_tier, body.actual_input_tokens,
        body.actual_output_tokens, body.input_tokens, body.output_tokens,
        body.cache_read_input_tokens, body.cache_creation_input_tokens,
        body.reasoning_tokens, body.additional_cost_microdollars)
    for value in values:
        if isinstance(value, str):
            require(len(value) <= 128, "identity")
        elif type(value) is int:
            require(0 <= value < 1 << 63, "integer")
    return values


def compact_authorization(auth: GatewayAuthorization) -> GatewayAuthorization:
    """Copy bounded comparator identities, dropping tags and unrelated row data."""
    native = (auth.id, auth.workspace_id, auth.key_hash, auth.credit_reservation_id, auth.invocation_nonce)
    labels = (auth.model_id, auth.provider, auth.endpoint_id, auth.created_at, auth.settlement)
    require(all(v is None or len(v) <= limit for values, limit in ((native, 64), (labels, 128))
                for v in values), "identity")
    require(len(auth.candidate_endpoint_ids) <= 128 and all(len(v) <= 128 for v in auth.candidate_endpoint_ids), "snapshot_size")
    require(len(auth.pricing_snapshot or "") <= LOCAL_BYTES, "snapshot_size")
    return GatewayAuthorization(id=auth.id, workspace_id=auth.workspace_id, key_hash=auth.key_hash,
        model_id=auth.model_id, provider=auth.provider, usage_type=auth.usage_type, estimated_microdollars=0,
        credit_reservation_id=auth.credit_reservation_id, invocation_nonce=auth.invocation_nonce,
        settlement=auth.settlement, created_at=auth.created_at, endpoint_id=auth.endpoint_id,
        candidate_endpoint_ids=list(auth.candidate_endpoint_ids), pricing_snapshot=auth.pricing_snapshot)


@dataclass
class Capture:
    reservation: Reservation
    authorization: GatewayAuthorization
    body: Any
    kind: str
    headers: tuple[str, ...]
    prediction: dict[str, Any] | None
    endpoint: Any = None
    endpoints: tuple[Any, ...] | None = None
    document: dict[str, Any] | None = None
    prices_match_booking: bool = True
    finalized: bool = False
    verified: VerifiedTerminal | None = None
    started: float = field(default_factory=time.monotonic)
    router_us: int | None = None
    booking_us: int | None = None
    comparator_us: int | None = None


_CAPTURE: contextvars.ContextVar[Capture | None] = contextvars.ContextVar("v2_capture", default=None)


def capture_prices(authorization: GatewayAuthorization, endpoint: Any, document: Any, catalog: Any,
                   *, prices_match_booking: bool = True) -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.authorization.id == authorization.id:
        capture.endpoint = endpoint
        capture.prices_match_booking = prices_match_booking
        ids = authorization.candidate_endpoint_ids
        if len(ids) <= 128 and len(authorization.pricing_snapshot or "") <= LOCAL_BYTES:
            capture.endpoints = tuple(endpoint if i == endpoint.id else catalog[i] for i in ids)
            capture.document = document


def retained_replay_decision(cache: dict[str, bytes], authorization_id: str) -> dict[str, Any]:
    """A cache miss cannot invent an original writer/sequence or renew a proof."""
    raw = cache.get(authorization_id)
    require(raw is not None, "authorization_link_gap")
    assert raw is not None
    return json.loads(raw)


def receipt_prediction(capture: Capture, verified: VerifiedTerminal) -> dict[str, Any] | None:
    """No contract value represents an eligible receipt without a retained basis."""
    if verified.eligibility != "eligible":
        return None
    require(capture.prediction is not None, "prediction_demand_gap")
    assert capture.prediction is not None
    return {k: capture.prediction[k] for k in PREDICTION.fields}


def terminal_cap_exhaustion(port: Port) -> None:
    """Terminal cap exhaustion blocks the day; it grants no receipt-only lane."""
    port.lost.set()


def compared_handoff_fields(fields: dict[str, Any], authorization: GatewayAuthorization) -> dict[str, Any]:
    """Echo the existing nonce identity required by P2's compared retention.

    Fresh legacy responses omit this field; replay responses already carry it.
    The nonce is the same handler-owned value signed into the existing binding.
    """
    if not fields:
        return fields
    require(bool(authorization.invocation_nonce), "authorization_link_gap")
    return {**fields, "invocation_nonce": authorization.invocation_nonce}


class WorkerQueue(Queue[tuple[Callable[..., Any], tuple[Any, ...]]]):
    """Queue.put_nowait normally waits for its mutex; this port never does."""

    def put_nowait(self, item: tuple[Callable[..., Any], tuple[Any, ...]]) -> None:
        if not self.mutex.acquire(blocking=False):
            raise Full
        try:
            if self._qsize() >= self.maxsize:
                raise Full
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
        finally:
            self.mutex.release()


class Runtime:
    def __init__(self, settings: Settings, async_runtime: Any, port: Port, booking_store: Any, *, typed: bool) -> None:
        self.settings, self.async_runtime, self.port, self.booking_store = settings, async_runtime, port, booking_store
        self.typed = typed
        self.signer = ShadowSigner(async_runtime.signer) if async_runtime.signer else None
        self.queue = WorkerQueue(32)
        self.worker = threading.Thread(target=self.drain, name="shadow-v2-compare", daemon=True)
        self.guard = self.lock = threading.RLock()
        # Reserve the bounded replay-decision cache inside the shared byte cap.
        self.tokens, self.refilled, self.pending, self.queued_bytes = 10., time.monotonic(), 0, 32*4096
        self.stopped = False
        self.startup_admission = SimpleNamespace(ready=True)
        self.startup_attempt_result: Callable[[Any, str], None] | None = None
        self.startup_attempt_start: Callable[[], None] | None = None
        self.decisions: dict[str, bytes] = {}
        self.worker.start()  # install-time prestart; no request can start a thread

    def authorize_http(self, request: Any, body: Any, auth: GatewayAuthorization, additions: dict[str, Any],
                       endpoints: list[Any], *, typed: bool, federated: bool, replay: bool) -> None:
        inputs: AuthorizeInput | None = None
        try:
            startup_ready = self.startup_admission.ready
            observed_us = time.time_ns()//1000
            capability = tuple(request.headers.getlist("X-TR-Settlement-Shadow-Policy"))
            capable = has_v2_capability(capability)
            original = None
            if replay and capable:
                try:
                    original = retained_replay_decision(self.decisions, auth.id)
                except ValueError:
                    # The replay still receives its own unresolved receipt below.
                    pass
            if not startup_ready or replay or not capable:
                inputs = AuthorizeInput(observed_us, authorization_identity(auth, self.async_runtime, typed),
                    self.settings.source_revision, receipt_dimensions(auth.provider, body.route_type, body.stream), None, None, False, False,
                    "missing_context" if not startup_ready else "replay" if capable else "header_absent")
            else:
                inputs = map_requested_facts(body=body, authorization=auth, endpoints=endpoints,
                    typed=typed, federated=federated, runtime=self.async_runtime,
                    revision=self.settings.source_revision, observed_us=observed_us,
                    validated_snapshot="billing_snapshot" in additions, server_owned=True)
            def retain(raw: bytes) -> None:
                if not self.guard.acquire(blocking=False):
                    self.port.lost.set()
                    return
                try:
                    if len(self.decisions) >= 32:
                        del self.decisions[next(iter(self.decisions))]
                    self.decisions[auth.id] = raw
                finally:
                    self.guard.release()
            fields = authorize(self.settings, capability,
                self.port, inputs, additions, self.signer, replay=replay,
                original_decision=original, retained_original=True, original_locator=original["receipt_locator"] if original else None,
                finalized_event=retain if not replay else None, deferred=True)
            additions.update(compared_handoff_fields(fields, auth))
        except Exception as error:
            if inputs is not None:
                inputs.result = "timeout" if isinstance(error, TimeoutError) else "failed"
            self.port.lost.set()
        finally:
            if inputs is not None and inputs.started:
                self.defer_input(inputs, capable)

    def retain_startup_attempts(self, harness: Any) -> None:
        self.startup_attempt_result = harness.attempt_result
        self.startup_attempt_start = harness.attempt_start

    def defer_input(self, inputs: AuthorizeInput, capable: bool) -> None:
        # Transfer the already-owned input through the native bounded queue.
        size = 2*EVENT.limit
        if self.admit(size):
            if inputs.reservation is not None and inputs.event is not None:
                self.port.fail_fold(inputs.reservation)
            return
        if not self.dispatch(self.input_work, inputs, capable, size):
            if inputs.reservation is not None and inputs.event is not None:
                self.port.fail_fold(inputs.reservation)
            self.release(size)

    def input_work(self, inputs: AuthorizeInput, capable: bool, size: int) -> None:
        try:
            if capable:
                try:
                    if self.startup_attempt_start is not None:
                        self.startup_attempt_start()
                    if inputs.reservation is not None and self.startup_attempt_result is not None:
                        self.startup_attempt_result(inputs.reservation, inputs.result)
                except Exception:  # noqa: S110 - record availability does not change the native result
                    pass
            if inputs.reservation is not None and inputs.event is not None:
                self.fold_complete(inputs.reservation, inputs.event, (), None)
        except Exception:
            if inputs.reservation is not None and inputs.event is not None:
                self.port.fail_fold(inputs.reservation)
            raise
        finally:
            self.release(size)

    def admit(self, size: int) -> str | None:
        if not self.guard.acquire(blocking=False):
            return "queue_full"
        try:
            if self.stopped:
                return "queue_full"
            now = time.monotonic()
            self.tokens = min(10., self.tokens + max(0., now-self.refilled)*2)
            self.refilled = now
            if self.tokens < 1:
                return "rate_limit"
            if self.pending >= 32 or self.queued_bytes + size > 256*1024:
                return "queue_full"
            self.tokens -= 1
            self.pending += 1
            self.queued_bytes += size
            return None
        finally:
            self.guard.release()

    def capture_terminal(self, request: Any, body: Any, auth: GatewayAuthorization, kind: str) -> tuple[Capture | None, int]:
        """Ruling 4: reserve once, retain bounded inputs; no parser or booking read."""
        reservation = self.port.reserve(time.time_ns()//1000, terminal=True)
        if reservation is None:
            terminal_cap_exhaustion(self.port)
            return None, 0
        size = (2*(HEADER_BYTES+1) + 8192 + sum(len(v) for v in auth.candidate_endpoint_ids) + 4*len(auth.pricing_snapshot or "")
                if len(auth.candidate_endpoint_ids) <= 128 else 256*1024+1)
        refused = self.admit(size)
        if refused:
            capture = Capture(reservation, auth, body, kind, (), None)
            self.finish(capture, VerifiedTerminal("C", "unknown", "none", refused), Booking(), None, defer=True)
            self.port.lost.set()
            return None, 0
        try:
            compact = compact_terminal_facts(body)
            # Do not collect an unbounded getlist; stop at the duplicate witness.
            headers = []
            for key, value in request.headers.raw:
                if key.lower() == b"x-tr-settlement-shadow":
                    headers.append(value[:HEADER_BYTES+1].decode("latin1"))
                    if len(headers) == 2:
                        break
            capture = Capture(reservation, compact_authorization(auth), compact, kind, tuple(headers), None)
        except Exception:
            self.release(size)
            self.port.lost.set()
            capture = Capture(reservation, auth, body, kind, (), None)
            self.finish(capture, VerifiedTerminal("C", "unknown", "none", "local_work_overflow"), Booking(), None, defer=True)
            return None, 0
        return capture, size

    def terminal(self, request: Any, body: Any, auth: GatewayAuthorization, kind: str,
                 callback: Callable[[], T]) -> T:
        capture = None
        size = 0
        try:
            capture, size = self.capture_terminal(request, body, auth, kind)
        except Exception:
            self.port.lost.set()
        token = _CAPTURE.set(capture)
        try:
            return callback()
        finally:
            _CAPTURE.reset(token)
            if capture is not None:
                capture.router_us = max(0, int((time.monotonic()-capture.started)*1000000))
                if not self.dispatch(self.work, capture, size):
                    self.port.lost.set()
                    try:
                        self.finish(capture, VerifiedTerminal("C", "unknown", "none", "queue_full"),
                                    Booking(), None, defer=True)
                    except Exception:
                        self.port.lost.set()
                    self.release(size)

    def dispatch(self, action: Callable[..., Any], *args: Any) -> bool:
        if self.stopped or not self.worker.is_alive():
            return False
        try:
            self.queue.put_nowait((action, args))
            return True
        except Full:
            return False

    def drain(self) -> None:
        while True:
            try:
                action, args = self.queue.get(timeout=.05)
            except Empty:
                if self.stopped:
                    return
                continue
            try:
                action(*args)
            except Exception:
                self.port.lost.set()
            finally:
                self.queue.task_done()

    def defer_authorize(self, reservation: Reservation, event: dict[str, Any]) -> None:
        self.defer_fold(reservation, event, (), None)

    def defer_fold(self, reservation: Reservation, event: dict[str, Any],
                   rows: tuple[Row, ...], prediction: dict[str, Any] | None) -> None:
        # finalize(fold_owed=True) owns the debt before bytes are exposed.
        # This integration point transfers only its bounded work to the worker.
        # Charge two encoded EVENT bounds for the handoff alongside terminal work
        # and the retained decision cache, including the currently running item.
        size = 2*EVENT.limit
        if self.admit(size):
            self.port.fail_fold(reservation)
            return
        if not self.dispatch(self.fold_work, reservation, event, rows, prediction, size):
            self.port.fail_fold(reservation)
            self.release(size)

    def fold_work(self, reservation: Reservation, event: dict[str, Any],
                  rows: tuple[Row, ...], prediction: dict[str, Any] | None, size: int) -> None:
        try:
            self.fold_complete(reservation, event, rows, prediction)
        except Exception:
            self.port.fail_fold(reservation)
            raise
        finally:
            self.release(size)

    def fold_complete(self, reservation: Reservation, event: dict[str, Any],
                      rows: tuple[Row, ...], prediction: dict[str, Any] | None) -> None:
        require(self.port.fold_counts(reservation.day, receipt_bucket(event), obligation=reservation), "counter_overflow")
        if prediction is not None:
            with self.port.service.lock:
                self.port.service.counters[reservation.day]["admission_observer"]["prediction_"+prediction["prediction"]] += 1
        require(self.port.complete(reservation, rows), "unfinished_slot")

    def release(self, size: int) -> None:
        if not self.guard.acquire(blocking=False):
            # Do not forget unresolved accounting or reclaim bytes without ownership.
            self.port.lost.set()
            return
        try:
            self.pending -= 1
            self.queued_bytes -= size
        finally:
            self.guard.release()

    def work(self, capture: Capture, size: int) -> None:
        try:
            self.process(capture)
        except Exception:
            self.port.lost.set()
            try:
                failed = (replace(capture.verified, partition="C", proof_kind="none", reason="worker_error")
                          if capture.verified else VerifiedTerminal("C", "unknown", "none", "worker_error"))
                self.finish(capture, failed, Booking(), None)
            except Exception:
                self.port.lost.set()
        finally:
            self.release(size)

    def process(self, capture: Capture) -> None:
        if capture.finalized:
            return
        if isinstance(capture.body, TerminalFacts):
            capture.body = GatewaySettleRequest.model_validate(capture.body._asdict(), strict=True)
        if capture.prediction is None:
            capture.prediction = self.port.capture_prediction(capture.authorization.workspace_id, capture.reservation.observed_us)
        auth = capture.authorization
        try:
            booking = self.booking_store.booking(auth.id, time.monotonic()+1)
        except Exception:
            booking = Booking()
        capture.booking_us = max(0, int((time.monotonic()-capture.started)*1000000)) if booking.confirmed else None
        def rebuild() -> Any:
            require(capture.endpoints is not None, "snapshot_size")
            assert capture.endpoints is not None
            return project(capture.endpoints, auth.created_at, capture.document)
        ctx = Context(auth, capture.body, capture.kind, capture.endpoint.id if capture.endpoint else None,
            self.settings.effective_serving_region, self.async_runtime.epoch,
            capture.reservation.observed_us//1000000, booking, rebuild if capture.endpoints else None,
            capture.endpoints is not None and capture.prices_match_booking,
            "stage_d_document" if capture.document else "unknown",
            journal_region=self.async_runtime.effective_journal_region)
        local_bytes = len(auth.pricing_snapshot or "")
        if capture.endpoints:
            try:
                local_bytes = max(local_bytes, len(snapshot_material(rebuild())[0]))
            except Exception:
                local_bytes = LOCAL_BYTES+1
        comparison_started = time.thread_time_ns()
        verified = verify_terminal(capture.headers, ctx, [self.signer.trusted] if self.signer else [], local_bytes=local_bytes)
        capture.comparator_us = (time.thread_time_ns()-comparison_started)//1000
        if (verified.comparison is not None and verified.comparison.adapter is not None
                and capture.endpoint is not None and verified.comparison.adapter != capture.endpoint.provider):
            verified = replace(verified, partition="C", reason="identity")
        if not self.typed:
            verified = replace(verified, partition="C", eligibility="unknown", proof_kind="none", reason="untyped")
        capture.verified = verified
        self.finish(capture, verified, booking, ctx)

    def original_sample(self, row: Row) -> Row | None:
        """Retain the first queued or durable logical sample across HTTP retries."""
        with self.port.service.lock:
            for batch in self.port.service.journal.batches.values():
                for pending in batch:
                    if (pending.kind, pending.key) == (row.kind, row.key):
                        return pending
        return self.port.service.store.read(row.kind, row.key, Budget(time.monotonic()+1, time.monotonic))

    def finish(self, capture: Capture, verified: VerifiedTerminal, booking: Booking, ctx: Context | None, *, defer: bool = False) -> None:
        """Finalize the reserved attempt exactly once, then fold its class and E."""
        if capture.finalized:
            return
        reservation, auth = capture.reservation, capture.authorization
        comparison = verified.comparison
        dims = receipt_dimensions(comparison.adapter if comparison and comparison.adapter else auth.provider,
                                  capture.body.route_type, comparison.verified_streamed if comparison else None)
        prediction = receipt_prediction(capture, verified)
        booking_ref = sha256(dict(authorization_id=auth.id, outcome=booking.outcome, amount=booking.amount)) if booking.confirmed else None
        event: dict[str, Any] = dict(v=2, policy_version=POLICY, seq=f"{reservation.event_seq:020}",
            event_at_us=reservation.observed_us, receipt_at_us=reservation.observed_us,
            phase=capture.kind, authorization_day=verified.authorization_day or dt.datetime.fromisoformat(auth.created_at.replace("Z", "+00:00")).date().isoformat(),
            authorization_id=auth.id, identity=committed_identity(authorization_identity(auth, self.async_runtime, self.typed)),
            router_revision=self.settings.source_revision, dimensions=dims,
            decision=dict(reason=None, facts_sha256=None, candidate_set_sha256=None), facts=None, candidates=None,
            receipt_locator=None, original_receipt=None, certificate_sha256=None,
            payload_sha256=comparison.payload_hash if comparison else None, proof_kind=verified.proof_kind,
            partition=verified.partition, eligibility=verified.eligibility,
            comparison=(comparison.classification if comparison.classification in {"exact", "explained-by-catalog-change", "unevaluable"}
                        else "mismatch") if comparison else None,
            booking=dict(outcome=booking.outcome, reference=booking_ref, winner=booking.kind), prediction=prediction,
            retry_original=None, completion_of=None,
            diagnostic=dict(reason=verified.reason, required_bytes=0, candidate_count=0) if verified.reason else None)
        rows: tuple[Row, ...] = ()
        reconcile: Row | None = None
        if comparison is not None and comparison.payload_hash is not None and ctx is not None:
            value = sample(ctx, comparison, observed_us=reservation.observed_us, router_us=capture.router_us or 0,
                           comparator_us=capture.comparator_us or 0, booking_us=capture.booking_us, instance=reservation.boot, revision=self.settings.source_revision,
                           admission={k: capture.prediction[k] for k in unknown()} if capture.prediction else None)
            value["timing"].update(router_settle_us=capture.router_us, comparator_us=capture.comparator_us)
            if verified.authorization_at_us is not None:
                value.update(authorization_day=event["authorization_day"], authorize_at_us=verified.authorization_at_us)
            value.update(v=2, policy_version=POLICY, proof_kind="compared",
                original_terminal_receipt=dict(day=reservation.day, boot=reservation.boot, event_seq=event["seq"]),
                health_source="shadow_observation", health_slot=prediction["health_slot"] if prediction else None,
                prediction_basis="counterfactual_backlog_v2")
            row = Row.build(SAMPLE_KIND, event["authorization_day"]+"/"+auth.id, value)
            old = self.original_sample(row)
            if old is not None:
                result = sample_retry(old.value(), value)
                event["retry_original"] = old.value()["original_terminal_receipt"]
                if result == "duplicate":
                    previous = old.value()
                    if (previous["booking"]["outcome"], previous["classification"]) != (value["booking"]["outcome"], value["classification"]):
                        # The immutable first sample cannot back an upgraded A
                        # attempt. Retain this attempt as C and append a separate
                        # original-class completion after its receipt is final.
                        result = "retry_original_gap"
                        if previous["booking"]["outcome"] in {"pending", "unknown"} and booking.confirmed:
                            reconcile = old
                        else:
                            self.port.lost.set()
                if result != "duplicate":
                    event.update(partition="C", proof_kind="none", diagnostic=dict(reason=result, required_bytes=0, candidate_count=0))
                    if result == "conflict":
                        with self.port.service.lock:
                            self.port.service.journal.days[reservation.day].resets += 1
            else:
                rows = (row,)
        raw = self.port.finalize(reservation, event, fold_owed=True)
        require(raw is not None, "unfinished_slot")
        capture.finalized = True
        if defer:
            self.defer_fold(reservation, event, rows, prediction)
        else:
            try:
                self.fold_complete(reservation, event, rows, prediction)
            except Exception:
                self.port.fail_fold(reservation)
                raise
        if reconcile is not None:
            self.reconcile_booking(reconcile, event)

    def reconcile_booking(self, backing: Row, attempt: dict[str, Any]) -> None:
        """Append P3's verified completion without rewriting either HTTP receipt."""
        value = backing.value()
        reference = value["original_terminal_receipt"]
        with self.port.service.lock:
            journal = self.port.service.journal
            journal.freeze(reference["day"])
            # Durable unknown-booking pages remain behind HWM but freeze()
            # returns only pages still needing upload. Include retained rows.
            pages = [page.row for page in journal.pages
                     if page.day == reference["day"] and page.row is not None]
            matches = [(page, event) for page in pages
                if page.value()["boot"] == reference["boot"]
                for event in page.value()["events"] if event["seq"] == reference["event_seq"]]
        require(len(matches) == 1, "retry_original_gap")
        original_page, original = matches[0]
        require(original["identity"] == attempt["identity"]
                and original["payload_sha256"] == attempt["payload_sha256"], "retry_identity")
        require(value["payload_hash"] == original["payload_sha256"]
                and value["classification"] == original["comparison"]
                and value["booking"]["outcome"] == original["booking"]["outcome"], "retry_backing")
        later = self.port.reserve(attempt["event_at_us"])
        require(later is not None, "unfinished_slot")
        assert later is not None
        completion = {**original, "phase": "completion", "seq": f"{later.event_seq:020}",
            "event_at_us": later.observed_us, "receipt_at_us": later.observed_us,
            "prediction": None, "completion_of": original["seq"],
            "retry_original": reference, "booking": attempt["booking"]}
        require(self.port.complete_event(later, original_page, completion) is not None, "unfinished_slot")
        require(self.port.complete(later), "unfinished_slot")

    def start(self) -> None:
        # Already prestarted by install, including applications without lifespan.
        pass

    def close(self) -> None:
        self.stopped = True

    def join(self) -> None:
        """Test/shutdown drain only; never called on the request path."""
        self.queue.join()



def install(app: Any, settings: Settings, backend: Any) -> None:
    service = getattr(app.state, "async_settle_shadow_observation", None)
    if service is None:
        return
    runtime = Runtime(settings, app.state.async_settle, service.port, EvidenceStore(backend._database),
                      typed=getattr(backend, "request_record_write_mode", "legacy") == "typed")
    app.state.async_settle_shadow_cohort = runtime
    app.router.add_event_handler("startup", runtime.start)
    app.router.add_event_handler("shutdown", runtime.close)


def authorize_http(request: Any, body: Any, auth: GatewayAuthorization, additions: dict[str, Any],
                   endpoints: list[Any], *, typed: bool, federated: bool, replay: bool) -> None:
    runtime = getattr(request.app.state, "async_settle_shadow_cohort", None)
    if runtime is not None:
        runtime.authorize_http(request, body, auth, additions, endpoints, typed=typed, federated=federated, replay=replay)


def terminal_http(request: Any, body: Any, auth: GatewayAuthorization, kind: str, callback: Callable[[], T]) -> T:
    runtime = getattr(request.app.state, "async_settle_shadow_cohort", None)
    if runtime is None:
        return callback()
    invoked = False
    outcome: list[T] = []
    failure: list[BaseException] = []
    def once() -> T:
        nonlocal invoked
        invoked = True
        try:
            result = callback()
            outcome.append(result)
            return result
        except BaseException as error:
            failure.append(error)
            raise
    try:
        return runtime.terminal(request, body, auth, kind, once)
    except Exception:
        runtime.port.lost.set()
        if failure:
            raise failure[0] from None
        if outcome:
            return outcome[0]
        if not invoked:
            return callback()
        raise
