from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from trusted_router.money import microdollars_to_float
from trusted_router.types import UsageType


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso_now() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def _is_byok(usage_type: str | UsageType) -> bool:
    """True iff `usage_type` represents BYOK billing.

    Accepts either a `UsageType` enum or a raw string from JSON round-trip.
    """
    return UsageType.coerce(usage_type).is_byok()


def _is_expired(expires_at: str | None) -> bool:
    """Treat unparseable ISO timestamps as already expired so a malformed
    cookie can't replay forever."""
    if not expires_at:
        return False
    try:
        parsed = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed <= utcnow()


@dataclass
class User:
    id: str
    email: str | None
    created_at: str = field(default_factory=iso_now)
    email_verified: bool = False
    wallet_address: str | None = None


@dataclass
class Workspace:
    id: str
    name: str
    owner_user_id: str
    created_at: str = field(default_factory=iso_now)
    deleted: bool = False
    content_storage_enabled: bool = False


@dataclass
class Member:
    workspace_id: str
    user_id: str
    role: str
    created_at: str = field(default_factory=iso_now)


@dataclass
class ApiKey:
    hash: str
    salt: str
    secret_hash: str
    lookup_hash: str
    name: str
    label: str
    workspace_id: str
    creator_user_id: str | None
    disabled: bool = False
    management: bool = False
    limit_microdollars: int | None = None
    limit_reset: str | None = None
    include_byok_in_limit: bool = True
    usage_microdollars: int = 0
    byok_usage_microdollars: int = 0
    expires_at: str | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None
    reserved_microdollars: int = 0


@dataclass
class EncryptedSecretEnvelope:
    algorithm: str
    key_ref: str
    encrypted_dek: str
    dek_nonce: str
    ciphertext: str
    nonce: str


@dataclass
class ByokProviderConfig:
    workspace_id: str
    provider: str
    secret_ref: str
    key_hint: str | None = None
    encrypted_secret: EncryptedSecretEnvelope | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.encrypted_secret, dict):
            self.encrypted_secret = EncryptedSecretEnvelope(**self.encrypted_secret)


@dataclass
class BroadcastDestination:
    id: str
    workspace_id: str
    type: str
    name: str
    endpoint: str
    enabled: bool = True
    include_content: bool = False
    method: str = "POST"
    encrypted_api_key: EncryptedSecretEnvelope | None = None
    encrypted_headers: EncryptedSecretEnvelope | None = None
    header_names: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.encrypted_api_key, dict):
            self.encrypted_api_key = EncryptedSecretEnvelope(**self.encrypted_api_key)
        if isinstance(self.encrypted_headers, dict):
            self.encrypted_headers = EncryptedSecretEnvelope(**self.encrypted_headers)


@dataclass
class BroadcastDeliveryJob:
    id: str
    workspace_id: str
    destination_id: str
    generation_id: str
    settle_body: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: str = field(default_factory=iso_now)
    last_error: str | None = None
    lease_owner: str | None = None
    leased_until: str | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None


@dataclass
class CreditAccount:
    workspace_id: str
    total_credits_microdollars: int = 0
    total_usage_microdollars: int = 0
    reserved_microdollars: int = 0
    # Auto-refill: when available drops below threshold, charge the saved
    # Stripe payment method off-session for `auto_refill_amount_microdollars`.
    # All four are required to be non-zero/non-None for auto-refill to fire.
    auto_refill_enabled: bool = False
    auto_refill_threshold_microdollars: int = 0
    auto_refill_amount_microdollars: int = 0
    stripe_customer_id: str | None = None
    stripe_payment_method_id: str | None = None
    # Last fired-at timestamp + outcome — kept so we can rate-limit retries
    # and surface a helpful error if the saved card declines.
    last_auto_refill_at: str | None = None
    last_auto_refill_status: str | None = None  # "succeeded" | "failed:<code>" | "pending"


@dataclass
class Reservation:
    id: str
    workspace_id: str
    key_hash: str
    amount_microdollars: int
    settled: bool = False
    created_at: str = field(default_factory=iso_now)
    # Caller-supplied idempotency key. When `reserve()` is invoked twice
    # with the same key, the second call returns the existing reservation
    # without applying the credit hold a second time. Required for safe
    # dual-write across two Spanner instances (Stage 5a) and for safe
    # change-stream replay (Stage 1 zero-downtime migration). The
    # gateway-authorize handler uses the pre-generated authorization_id
    # as the natural key. Optional + nullable for back-compat with
    # callers that haven't been updated yet — those keep the pre-existing
    # non-idempotent semantics.
    idempotency_key: str | None = None


