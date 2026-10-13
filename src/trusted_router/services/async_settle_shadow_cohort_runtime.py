"""Opted-cohort HTTP observation; all verification and storage run off-request."""
from __future__ import annotations

import contextvars
import datetime as dt
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar, cast

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
from trusted_router.services.async_settle_shadow import Runtime as LegacyRuntime
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


def receipt_dimensions(adapter: str | None, route: str | None, streamed: bool | None) -> dict[str, Any]:
    """The v2 schema represents unobserved dimensions with null."""
    values = dimensions(adapter, route, streamed)
    return dict(zip(("adapter", "route_type", "streamed"),
                    (None if v == "unknown" else v for v in values), strict=True))


def fold_final_counts(port: Port, day: str, event: dict[str, Any]) -> bool:
    """P3 currently cannot fold nullable dimensions; retain its sticky loss."""
    try:
        return port.fold_counts(day, receipt_bucket(event))
    except (StopIteration, ValueError, KeyError):
        port.lost.set()
        return False


def authorization_identity(auth: GatewayAuthorization, runtime: Any, typed: bool) -> dict[str, Any]:
    return dict(authorization_id=auth.id, generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id, key_id=auth.key_hash, invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id, billing_authority=auth.settlement,
        journal_region=runtime.effective_journal_region, epoch=runtime.epoch,
        settle_origin="typed" if typed else "legacy")


