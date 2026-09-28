"""Dormant billing v1 contract; no gateway imports or activation side effects.

Callers supply effective customer-priced endpoints and requested/observed feature
facts. No catalog lookup occurs during evaluation. See docs/async-settlement-billing-v1.md
for the byte contract, checked-int64 domain and last-tier fallback decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.stage_d import endpoint_pricing_candidate

MAX_INT = (1 << 63) - 1
UInt = Annotated[int, Field(strict=True, ge=0, le=MAX_INT)]
Identity = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_./:@+\-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SettlementMode = Literal["sync", "async"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def integer_versions(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in ("v", "price_history_version", "snapshot_version"):
                if name in value and type(value[name]) is not int:
                    raise ValueError("unknown price version")
        return value


class Eligibility(Frozen):
    """Facts from ONE phase; check both requested and observed facts.

    These defaults describe ordinary typed Credits. Integration must populate
    every nonordinary fact; this type is not an authorization or a ticket.
    Unknown fields fail closed instead of silently omitting a new fee layer.
    """

    typed: Annotated[bool, Field(strict=True)] = True
    usage_type: str = "Credits"
    authority: str = "local"
    route_type: str = "chat.completions"
    streamed: Annotated[bool, Field(strict=True)] = False
    service_tier: str | None = None
    app_markup: UInt = 0
    custom_markup: UInt = 0
    receipt_fee: UInt = 0
    request_fee: UInt = 0
    custom_model: Annotated[bool, Field(strict=True)] = False
    user_model: Annotated[bool, Field(strict=True)] = False
    tool_cost: Annotated[bool, Field(strict=True)] = False
    search_cost: Annotated[bool, Field(strict=True)] = False
    image_cost: Annotated[bool, Field(strict=True)] = False
    video_cost: Annotated[bool, Field(strict=True)] = False
    partner: Annotated[bool, Field(strict=True)] = False
    liberty: Annotated[bool, Field(strict=True)] = False
    native_batch: Annotated[bool, Field(strict=True)] = False
    fusion: Annotated[bool, Field(strict=True)] = False
    polyphemus: Annotated[bool, Field(strict=True)] = False
    private_tier_basis: Annotated[bool, Field(strict=True)] = False


def exclusion(context: Eligibility) -> str | None:
    if not context.typed:
        return "untyped"
    if context.usage_type != "Credits":
        return "non_credits"
    if context.authority != "local":
        return "settlement_authority"
    if context.route_type not in ("chat.completions", "responses"):
        return "unsupported_route"
    if context.service_tier not in (None, "default"):
        return "service_tier"
    for name in (
        "app_markup", "custom_markup", "receipt_fee", "request_fee", "custom_model",
        "user_model", "tool_cost", "search_cost", "image_cost", "video_cost", "partner",
        "liberty", "native_batch", "fusion", "polyphemus", "private_tier_basis",
    ):
        if getattr(context, name):
            return name
    return None


def require_eligible(context: Eligibility) -> None:
    reason = exclusion(context)
    if reason is not None:
        raise ValueError(reason)


class Rates(Frozen):
    input_micro_per_million: UInt
    cached_input_micro_per_million: UInt
    cache_creation_micro_per_million: UInt
    output_micro_per_million: UInt


class Tier(Frozen):
    max_prompt_tokens: UInt | None
    rates: Rates


class Candidate(Frozen):
    endpoint_id: Identity
    provider: Literal["openai", "anthropic"]
    model_id: Identity
    usage_type: Literal["Credits"]
    price_history_version: Literal[1]
    rates: Rates
    tiers: tuple[Tier, ...] = Field(max_length=64)
    request_fee_micro: UInt
    rounding: Literal["half_up_per_million"]
    prompt_convention: Literal["includes_cache", "excludes_cache"]
    output_convention: Literal["includes_reasoning"]

    @model_validator(mode="after")
    def supported(self) -> Candidate:
        if self.request_fee_micro != 0:
            raise ValueError("request_fee")
        if self.prompt_convention != (
            "excludes_cache" if self.provider == "anthropic" else "includes_cache"
        ):
            raise ValueError("unsupported prompt convention for adapter")
        if not self.model_id.startswith(self.provider + "/"):
            raise ValueError("custom or unsupported model")
        previous = -1
        for index, tier in enumerate(self.tiers):
            maximum = tier.max_prompt_tokens
            if maximum is None:
                if index != len(self.tiers) - 1:
                    raise ValueError("unbounded tier must be last")
            elif maximum <= previous:
                raise ValueError("tier boundaries must increase")
            else:
                previous = maximum
        return self


class BillingSnapshot(Frozen):
    v: Literal[1]
    kind: Literal["credits_endpoint"]
    candidates: tuple[Candidate, ...] = Field(min_length=1, max_length=64)
    minimum_charge: Literal["one_micro_if_positive"]
    charge_cap: None
    tier_basis: Literal["total_prompt"]
    tier_boundary: Literal["inclusive"]
    tier_fallback: Literal["last_tier"]

    @model_validator(mode="after")
    def unique_candidates(self) -> BillingSnapshot:
        ids = [c.endpoint_id for c in self.candidates]
        if ids != sorted(set(ids)):
            raise ValueError("candidates must be unique and sorted by endpoint_id")
        return self


class RawUsage(Frozen):
    input_tokens: UInt
    output_tokens: UInt
    cache_read_tokens: UInt = 0
    cache_creation_tokens: UInt = 0
    reasoning_tokens: UInt = 0


class NormalizedUsage(Frozen):
    uncached_input_tokens: UInt
    total_prompt_tokens: UInt
    output_tokens: UInt
    cache_read_tokens: UInt
    cache_creation_tokens: UInt
    reasoning_tokens: UInt

    @model_validator(mode="after")
    def consistent(self) -> NormalizedUsage:
        if self.total_prompt_tokens != checked(
            self.uncached_input_tokens + self.cache_read_tokens + self.cache_creation_tokens
        ) or self.reasoning_tokens > self.output_tokens:
            raise ValueError("malformed_usage")
        return self


class Evaluation(Frozen):
    usage: NormalizedUsage
    charge_micro: UInt


class TerminalEnvelope(Frozen):
    v: Literal[1]
    authorization_id: Identity
    generation_id: Identity
    workspace_id: Identity
    key_id: Identity
    invocation_nonce: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    billing_authority: Literal["local"]
    journal_region: Identity
    epoch: UInt
    selected_endpoint: Identity
    snapshot_version: Literal[1]
    snapshot_hash: Digest
    usage: NormalizedUsage
    charge_micro: UInt
    terminal_kind: Literal["settle", "refund"]
    route_type: Literal["chat.completions", "responses"]
    streamed: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def refund_is_free(self) -> TerminalEnvelope:
        if self.terminal_kind == "refund" and self.charge_micro != 0:
            raise ValueError("refund must be zero")
        return self


class AcceptanceStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    SYNC_REQUIRED = "sync_required"
    CONFLICT = "conflict"
    INVALID = "invalid"


class AcceptanceOutcome(Frozen):
    """Durable acceptance is pending, never evidence of ledger finalization."""

    status: AcceptanceStatus
    payload_hash: Digest | None = None
    settlement_status: Literal["pending"] | None = None

    @model_validator(mode="after")
    def pending_only_when_durable(self) -> AcceptanceOutcome:
        durable = self.status in (AcceptanceStatus.ACCEPTED, AcceptanceStatus.DUPLICATE)
        if durable != (self.payload_hash is not None and self.settlement_status == "pending"):
            raise ValueError("durable outcome requires hash and pending status")
        if not durable and (self.payload_hash is not None or self.settlement_status is not None):
            raise ValueError("rejection cannot acknowledge durability")
        return self


def checked(value: int) -> int:
    if value < 0 or value > MAX_INT:
        raise ValueError("arithmetic_overflow")
    return value


def canonical_bytes(value: Frozen) -> bytes:
    """ASCII JSON, lexical keys, compact separators, no newline/hash self-field."""
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("ascii")


def canonical_hash(value: Frozen) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_snapshot(raw: bytes | str) -> BillingSnapshot:
    data = json.loads(raw, object_pairs_hook=_unique_object)
    return BillingSnapshot.model_validate(data)


def build_snapshot(endpoints: Iterable[ModelEndpoint], requested: Eligibility) -> BillingSnapshot:
    """Freeze effective endpoints, resolving missing cache rates NOW, once.

    Catalog markup already embedded in customer rates is retained. The initial
    normalization allowlist is OpenAI and Anthropic; adding an adapter requires
    final-usage differential coverage. No mutable catalog is consulted here.
    """
    require_eligible(requested)
    candidates = []
    for endpoint in endpoints:
        integer = TypeAdapter(UInt)
        for rate in (endpoint.prompt_price_microdollars_per_million_tokens,
                     endpoint.completion_price_microdollars_per_million_tokens,
                     endpoint.request_price_microdollars):
            integer.validate_python(rate)
        for tier in endpoint.price_tiers:
            for value in (tier.max_prompt_tokens,
                          tier.prompt_price_microdollars_per_million_tokens,
                          tier.completion_price_microdollars_per_million_tokens,
                          tier.prompt_cached_price_microdollars_per_million_tokens):
                if value is not None:
                    integer.validate_python(value)
        price = endpoint_pricing_candidate(endpoint)
        candidates.append(Candidate.model_validate({
            **price,
            "provider": endpoint.provider,
            "model_id": endpoint.model_id,
            "usage_type": endpoint.usage_type,
            "prompt_convention": (
                "excludes_cache" if endpoint.provider == "anthropic" else "includes_cache"
            ),
            "output_convention": "includes_reasoning",
        }))
    return BillingSnapshot(
        v=1, kind="credits_endpoint",
        candidates=tuple(sorted(candidates, key=lambda c: c.endpoint_id)),
        minimum_charge="one_micro_if_positive", charge_cap=None,
        tier_basis="total_prompt", tier_boundary="inclusive", tier_fallback="last_tier",
    )


def evaluate(
    snapshot: BillingSnapshot, selected_endpoint: str, raw: RawUsage, observed: Eligibility,
) -> Evaluation:
    require_eligible(observed)
    candidate = next((c for c in snapshot.candidates if c.endpoint_id == selected_endpoint), None)
    if candidate is None:
        raise ValueError("unsupported_endpoint")
    cached = checked(raw.cache_read_tokens + raw.cache_creation_tokens)
    if candidate.prompt_convention == "includes_cache":
        if cached > raw.input_tokens:
            raise ValueError("malformed_usage")
        uncached = raw.input_tokens - cached
        total = raw.input_tokens
    else:
        uncached = raw.input_tokens
        total = checked(uncached + cached)
    usage = NormalizedUsage(
        uncached_input_tokens=uncached, total_prompt_tokens=total,
        output_tokens=raw.output_tokens, cache_read_tokens=raw.cache_read_tokens,
        cache_creation_tokens=raw.cache_creation_tokens, reasoning_tokens=raw.reasoning_tokens,
    )
    rates = candidate.rates
    if candidate.tiers:
        rates = candidate.tiers[-1].rates
        for tier in candidate.tiers:
            if tier.max_prompt_tokens is None or total <= tier.max_prompt_tokens:
                rates = tier.rates
                break
    components = (
        (uncached, rates.input_micro_per_million),
        (raw.cache_read_tokens, rates.cached_input_micro_per_million),
        (raw.cache_creation_tokens, rates.cache_creation_micro_per_million),
        (raw.output_tokens, rates.output_micro_per_million),
    )
    cost = candidate.request_fee_micro
    positive = cost > 0
    for tokens, rate in components:
        product = checked(tokens * rate)
        cost = checked(cost + checked(product + 500_000) // 1_000_000)
        positive = positive or (tokens > 0 and rate > 0)
    return Evaluation(usage=usage, charge_micro=max(cost, 1) if positive else 0)


def validate_envelope(snapshot: BillingSnapshot, envelope: TerminalEnvelope) -> None:
    """Verify charge/hash against frozen inputs; ticket/identity checks are PR 3+."""
    if envelope.snapshot_hash != canonical_hash(snapshot):
        raise ValueError("snapshot_hash_mismatch")
    candidate = next(
        (c for c in snapshot.candidates if c.endpoint_id == envelope.selected_endpoint), None
    )
    if candidate is None:
        raise ValueError("unsupported_endpoint")
    usage = envelope.usage
    raw = RawUsage(
        input_tokens=(usage.total_prompt_tokens if candidate.prompt_convention == "includes_cache"
                      else usage.uncached_input_tokens),
        output_tokens=usage.output_tokens, cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens, reasoning_tokens=usage.reasoning_tokens,
    )
    evaluated = evaluate(snapshot, envelope.selected_endpoint, raw, Eligibility(
        route_type=envelope.route_type, streamed=envelope.streamed,
    ))
    expected = evaluated.charge_micro if envelope.terminal_kind == "settle" else 0
    if envelope.charge_micro != expected or envelope.usage != evaluated.usage:
        raise ValueError("charge_mismatch")