@dataclass
class GatewayAuthorization:
    id: str
    workspace_id: str
    key_hash: str
    model_id: str
    provider: str
    usage_type: UsageType
    estimated_microdollars: int
    credit_reservation_id: str | None = None
    settled: bool = False
    created_at: str = field(default_factory=iso_now)
    requested_model_id: str | None = None
    candidate_model_ids: list[str] = field(default_factory=list)
    region: str | None = None
    endpoint_id: str | None = None
    candidate_endpoint_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # JSON round-trip stores usage_type as a string; coerce so the field
        # is always a UsageType at runtime regardless of construction path.
        if not isinstance(self.usage_type, UsageType):
            self.usage_type = UsageType.coerce(self.usage_type)


@dataclass
class Generation:
    id: str
    request_id: str
    workspace_id: str
    key_hash: str
    model: str
    provider_name: str
    app: str
    tokens_prompt: int
    tokens_completion: int
    total_cost_microdollars: int
    usage_type: UsageType
    speed_tokens_per_second: float
    finish_reason: str
    status: str
    streamed: bool
    usage_estimated: bool = True
    created_at: str = field(default_factory=iso_now)
    provider: str | None = None
    elapsed_milliseconds: int | None = None
    first_token_milliseconds: int | None = None
    region: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.usage_type, UsageType):
            self.usage_type = UsageType.coerce(self.usage_type)

    @classmethod
    def from_chat_result(
        cls,
        *,
        result: Any,
        workspace_id: str,
        key_hash: str,
        model_id: str,
        app_name: str,
        actual_cost_microdollars: int,
        usage_type: UsageType | str,
        streamed: bool,
        provider: str | None = None,
        region: str | None = None,
    ) -> Generation:
        elapsed_ms = _seconds_to_milliseconds(getattr(result, "elapsed_seconds", 0.001))
        first_token_seconds = getattr(result, "first_token_seconds", None)
        return cls(
            id=f"gen-{uuid.uuid4().hex}",
            request_id=result.request_id,
            workspace_id=workspace_id,
            key_hash=key_hash,
            model=model_id,
            provider_name=result.provider_name,
            app=app_name,
            tokens_prompt=result.input_tokens,
            tokens_completion=result.output_tokens,
            total_cost_microdollars=actual_cost_microdollars,
            usage_type=UsageType.coerce(usage_type),
            speed_tokens_per_second=result.output_tokens / max(result.elapsed_seconds, 0.001),
            finish_reason=result.finish_reason,
            status="success",
            streamed=streamed,
            usage_estimated=result.usage_estimated,
            provider=provider,
            elapsed_milliseconds=elapsed_ms,
            first_token_milliseconds=(
                _seconds_to_milliseconds(first_token_seconds) if first_token_seconds is not None else None
            ),
            region=region,
        )

    @classmethod
    def from_settle_body(
        cls,
        *,
        authorization: GatewayAuthorization,
        provider_name: str,
        model_id: str | None = None,
        usage_type: UsageType | str | None = None,
        provider: str | None = None,
        body: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        actual_cost_microdollars: int,
    ) -> Generation:
        elapsed = max(float(body.get("elapsed_seconds") or 0.001), 0.001)
        first_token_raw = body.get("first_token_seconds") or body.get("time_to_first_token_seconds")
        first_token = max(float(first_token_raw), 0.001) if first_token_raw is not None else None
        return cls(
            id=f"gen-{uuid.uuid4().hex}",
            request_id=str(body.get("request_id") or f"req-{uuid.uuid4()}"),
            workspace_id=authorization.workspace_id,
            key_hash=authorization.key_hash,
            model=model_id or authorization.model_id,
            provider_name=provider_name,
            app=str(body.get("app") or "TrustedRouter Gateway"),
            tokens_prompt=input_tokens,
            tokens_completion=output_tokens,
            total_cost_microdollars=actual_cost_microdollars,
            usage_type=UsageType.coerce(usage_type or authorization.usage_type),
            speed_tokens_per_second=output_tokens / elapsed,
            finish_reason=str(body.get("finish_reason") or "stop"),
            status=str(body.get("status") or "success"),
            streamed=bool(body.get("streamed", False)),
            usage_estimated=bool(body.get("usage_estimated", False)),
            provider=provider,
            elapsed_milliseconds=_seconds_to_milliseconds(elapsed),
            first_token_milliseconds=(
                _seconds_to_milliseconds(first_token) if first_token is not None else None
            ),
            region=authorization.region,
        )

    def to_openrouter_generation(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "model": self.model,
            "provider_name": self.provider_name,
            "app_id": None,
            "http_referer": None,
            "origin": self.app,
            "usage": microdollars_to_float(self.total_cost_microdollars),
            "usage_microdollars": self.total_cost_microdollars,
            "total_cost": microdollars_to_float(self.total_cost_microdollars),
            "total_cost_microdollars": self.total_cost_microdollars,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "native_tokens_prompt": self.tokens_prompt,
            "native_tokens_completion": self.tokens_completion,
            "finish_reason": self.finish_reason,
            "native_finish_reason": self.finish_reason,
            "streamed": self.streamed,
            "is_byok": self.usage_type.is_byok(),
            "generation_time": self.elapsed_milliseconds
            if self.elapsed_milliseconds is not None
            else int(
                1000
                * (self.tokens_completion / self.speed_tokens_per_second)
                if self.speed_tokens_per_second > 0
                else 0
            ),
            "latency": self.first_token_milliseconds,
            "router": "trustedrouter/v1",
            "usage_type": self.usage_type,
            "usage_estimated": self.usage_estimated,
        }