def map_requested_facts(*, body: Any, authorization: GatewayAuthorization, endpoints: list[Any],
                        typed: bool, federated: bool, runtime: Any, revision: str,
                        observed_us: int) -> AuthorizeInput:
    """Ruling 5: one mapping of handler-owned facts and complete returned candidates.

    Missing members and unsupported feature representations are unresolved.
    Ordinary program facts follow the actual resolved authorization and route;
    adapter eligibility requires every candidate's supported pricing projection.
    """
    auth = authorization
    dims = receipt_dimensions(auth.provider, body.route_type, body.stream)
    identity = authorization_identity(auth, runtime, typed)
    facts = candidates = None
    complete = supported = False
    reason: str | None = "missing_context"
    try:
        require(body.route_type is not None and type(body.stream) is bool, "missing_context")
        require(body.requested_parameters is not None, "missing_context")
        require(not body.model_extra and "input_modalities" not in body.model_fields_set, "missing_context")
        ordinary = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
                    "stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias", "logprobs",
                    "top_logprobs", "response_format", "reasoning_effort", "stream"}
        require(not set(body.requested_parameters) - ordinary, "unknown_parameters")
        # A compound/alias route has no complete program witness at this seam.
        require(body.model == auth.model_id and not body.models, "missing_context")
        require(type(auth.additional_cost_reservation_microdollars) is int, "missing_context")
        requested = Eligibility(typed=typed, usage_type=auth.usage_type.value,
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
            project(tuple(endpoints), auth.created_at)
            supported = complete
        except (ValueError, TypeError, KeyError):
            supported = False
        candidates = canonical(projection)
        facts = RequestedFacts.from_object(dict(settle_origin="typed" if typed else "legacy",
            usage_class=requested.usage_type, billing_authority=requested.authority,
            route=requested.route_type, streamed=requested.streamed, service_tier=requested.service_tier,
            app_markup=requested.app_markup, custom_markup=requested.custom_markup,
            receipt_requested=body.inference_receipt, receipt_fee=requested.receipt_fee,
            commercial_program="partner" if requested.partner else "ordinary", features={k: bool(getattr(requested, k)) for k in FEATURES + DEPENDENT},
            unknown_features=[]))
        reason = exclusion(requested)
        if body.inference_receipt:
            reason = "receipt_fee"
        if reason is None and projection and all(p["adapter"] == "other" for p in projection):
            reason = "unsupported_adapter"
        if reason is None and projection and all("request_fee" in p["features"] for p in projection):
            reason = "request_fee"
    except (ValueError, AttributeError, TypeError, KeyError) as error:
        reason = "unknown_parameters" if str(error) == "unknown_parameters" else "missing_context"
    return AuthorizeInput(observed_us, identity, revision, dims, facts, candidates, complete, supported, reason)


def compact_terminal_facts(body: GatewaySettleRequest) -> GatewaySettleRequest:
    """Retain only bounded comparator inputs, never request extras or metadata."""
    names = ("authorization_id", "route_type", "streamed", "usage_estimated", "service_tier",
             "actual_input_tokens", "actual_output_tokens", "input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens",
             "additional_cost_microdollars")
    values = {name: getattr(body, name) for name in names}
    require(all(len(v) <= 128 for v in values.values() if isinstance(v, str)), "identity")
    require(all(0 <= v < 1 << 63 for v in values.values() if type(v) is int), "integer")
    return GatewaySettleRequest.model_construct(**values)


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
    return EVENT.parse(raw)


def receipt_prediction(capture: Capture, verified: VerifiedTerminal) -> dict[str, Any] | None:
    """No contract value represents an eligible receipt without a retained basis."""
    if verified.eligibility != "eligible":
        return None
    require(capture.prediction is not None, "prediction_demand_gap")
    assert capture.prediction is not None
    return {k: capture.prediction[k] for k in PREDICTION.fields}


def permit_exhaustion(port: Port) -> None:
    """P3 cannot reserve a C slot without a terminal permit; keep sticky loss."""
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


def retry_completion_backing(original: dict[str, Any], later: dict[str, Any]) -> None:
    """P3 cannot reconcile a changed booking/result against an immutable sample.

    Its A1 completion context accepts the later fact, but verify_complete still
    requires the original sample's booking outcome and comparison to match it.
    Keep both attempts' classes final; never overwrite the first sample.
    """
    require((original["booking"]["outcome"], original["classification"])
            == (later["booking"]["outcome"], later["classification"]), "retry_original_gap")


class Runtime:
    def __init__(self, settings: Settings, async_runtime: Any, port: Port, booking_store: Any, *, typed: bool) -> None:
        self.settings, self.async_runtime, self.port, self.booking_store = settings, async_runtime, port, booking_store
        self.typed = typed
        self.signer = ShadowSigner(async_runtime.signer) if async_runtime.signer else None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-v2-compare")
        self.guard = self.lock = threading.RLock()
        # Reserve the bounded replay-decision cache inside the shared byte cap.
        self.tokens, self.refilled, self.pending, self.queued_bytes = 10., time.monotonic(), 0, 32*4096
        self.stopped = False
        self.decisions: dict[str, bytes] = {}

    def authorize_http(self, request: Any, body: Any, auth: GatewayAuthorization, additions: dict[str, Any],
                       endpoints: list[Any], *, typed: bool, federated: bool, replay: bool) -> None:
        try:
            observed_us = time.time_ns()//1000
            capability = tuple(request.headers.getlist("X-TR-Settlement-Shadow-Policy"))
            original = None
            if replay and has_v2_capability(capability):
                try:
                    original = retained_replay_decision(self.decisions, auth.id)
                except ValueError:
                    # The replay still receives its own unresolved receipt below.
                    pass
            if replay or not has_v2_capability(capability):
                inputs = AuthorizeInput(observed_us, authorization_identity(auth, self.async_runtime, typed),
                    self.settings.source_revision, receipt_dimensions(auth.provider, body.route_type, body.stream), None, None, False, False,
                    "replay" if has_v2_capability(capability) else "header_absent")
            else:
                inputs = map_requested_facts(body=body, authorization=auth, endpoints=endpoints,
                    typed=typed, federated=federated, runtime=self.async_runtime,
                    revision=self.settings.source_revision, observed_us=observed_us)
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
                original_decision=original, original_locator=original["receipt_locator"] if original else None,
                finalized_event=retain if not replay else None)
            additions.update(compared_handoff_fields(fields, auth))
        except Exception:
            self.port.lost.set()

    def admit(self, size: int) -> str | None:
        if not self.guard.acquire(blocking=False):
            return "queue_full"
        try:
            if self.stopped:
                return "queue_full"
            # Reuse the existing rate/burst/task/byte guard. Its inner RLock is
            # already owned, so the request thread never waits for contention.
            return LegacyRuntime.admit(cast(Any, self), size)
        finally:
            self.guard.release()

    def capture_terminal(self, request: Any, body: Any, auth: GatewayAuthorization, kind: str) -> tuple[Capture | None, int]:
        """Ruling 4: reserve once, retain bounded inputs; no parser or booking read."""
        reservation = self.port.reserve(time.time_ns()//1000, terminal=True)
        if reservation is None:
            permit_exhaustion(self.port)
            return None, 0
        size = (2*(HEADER_BYTES+1) + 8192 + sum(len(v) for v in auth.candidate_endpoint_ids) + 4*len(auth.pricing_snapshot or "")
                if len(auth.candidate_endpoint_ids) <= 128 else 256*1024+1)
        refused = self.admit(size)
        if refused:
            capture = Capture(reservation, auth, body, kind, (), None)
            self.finish(capture, VerifiedTerminal("C", "unknown", "none", refused), Booking(), None)
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
            self.finish(capture, VerifiedTerminal("C", "unknown", "none", "local_work_overflow"), Booking(), None)
            return None, 0
        # Reserve prediction basis at receipt time, even if comparison/booking later fails.
        try:
            capture.prediction = self.port.capture_prediction(auth.workspace_id, reservation.observed_us)
        except Exception:
            self.release(size)
            raise
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
                try:
                    self.executor.submit(self.work, capture, size)
                except Exception:
                    self.port.lost.set()
                    self.release(size)

    def release(self, size: int) -> None:
        with self.guard:
            self.pending -= 1
            self.queued_bytes -= size

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

    def finish(self, capture: Capture, verified: VerifiedTerminal, booking: Booking, ctx: Context | None) -> None:
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
                    try:
                        retry_completion_backing(old.value(), value)
                    except ValueError:
                        result = "retry_original_gap"
                        self.port.lost.set()
                if result != "duplicate":
                    event.update(partition="C", proof_kind="none", diagnostic=dict(reason=result, required_bytes=0, candidate_count=0))
                    if result == "conflict":
                        with self.port.service.lock:
                            self.port.service.journal.days[reservation.day].resets += 1
            else:
                rows = (row,)
        EVENT(event)
        raw = self.port.finalize(reservation, event)
        require(raw is not None, "unfinished_slot")
        capture.finalized = True
        require(fold_final_counts(self.port, reservation.day, event), "counter_overflow")
        if prediction is not None:
            with self.port.service.lock:
                self.port.service.counters[reservation.day]["admission_observer"]["prediction_"+prediction["prediction"]] += 1
        self.port.complete(reservation, rows)

    def start(self) -> None:
        # Start the dedicated worker during lifespan startup, before HTTP traffic.
        self.executor.submit(lambda: None)

    def close(self) -> None:
        self.stopped = True
        self.executor.shutdown(wait=False, cancel_futures=False)


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
