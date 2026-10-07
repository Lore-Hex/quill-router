"""Pure post-outcome comparison; no storage, admission, or money-writing APIs."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from trusted_router import billing_snapshot as b
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_projection import snapshot_material
from trusted_router.async_settle_shadow_wire import CANDIDATES, LOCAL_BYTES, Envelope, parse_header
from trusted_router.detached_jws import TrustedKey
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.services.settle_outbox_apply import normalized_prompt_accounting
from trusted_router.stage_d import endpoint_cost_microdollars_from_candidate
from trusted_router.storage_models import GatewayAuthorization, generation_id_for_authorization


@dataclass(frozen=True)
class Booking:
    amount: int | None = None
    outcome: str = "unknown"
    confirmed: bool = False

    @property
    def kind(self) -> str | None:
        return {"settled": "settle", "refunded": "refund"}.get(self.outcome)


@dataclass(frozen=True)
class Context:
    authorization: GatewayAuthorization
    body: GatewaySettleRequest
    attempted_kind: str
    selected_endpoint: str | None
    region: str
    epoch: int
    received_at: int
    booking: Booking
    # Rebuild uses only detached request-time catalog/price references.
    rebuild: Callable[[], b.BillingSnapshot] | None = None
    rebuild_matches_booking_view: bool = False
    price_source: str = "unknown"


@dataclass
class Comparison:
    adapter: str | None = None
    model_id: str | None = None
    classification: str = "unevaluable"
    reasons: set[str] = field(default_factory=set)
    snapshot_hash: str | None = None
    payload_hash: str | None = None
    rebuilt_snapshot_hash: str | None = None
    raw_usage: dict[str, int] | None = None
    python_usage: dict[str, int] | None = None
    go_usage: dict[str, int] | None = None
    legacy_usage: dict[str, int] | None = None
    python_micro: int | None = None
    go_micro: int | None = None
    booked_micro: int | None = None
    rebuilt_micro: int | None = None
    legacy_frozen_micro: int | None = None
    python_minus_go: int | None = None
    booked_minus_frozen: int | None = None
    rebuilt_minus_frozen: int | None = None
    booked_minus_rebuilt: int | None = None
    binding_verified: bool = False
    raw_matches_body: bool = False
    snapshot_transport: str = "unknown"
    s0_reconstruction: str = "not_attempted"
    observed_eligible: bool | None = None
    go_revision: str | None = None
    handoff_prepare_us: int | None = None

    def reject(self, reason: str | None, classification: str = "unevaluable") -> Comparison:
        self.classification = classification
        if reason is not None:
            self.reasons.add(reason)
        return self


def legacy_oracle(candidate: b.Candidate, body: GatewaySettleRequest, kind: str) -> tuple[int, dict[str, int]]:
    """Copy ALL S0 pricing fields into Stage D; normalize independently of P."""
    if candidate.prompt_convention != ("excludes_cache" if candidate.provider == "anthropic" else "includes_cache"):
        raise ValueError("legacy_oracle_unavailable")
    uncached, total, read, creation = normalized_prompt_accounting(candidate.provider, body)
    usage = dict(uncached_input_tokens=uncached, total_prompt_tokens=total,
                 output_tokens=body.output_count, cache_read_tokens=read,
                 cache_creation_tokens=creation, reasoning_tokens=body.reasoning_tokens or 0)
    projection = candidate.model_dump(mode="json", include={
        "endpoint_id", "price_history_version", "rates", "tiers", "request_fee_micro", "rounding"})
    amount = endpoint_cost_microdollars_from_candidate(
        projection, uncached, body.output_count, cache_read_tokens=read,
        cache_creation_tokens=creation)
    if type(amount) is not int or not 0 <= amount < 1 << 63:
        raise ValueError("arithmetic_overflow")
    return (amount if kind == "settle" else 0), usage


def _raw_body(body: GatewaySettleRequest) -> b.RawUsage | None:
    if (body.actual_input_tokens is None and body.input_tokens is None
            or body.actual_output_tokens is None and body.output_tokens is None):
        return None
    return b.RawUsage(input_tokens=body.input_count, output_tokens=body.output_count,
                      cache_read_tokens=body.cache_read_count,
                      cache_creation_tokens=body.cache_creation_count,
                      reasoning_tokens=body.reasoning_tokens or 0)


def price_only(old: b.BillingSnapshot, new: b.BillingSnapshot, selected: str) -> bool:
    if len(old.candidates) != len(new.candidates):
        return False
    selected_changed = False
    for a, z in zip(old.candidates, new.candidates, strict=True):
        if a.model_dump(exclude={"rates", "tiers"}) != z.model_dump(exclude={"rates", "tiers"}):
            return False
        if tuple(t.max_prompt_tokens for t in a.tiers) != tuple(t.max_prompt_tokens for t in z.tiers):
            return False
        if a.endpoint_id == selected:
            selected_changed = a.rates != z.rates or a.tiers != z.tiers
    return selected_changed


def compare(headers: tuple[str, ...], context: Context, keys: Sequence[TrustedKey]) -> Comparison:
    result = Comparison(booked_micro=context.booking.amount)
    try:
        envelope = parse_header(headers)
        return _compare(envelope, context, keys, result)
    except ValueError as exc:
        reason = str(exc)
        allowed = {"header_duplicate", "header_size", "base64", "json_encoding", "json_duplicate",
                   "json_shape", "integer", "proof_signature", "proof_expired", "hash", "identity",
                   "raw_usage", "go_failure", "snapshot_size", "missing_envelope"}
        reason = reason if reason in allowed else "worker_error"
        return result.reject(reason, "hash" if reason in {"proof_signature", "hash"} else "identity" if reason == "identity" else "unevaluable")


def _compare(envelope: Envelope, ctx: Context, keys: Sequence[TrustedKey], out: Comparison) -> Comparison:
    claims = verify_binding(envelope.proof, keys, ctx.received_at)
    out.binding_verified = True
    out.go_revision = envelope.go_revision
    out.handoff_prepare_us = envelope.handoff_prepare_us
    auth, body, terminal = ctx.authorization, ctx.body, envelope.terminal
    # Hash/signature disagreement precedes identity disagreement, even when
    # an adversarial envelope contains both. Hash-only reconstruction failure
    # remains the lower-priority coverage outcome specified separately.
    if envelope.snapshot is not None:
        out.snapshot_hash = snapshot_material(envelope.snapshot)[1]
        if out.snapshot_hash != claims.snapshot_hash:
            return out.reject("hash", "hash")
    if terminal is not None:
        if terminal.snapshot_hash != claims.snapshot_hash or b.canonical_hash(terminal) != envelope.payload_hash:
            return out.reject("hash", "hash")
    expected = dict(authorization_id=auth.id, generation_id=generation_id_for_authorization(auth.id),
                    workspace_id=auth.workspace_id, key_id=auth.key_hash,
                    invocation_nonce=auth.invocation_nonce, reservation_id=auth.credit_reservation_id,
                    billing_authority=auth.settlement, journal_region=ctx.region, epoch=ctx.epoch,
                    route_type=body.route_type, streamed=body.streamed)
    # A usage-less refund still has the proof's route, but cannot be evaluable.
    if ctx.attempted_kind == "refund" and body.route_type is None:
        expected["route_type"] = claims.route_type
    if any(getattr(claims, key) != value for key, value in expected.items()):
        return out.reject("identity", "identity")
    if ctx.selected_endpoint not in auth.candidate_endpoint_ids and ctx.selected_endpoint != auth.endpoint_id:
        return out.reject("identity", "identity")
    if terminal is not None:
        if (terminal.terminal_kind != ctx.attempted_kind or terminal.selected_endpoint != ctx.selected_endpoint
                or any(getattr(terminal, key) != getattr(claims, key) for key in expected if key != "reservation_id")):
            return out.reject("identity", "identity")
    snapshot = envelope.snapshot
    rebuilt = None
    out.snapshot_transport = "full" if snapshot is not None else "hash_only"
    out.s0_reconstruction = "not_needed" if snapshot is not None else "failed"
    if snapshot is None:
        try:
            if ctx.rebuild is None:
                raise ValueError("missing rebuild")
            rebuilt = snapshot = ctx.rebuild()
            snapshot_bytes, snapshot_hash = snapshot_material(snapshot)
            if (len(snapshot.candidates) > CANDIDATES or len(snapshot_bytes) > LOCAL_BYTES
                    or snapshot_hash != claims.snapshot_hash):
                raise ValueError("snapshot hash")
        except Exception:
            return out.reject("snapshot_reconstruction_failed")
        out.s0_reconstruction = "verified"
    out.snapshot_hash = out.snapshot_hash or snapshot_material(snapshot)[1]
    if out.snapshot_hash != claims.snapshot_hash:
        return out.reject("hash", "hash")
    if terminal is not None:
        out.payload_hash = envelope.payload_hash
        out.go_micro, out.go_usage = terminal.charge_micro, terminal.usage.model_dump()
    raw = _raw_body(body)
    if raw is None:
        return out.reject("usage_missing")
    out.raw_usage = raw.model_dump()
    if envelope.raw != raw:
        return out.reject("raw_usage", "normalization")
    out.raw_matches_body = True
    if body.usage_estimated:
        return out.reject("usage_estimated")
    observed_exclusion = b.exclusion(envelope.observed)
    out.observed_eligible = observed_exclusion is None
    if (envelope.observed.route_type != claims.route_type or envelope.observed.streamed != claims.streamed
            or body.service_tier not in (None, "default") or body.additional_cost_microdollars):
        return out.reject("identity", "identity")
    if observed_exclusion:
        return out.reject(observed_exclusion)
    selected = next((c for c in snapshot.candidates if c.endpoint_id == ctx.selected_endpoint), None)
    if selected is None:
        return out.reject("identity", "identity")
    out.adapter, out.model_id = selected.provider, selected.model_id
    try:
        evaluated = b.evaluate(snapshot, selected.endpoint_id, raw, envelope.observed)
        out.python_micro = evaluated.charge_micro if ctx.attempted_kind == "settle" else 0
        out.python_usage = evaluated.usage.model_dump()
    except ValueError as exc:
        if str(exc) == "arithmetic_overflow":
            return out.reject("arithmetic_overflow")
        return out.reject("malformed_usage", "normalization")
    try:
        out.legacy_frozen_micro, out.legacy_usage = legacy_oracle(selected, body, ctx.attempted_kind)
    except Exception:
        out.reasons.add("legacy_oracle_unavailable")
    if (out.legacy_usage is not None and out.legacy_usage != out.python_usage
            or terminal is not None and out.go_usage != out.python_usage):
        return out.reject("raw_usage", "normalization")
    if out.go_micro is not None:
        out.python_minus_go = out.python_micro - out.go_micro
    if terminal is None or out.python_micro != out.go_micro or out.legacy_frozen_micro not in (None, out.python_micro):
        return out.reject("go_failure" if terminal is None or out.python_micro != out.go_micro else None, "evaluator_disagreement")
    try:
        b.validate_envelope(snapshot, terminal)
    except ValueError:
        return out.reject(None, "evaluator_disagreement")
    if ctx.booking.kind is not None and ctx.booking.kind != ctx.attempted_kind:
        return out.reject("winner_polarity", "requires_review")
    if not ctx.booking.confirmed or ctx.booking.amount is None or ctx.booking.kind is None:
        return out.reject("booking_pending" if ctx.booking.outcome == "pending" else "booking_unknown")
    out.booked_minus_frozen = ctx.booking.amount - out.python_micro
    try:
        if rebuilt is None and ctx.rebuild is not None:
            rebuilt = ctx.rebuild()
        if rebuilt is not None:
            out.rebuilt_snapshot_hash = snapshot_material(rebuilt)[1]
            reval = b.evaluate(rebuilt, selected.endpoint_id, raw, envelope.observed)
            out.rebuilt_micro = reval.charge_micro if ctx.attempted_kind == "settle" else 0
            out.rebuilt_minus_frozen = out.rebuilt_micro - out.python_micro
            out.booked_minus_rebuilt = ctx.booking.amount - out.rebuilt_micro
    except Exception:
        out.reasons.add("rebuild_unavailable")
    if out.legacy_frozen_micro is None:
        return out.reject("legacy_oracle_unavailable", "requires_review")
    if ctx.booking.amount == out.python_micro:
        out.classification = "exact"
        return out
    if rebuilt is None or out.rebuilt_micro is None or not ctx.rebuild_matches_booking_view:
        return out.reject("rebuild_unavailable")
    if (ctx.attempted_kind == "settle" and ctx.booking.amount == out.rebuilt_micro
            and out.snapshot_hash != out.rebuilt_snapshot_hash and price_only(snapshot, rebuilt, selected.endpoint_id)):
        return out.reject("catalog_change", "explained-by-catalog-change")
    return out.reject(None, "evaluator_disagreement")