@dataclass
class ProviderBenchmarkSample:
    """Privacy-safe provider performance sample for future public rankings.

    This intentionally omits workspace_id, key_hash, app, prompt, and output.
    Public ranking pages can aggregate these rows without exposing tenants.
    """

    id: str
    model: str
    provider: str
    provider_name: str
    status: str
    usage_type: UsageType
    streamed: bool
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_microdollars: int = 0
    speed_tokens_per_second: float | None = None
    elapsed_milliseconds: int | None = None
    first_token_milliseconds: int | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    error_status: int | None = None
    region: str | None = None
    created_at: str = field(default_factory=iso_now)

    def __post_init__(self) -> None:
        if not isinstance(self.usage_type, UsageType):
            self.usage_type = UsageType.coerce(self.usage_type)

    @classmethod
    def from_generation(cls, generation: Generation) -> ProviderBenchmarkSample:
        return cls(
            id=f"bench-{uuid.uuid4().hex}",
            model=generation.model,
            provider=generation.provider or _provider_from_model_id(generation.model),
            provider_name=generation.provider_name,
            status=generation.status,
            usage_type=generation.usage_type,
            streamed=generation.streamed,
            input_tokens=generation.tokens_prompt,
            output_tokens=generation.tokens_completion,
            total_cost_microdollars=generation.total_cost_microdollars,
            speed_tokens_per_second=generation.speed_tokens_per_second,
            elapsed_milliseconds=generation.elapsed_milliseconds,
            first_token_milliseconds=generation.first_token_milliseconds,
            finish_reason=generation.finish_reason,
            region=generation.region,
            created_at=generation.created_at,
        )

    @classmethod
    def from_provider_error(
        cls,
        *,
        model: Any,
        provider_name: str,
        input_tokens: int,
        elapsed_seconds: float,
        streamed: bool,
        usage_type: UsageType | str,
        error_status: int,
        error_type: str,
        region: str | None,
        provider: str | None = None,
    ) -> ProviderBenchmarkSample:
        return cls(
            id=f"bench-{uuid.uuid4().hex}",
            model=str(model.id),
            provider=str(provider or model.provider),
            provider_name=provider_name,
            status="error",
            usage_type=UsageType.coerce(usage_type),
            streamed=streamed,
            input_tokens=input_tokens,
            output_tokens=0,
            total_cost_microdollars=0,
            speed_tokens_per_second=None,
            elapsed_milliseconds=_seconds_to_milliseconds(elapsed_seconds),
            first_token_milliseconds=None,
            finish_reason="error",
            error_type=error_type,
            error_status=error_status,
            region=region,
        )


def _seconds_to_milliseconds(value: float) -> int:
    return max(1, int(round(max(float(value), 0.001) * 1000)))


def _provider_from_model_id(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


@dataclass
class SyntheticProbeSample:
    """Privacy-safe synthetic monitor sample.

    These rows are public-status material. They intentionally do not carry
    prompts, outputs, raw request bodies, API keys, or workspace identifiers.
    """

    id: str
    probe_type: str
    target: str
    target_url: str
    monitor_region: str
    status: str
    target_region: str | None = None
    latency_milliseconds: int | None = None
    ttfb_milliseconds: int | None = None
    http_status: int | None = None
    error_type: str | None = None
    provider: str | None = None
    model: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    generation_id: str | None = None
    attestation_digest: str | None = None
    source_commit: str | None = None
    cost_microdollars: int = 0
    output_match: bool | None = None
    created_at: str = field(default_factory=iso_now)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "probe_type": self.probe_type,
            "target": self.target,
            "target_url": self.target_url,
            "monitor_region": self.monitor_region,
            "target_region": self.target_region,
            "status": self.status,
            "latency_milliseconds": self.latency_milliseconds,
            "ttfb_milliseconds": self.ttfb_milliseconds,
            "http_status": self.http_status,
            "error_type": self.error_type,
            "provider": self.provider,
            "model": self.model,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "generation_id": self.generation_id,
            "attestation_digest": self.attestation_digest,
            "source_commit": self.source_commit,
            "cost_microdollars": self.cost_microdollars,
            "output_match": self.output_match,
            "created_at": self.created_at,
        }


@dataclass
class SyntheticRollup:
    """Precomputed synthetic-monitor aggregate.

    Rollups are metadata-only public-status material. They intentionally
    contain no prompts, outputs, raw request bodies, API keys, workspace IDs,
    or BYOK material.
    """

    id: str
    period: str
    period_start: str
    component: str
    target: str
    probe_type: str
    monitor_region: str
    target_region: str | None = None
    sample_count: int = 0
    up_count: int = 0
    down_count: int = 0
    degraded_count: int = 0
    routing_degraded_count: int = 0
    trust_degraded_count: int = 0
    unknown_count: int = 0
    latency_histogram: dict[str, int] = field(default_factory=dict)
    ttfb_histogram: dict[str, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)
    last_checked_at: str | None = None
    cost_microdollars: int = 0
    updated_at: str = field(default_factory=iso_now)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "period": self.period,
            "period_start": self.period_start,
            "component": self.component,
            "target": self.target,
            "probe_type": self.probe_type,
            "monitor_region": self.monitor_region,
            "target_region": self.target_region,
            "sample_count": self.sample_count,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "degraded_count": self.degraded_count,
            "routing_degraded_count": self.routing_degraded_count,
            "trust_degraded_count": self.trust_degraded_count,
            "unknown_count": self.unknown_count,
            "last_checked_at": self.last_checked_at,
            "cost_microdollars": self.cost_microdollars,
            "updated_at": self.updated_at,
        }


@dataclass
class SignupResult:
    """Outcome of a successful `STORE.signup()` call."""

    user: User
    workspace: Workspace
    raw_key: str
    api_key: ApiKey
    trial_credit_microdollars: int


@dataclass
class AuthSession:
    hash: str
    salt: str
    secret_hash: str
    lookup_hash: str
    user_id: str
    provider: str
    label: str
    workspace_id: str | None = None
    created_at: str = field(default_factory=iso_now)
    expires_at: str | None = None
    state: str = "active"  # "active" | "pending_email" (legacy wallet email attach)


@dataclass
class EmailSendBlock:
    """Record of an email address that should not receive further sends.

    Created when SES posts a bounce or complaint via SNS. The email
    service consults `STORE.is_email_blocked(email)` before each send.
    """

    email: str
    reason: str
    bounce_type: str | None = None
    feedback_id: str | None = None
    created_at: str = field(default_factory=iso_now)


@dataclass
class WalletChallenge:
    """SIWE nonce + canonical message for a single MetaMask sign-in attempt."""

    hash: str
    salt: str
    secret_hash: str
    lookup_hash: str
    address: str
    message: str
    created_at: str = field(default_factory=iso_now)
    expires_at: str | None = None
    consumed_at: str | None = None


@dataclass
class VerificationToken:
    """One-shot magic-link token for wallet user email verification."""

    hash: str
    salt: str
    secret_hash: str
    lookup_hash: str
    user_id: str
    purpose: str
    created_at: str = field(default_factory=iso_now)
    expires_at: str | None = None
    consumed_at: str | None = None


@dataclass
class OAuthAuthorizationCode:
    """One-shot OAuth/PKCE code used to delegate workspace credits to an app."""

    hash: str
    salt: str
    secret_hash: str
    lookup_hash: str
    workspace_id: str
    user_id: str | None
    app_id: int
    callback_url: str
    key_label: str
    limit_microdollars: int | None = None
    limit_reset: str | None = None
    expires_at: str | None = None
    code_challenge: str | None = None
    code_challenge_method: str | None = None
    created_at: str = field(default_factory=iso_now)
    code_expires_at: str | None = None
    consumed_at: str | None = None
    spawn_agent: str | None = None
    spawn_cloud: str | None = None


@dataclass
class RateLimitHit:
    allowed: bool
    limit: int
    remaining: int
    reset_at: str
    retry_after_seconds: int
