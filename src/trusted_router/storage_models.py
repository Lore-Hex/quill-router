from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from trusted_router.money import microdollars_to_float
from trusted_router.types import UsageType


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso_now() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def _elapsed_iso_milliseconds(start: str, end: str) -> int | None:
    try:
        started = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((ended - started).total_seconds() * 1000))


def _video_failure_type(value: str | None) -> str:
    """Map gateway failure markers to a finite, public-safe taxonomy."""
    marker = (value or "").strip().lower()
    return {
        "provider_error": "provider_error",
        "provider_failed": "provider_failed",
        "rate_limit_exceeded": "rate_limit",
        "submission_interrupted": "submission_interrupted",
        "timeout": "timeout",
    }.get(marker, "provider_error")


def _is_byok(usage_type: str | UsageType) -> bool:
    """True iff `usage_type` represents BYOK billing.

    Accepts either a `UsageType` enum or a raw string from JSON round-trip.
    """
    return UsageType.coerce(usage_type).is_byok()


def _is_synthetic_metadata(metadata: Any) -> bool:
    return isinstance(metadata, dict) and str(metadata.get("trustedrouter_synthetic")).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _coerce_tool_calls(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    tool_calls: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            tool_calls.append({str(key): item_value for key, item_value in item.items()})
    return tool_calls or None


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
    # Owner notifications (see phone_verification.py). A verified phone is the
    # cost floor on account farming, so it gates every notify channel including
    # email. `pending_phone` is the number awaiting proof and is deliberately
    # NOT reachable — otherwise starting verification would itself be a way to
    # send someone a message.
    phone: str | None = None
    phone_verified: bool = False
    phone_verified_at: str | None = None
    pending_phone: str | None = None
    phone_code_hash: str | None = None
    phone_code_salt: str | None = None
    phone_code_expires_at: str | None = None
    phone_code_attempts: int = 0
    phone_code_sent_at: str | None = None


@dataclass
class ProviderAccessGrant:
    """Explicit access to one provider's private operational data."""

    user_id: str
    provider: str
    role: str = "viewer"
    created_at: str = field(default_factory=iso_now)


def normalize_provider_access_slug(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in normalized
    ):
        raise ValueError("provider must be a lowercase provider slug")
    return normalized


def normalize_provider_access_role(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"viewer", "admin"}:
        raise ValueError("provider access role must be viewer or admin")
    return normalized


@dataclass
class Workspace:
    id: str
    name: str
    owner_user_id: str
    created_at: str = field(default_factory=iso_now)
    deleted: bool = False
    content_storage_enabled: bool = False
    # Operational QUIESCE for the typed-billing migration (and a general billing
    # kill switch): when paused, the gateway rejects new authorizes/validates and
    # key creation is blocked, so in-flight requests can drain to zero holds before
    # a workspace is flipped to typed enforcement (codex Step-6 design). Settle of
    # already-authorized requests is NOT blocked — only new work is.
    billing_paused: bool = False
    billing_pause_reason: str = ""
    # Non-empty marks a SHADOW of a home-plane workspace, materialized locally
    # so a federated key can resolve (see federated_workspace_from_record). It
    # is not a workspace anyone created here: it has no owner, no member row,
    # and no entitlements beyond what the home plane's allow-list serves. A
    # later reconciliation needs to tell shadows from locally-issued
    # workspaces, and a boolean would not say WHICH home to reconcile against.
    federated_home: str = ""


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
    # Optional per-window spend limits (fixed UTC calendar windows, lazily
    # reset — see spend_windows.py). NULL = window unlimited. Creation-time
    # config is seeded to tr_key_limit *_limit_micro; the window USAGE counters
    # are typed-DML-owned and live only on the typed row.
    limit_daily_microdollars: int | None = None
    limit_weekly_microdollars: int | None = None
    limit_monthly_microdollars: int | None = None
    # False (the default) makes window budgets HARD LIMITS (429). True explicitly
    # opts into alert-only thresholds: crossing a window emails the workspace
    # owner but does not block requests. `budget_alerted` dedups the email as
    # {window: window_start_iso} (JSON-owned alert state, not a counter).
    budget_alert_only: bool = False
    budget_alerted: dict[str, str] = field(default_factory=dict)
    include_byok_in_limit: bool = True
    usage_microdollars: int = 0
    byok_usage_microdollars: int = 0
    expires_at: str | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None
    reserved_microdollars: int = 0
    # Independent usage-counter rows for a high-throughput key. Keys with an
    # exact lifetime spend limit remain at one shard. Fixed-window limits are
    # approximate snapshot checks and may sum usage across shards.
    usage_shard_count: int = 1
    tags: dict[str, str] = field(default_factory=dict)
    # Non-empty marks a key learned from a home plane via federation. Such a
    # key has NO usable secret_hash, so it can only ever authenticate through
    # the attested gateway (lookup-hash) path, never the direct raw-bearer one.
    federated_home: str = ""


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
class CustomModel:
    id: str
    owner_user_id: str
    owner_workspace_id: str
    name: str
    base_model_id: str
    hidden_prompt: str
    revision: int = 1
    enabled: bool = True
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None


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
class VideoJob:
    """Content-free durable state for one asynchronous video generation.

    Prompts, reference media, provider response bodies, download URLs, and
    generated bytes are intentionally absent. The attested gateway keeps those
    on the provider path; this row exists only to make polling and billing
    idempotent across regions and process restarts.
    """

    id: str
    workspace_id: str
    key_hash: str
    authorization_id: str
    model: str
    provider: str
    endpoint_id: str
    provider_model: str
    quoted_microdollars: int
    input_mode: str = "text"
    duration_seconds: int = 0
    resolution: str = ""
    aspect_ratio: str = ""
    generate_audio: bool = False
    region: str = ""
    status: str = "submitting"
    provider_job_id: str | None = None
    provider_status: str | None = None
    generation_id: str | None = None
    attempts: int = 0
    next_poll_at: str = field(default_factory=iso_now)
    lease_owner: str | None = None
    leased_until: str | None = None
    last_error: str | None = None
    content_expires_at: str | None = None
    cleaned_at: str | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None


@dataclass
class SettleOutboxRow:
    """A durable settle intent (docs/design/durable-settle-outbox.md).

    Enqueued before the inline settle is attempted; recovered by the drain if the
    inline attempt is lost. The FROZEN inputs (actual_cost_micro / selected_* /
    model_id / settle_origin / reservation_id) are captured from the SAME decision
    the inline attempt used, so a drain applies a deterministic amount and origin
    no matter what pricing or serving env changes afterward. The PK is
    (authorization_id, intent_kind) so a settle and a refund never clobber.

    status: pending -> done (charged, terminal) | dead (drain gave up, FREEZES the
    hold — a human resolves) | release_approved (human ok'd freeing the hold).
    """

    authorization_id: str
    intent_kind: str  # "settle" | "refund"
    settle_origin: str  # "typed" | "legacy"
    actual_cost_micro: int
    reservation_id: str | None = None
    selected_endpoint_id: str | None = None
    model_id: str | None = None
    selected_usage_type: str | None = None
    settle_body: str | None = None  # raw GatewaySettleRequest JSON (audit/generation)
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    next_attempt_at: str | None = field(default_factory=iso_now)
    lease_owner: str | None = None
    leased_until: str | None = None
    created_at: str = field(default_factory=iso_now)
    updated_at: str | None = None
    # NULL while unresolved. Spanner row deletion policies ignore NULL, so a
    # pending/dead row can never expire before settlement repair is complete.
    terminal_at: str | None = None


@dataclass
class CreditAccount:
    workspace_id: str
    # Number of independent tr_credit_balance sub-ledgers owned by this
    # workspace. The default preserves the original one-row behavior; only the
    # pause/drain operator path may activate more shards for a hot workspace.
    shard_count: int = 1
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
class CreditMoney:
    """In-memory single-book credit money for one workspace. The Spanner
    store keeps this in the typed tr_credit_balance table; the InMemory twin
    keeps it here so CreditAccount stays metadata-only."""

    total_credits_microdollars: int = 0
    total_usage_microdollars: int = 0
    reserved_microdollars: int = 0


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
    idempotency_key: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    idempotency_fingerprint: str | None = None
    custom_model_id: str | None = None
    custom_model_revision: int | None = None
    additional_cost_reservation_microdollars: int = 0
    # Frozen at authorization time. Settlement must never infer this from a
    # caller-supplied route_type because native provider Batch APIs retain
    # plaintext content and receive discounted billing.
    native_batch_eligible: bool = False
    # The settlement DECISION, made at authorize and stored — settle must not
    # re-derive it from key state, which can change between authorize and
    # settle. "local" books against the plane's own balance (every
    # authorization before this field existed); "deferred_home" records the
    # spend as debt to the home plane's ledger, forwarded asynchronously.
    settlement: str = "local"
    # Only deferred authorizations carry an expiry: it is what lets the reaper
    # reclaim the outstanding-counter estimate when the enclave dies between
    # authorize and settle. Local authorizations keep their pre-existing
    # lifecycle untouched.
    expires_at: str | None = None
    # Durable, content-free replay facts written in the same transaction that
    # settles or refunds the billing reservation. Bigtable generation mirrors
    # are deliberately not the source of truth for this outcome: a successful
    # Spanner commit followed by a failed mirror must never look like a refund.
    finalization_outcome: str | None = None
    finalized_cost_microdollars: int | None = None
    finalized_usage_type: str | None = None
    finalized_generation_id: str | None = None
    finalized_model_id: str | None = None
    finalized_provider: str | None = None
    finalized_region: str | None = None
    finalized_input_tokens: int | None = None
    finalized_output_tokens: int | None = None
    finalized_reasoning_tokens: int | None = None
    finalized_cached_input_tokens: int | None = None

    def __post_init__(self) -> None:
        # JSON round-trip stores usage_type as a string; coerce so the field
        # is always a UsageType at runtime regardless of construction path.
        if not isinstance(self.usage_type, UsageType):
            self.usage_type = UsageType.coerce(self.usage_type)

    def record_finalization(
        self,
        *,
        success: bool,
        actual_microdollars: int,
        selected_usage_type: UsageType | str,
        generation: Generation | None,
    ) -> None:
        """Persist the authoritative replay result beside the billing claim."""
        usage_type = UsageType.coerce(selected_usage_type)
        self.settled = True
        self.finalization_outcome = "settled" if success else "refunded"
        self.finalized_cost_microdollars = max(0, int(actual_microdollars)) if success else 0
        self.finalized_usage_type = usage_type.value
        self.finalized_generation_id = generation.id if generation is not None else None
        self.finalized_model_id = generation.model if generation is not None else self.model_id
        self.finalized_provider = (
            generation.provider if generation is not None and generation.provider else self.provider
        )
        self.finalized_region = generation.region if generation is not None else self.region
        self.finalized_input_tokens = (
            max(0, int(generation.tokens_prompt)) if generation is not None else 0
        )
        self.finalized_output_tokens = (
            max(0, int(generation.tokens_completion)) if generation is not None else 0
        )
        self.finalized_reasoning_tokens = (
            max(0, int(generation.reasoning_tokens)) if generation is not None else 0
        )
        self.finalized_cached_input_tokens = (
            max(0, int(generation.cached_input_tokens)) if generation is not None else 0
        )


@dataclass(frozen=True)
class TypedFinalizeResult:
    finalized: bool
    activity_indexed: bool
    request_record_typed: bool = False


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
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: list[dict[str, Any]] | None = None
    created_at: str = field(default_factory=iso_now)
    provider: str | None = None
    elapsed_milliseconds: int | None = None
    first_token_milliseconds: int | None = None
    # Time to first response BYTE (headers / first SSE frame), distinct from
    # first_token_milliseconds which is time to first CONTENT token (TTFT).
    ttfb_milliseconds: int | None = None
    region: str | None = None
    user: str | None = None
    session_id: str | None = None
    http_referer: str | None = None
    app_categories: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    # Internal provider COGS for fixed-price orchestration leaves. This is
    # intentionally omitted from public generation/activity response shapes.
    operator_cost_microdollars: int | None = None
    route_type: str | None = None
    video_input_mode: str | None = None
    video_duration_seconds: int | None = None
    video_resolution: str | None = None
    video_aspect_ratio: str | None = None
    video_generate_audio: bool | None = None

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
        first_byte_seconds = getattr(result, "first_byte_seconds", None)
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
            cached_input_tokens=int(getattr(result, "cached_input_tokens", 0) or 0),
            reasoning_tokens=int(getattr(result, "reasoning_tokens", 0) or 0),
            tool_calls=_coerce_tool_calls(getattr(result, "tool_calls", None)),
            provider=provider,
            elapsed_milliseconds=elapsed_ms,
            first_token_milliseconds=(
                _seconds_to_milliseconds(first_token_seconds)
                if first_token_seconds is not None
                else None
            ),
            ttfb_milliseconds=(
                _seconds_to_milliseconds(first_byte_seconds)
                if first_byte_seconds is not None
                else None
            ),
            region=region,
        )

    @classmethod
    def from_embeddings_result(
        cls,
        *,
        result: dict[str, Any],
        workspace_id: str,
        key_hash: str,
        model_id: str,
        app_name: str,
        actual_cost_microdollars: int,
        usage_type: UsageType | str,
        input_tokens: int,
        provider: str | None = None,
        provider_name: str | None = None,
        region: str | None = None,
        elapsed_seconds: float = 0.001,
    ) -> Generation:
        """Record an embeddings call. Embeddings bill INPUT tokens only, so
        `tokens_completion` is 0 and there is no throughput figure
        (speed_tokens_per_second=0). `finish_reason` is "stop" by
        convention — embeddings have no streamed completion."""
        request_id = str(result.get("id") or f"emb-{uuid.uuid4().hex}")
        return cls(
            id=f"gen-{uuid.uuid4().hex}",
            request_id=request_id,
            workspace_id=workspace_id,
            key_hash=key_hash,
            model=model_id,
            provider_name=provider_name or (provider or model_id),
            app=app_name,
            tokens_prompt=input_tokens,
            tokens_completion=0,
            total_cost_microdollars=actual_cost_microdollars,
            usage_type=UsageType.coerce(usage_type),
            speed_tokens_per_second=0.0,
            finish_reason="stop",
            status="success",
            streamed=False,
            usage_estimated=False,
            provider=provider,
            elapsed_milliseconds=_seconds_to_milliseconds(elapsed_seconds),
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
        operator_cost_microdollars: int | None = None,
    ) -> Generation:
        elapsed = max(float(body.get("elapsed_seconds") or 0.001), 0.001)
        first_token_raw = body.get("first_token_seconds") or body.get("time_to_first_token_seconds")
        first_token = max(float(first_token_raw), 0.001) if first_token_raw is not None else None
        first_byte_raw = body.get("first_byte_seconds") or body.get("time_to_first_byte_seconds")
        first_byte = max(float(first_byte_raw), 0.001) if first_byte_raw is not None else None
        app = str(body.get("app") or "TrustedRouter Gateway")
        if _is_synthetic_metadata(body.get("metadata")):
            app = "TrustedRouter Synthetic"
        return cls(
            id=generation_id_for_authorization(authorization.id),
            request_id=str(
                body.get("request_id") or f"req-{uuid.uuid5(uuid.NAMESPACE_URL, authorization.id)}"
            ),
            workspace_id=authorization.workspace_id,
            key_hash=authorization.key_hash,
            model=model_id or authorization.model_id,
            provider_name=provider_name,
            app=app,
            tokens_prompt=input_tokens,
            tokens_completion=output_tokens,
            total_cost_microdollars=actual_cost_microdollars,
            usage_type=UsageType.coerce(usage_type or authorization.usage_type),
            speed_tokens_per_second=output_tokens / elapsed,
            finish_reason=str(body.get("finish_reason") or "stop"),
            status=str(body.get("status") or "success"),
            streamed=bool(body.get("streamed", False)),
            usage_estimated=bool(body.get("usage_estimated", False)),
            # `cache_read_input_tokens` is the canonical settle-body field: it is
            # what the attested gateway sends, what SettleRequest declares, and
            # what billing reads via `cache_read_count`. Reading only the two
            # legacy aliases meant this metric was silently 0 on every attested
            # generation. Billing was never affected — the cost is computed
            # upstream and passed in as `actual_cost_microdollars`; only this
            # activity-index field was blank.
            cached_input_tokens=int(
                body.get("cache_read_input_tokens")
                or body.get("cached_input_tokens")
                or body.get("cached_tokens")
                or 0
            ),
            reasoning_tokens=int(body.get("reasoning_tokens") or 0),
            # Tool-call arguments are model output content, not activity
            # metadata. The attested gateway returns them to the caller but the
            # control-plane activity index never persists them.
            tool_calls=None,
            provider=provider,
            elapsed_milliseconds=_seconds_to_milliseconds(elapsed),
            first_token_milliseconds=(
                _seconds_to_milliseconds(first_token) if first_token is not None else None
            ),
            ttfb_milliseconds=(
                _seconds_to_milliseconds(first_byte) if first_byte is not None else None
            ),
            region=authorization.region,
            user=str(body["user"]) if body.get("user") is not None else None,
            session_id=(str(body["session_id"]) if body.get("session_id") is not None else None),
            http_referer=(
                str(body["http_referer"]) if body.get("http_referer") is not None else None
            ),
            app_categories=[str(item) for item in body.get("app_categories") or []],
            tags=dict(authorization.tags),
            operator_cost_microdollars=operator_cost_microdollars,
            route_type=(str(body["route_type"]) if body.get("route_type") else None),
            video_input_mode=(
                str(body["video_input_mode"]) if body.get("video_input_mode") else None
            ),
            video_duration_seconds=(
                int(body["video_duration_seconds"])
                if body.get("video_duration_seconds") is not None
                else None
            ),
            video_resolution=(
                str(body["video_resolution"]) if body.get("video_resolution") else None
            ),
            video_aspect_ratio=(
                str(body["video_aspect_ratio"]) if body.get("video_aspect_ratio") else None
            ),
            video_generate_audio=(
                bool(body["video_generate_audio"])
                if body.get("video_generate_audio") is not None
                else None
            ),
            created_at=authorization.created_at,
        )

    def to_openrouter_generation(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "model": self.model,
            "provider_name": self.provider_name,
            "app_id": None,
            "http_referer": self.http_referer,
            "origin": self.app,
            "user": self.user,
            "session_id": self.session_id,
            "app_categories": list(self.app_categories),
            "tags": dict(self.tags),
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
                1000 * (self.tokens_completion / self.speed_tokens_per_second)
                if self.speed_tokens_per_second > 0
                else 0
            ),
            "latency": self.first_token_milliseconds,
            "router": "trustedrouter/v1",
            "usage_type": self.usage_type,
            "usage_estimated": self.usage_estimated,
        }


def generation_id_for_authorization(authorization_id: str) -> str:
    """Return the stable generation id for one gateway authorization.

    Settlement can commit while its HTTP response is lost.  The durable outbox
    then rebuilds and re-indexes the same metadata.  A deterministic id makes
    that repair idempotent in Bigtable instead of creating duplicate activity
    rows on every replay.
    """
    return f"gen-{uuid.uuid5(uuid.NAMESPACE_URL, f'trustedrouter:{authorization_id}').hex}"


# Redaction for provider error strings persisted on benchmark samples. Shared
# by the probe (client-side) and the internal ingest route (server-side) so a
# buggy or future caller cannot persist key-shaped or bearer material.
_KEY_SHAPED_RE = re.compile(r"(?i)\b(sk|rk)-[A-Za-z0-9_\-*]{4,}")
_BEARER_TOKEN_RE = re.compile(r"(?i)bearer\s+\S+")


def scrub_provider_error_message(value: str) -> str:
    scrubbed = _KEY_SHAPED_RE.sub("sk-***", value)
    return _BEARER_TOKEN_RE.sub("Bearer ***", scrubbed)


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
    ttfb_milliseconds: int | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    error_status: int | None = None
    # Truncated upstream/provider error detail from synthetic probes. Organic
    # samples leave it None; privacy-safe because this is a provider error
    # string, never tenant content.
    error_message: str | None = None
    region: str | None = None
    # Internal-only provenance: "organic" (real production traffic),
    # "synthetic" (short rotation probe), or "synthetic_throughput" (long
    # effective-output benchmark). Public ranking pages use the long probe only
    # for request-start-to-completion throughput, never provider uptime.
    source: str = "organic"
    # Caller-self-reported app name (Generation.app, from the X-Title / Referer
    # header), used by the /apps directory. Privacy-safe: it's the public title
    # the caller chose to send — never a workspace, key, or prompt. Empty / the
    # "TrustedRouter Gateway" default is treated as anonymous "Direct" traffic;
    # "TrustedRouter Synthetic" (the monitor) is excluded from the apps ranking.
    app: str = ""
    route_type: str | None = None
    video_input_mode: str | None = None
    video_duration_seconds: int | None = None
    video_resolution: str | None = None
    video_aspect_ratio: str | None = None
    video_generate_audio: bool | None = None
    created_at: str = field(default_factory=iso_now)

    def __post_init__(self) -> None:
        if not isinstance(self.usage_type, UsageType):
            self.usage_type = UsageType.coerce(self.usage_type)

    @classmethod
    def from_generation(cls, generation: Generation) -> ProviderBenchmarkSample:
        return cls(
            id=f"bench-{uuid.uuid5(uuid.NAMESPACE_URL, generation.id).hex}",
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
            ttfb_milliseconds=generation.ttfb_milliseconds,
            finish_reason=generation.finish_reason,
            region=generation.region,
            app=generation.app,
            route_type=generation.route_type,
            video_input_mode=generation.video_input_mode,
            video_duration_seconds=generation.video_duration_seconds,
            video_resolution=generation.video_resolution,
            video_aspect_ratio=generation.video_aspect_ratio,
            video_generate_audio=generation.video_generate_audio,
            created_at=generation.created_at,
        )

    @classmethod
    def from_video_job_failure(
        cls,
        job: VideoJob,
        *,
        provider_name: str,
    ) -> ProviderBenchmarkSample:
        return cls(
            id=f"bench-{uuid.uuid5(uuid.NAMESPACE_URL, f'video:{job.id}:failed').hex}",
            model=job.model,
            provider=job.provider,
            provider_name=provider_name,
            status="error",
            usage_type=UsageType.CREDITS,
            streamed=False,
            total_cost_microdollars=0,
            elapsed_milliseconds=_elapsed_iso_milliseconds(
                job.created_at, job.updated_at or iso_now()
            ),
            finish_reason="failed",
            error_type=_video_failure_type(job.last_error),
            region=job.region or None,
            source="organic",
            route_type="videos",
            video_input_mode=job.input_mode,
            video_duration_seconds=job.duration_seconds or None,
            video_resolution=job.resolution or None,
            video_aspect_ratio=job.aspect_ratio or None,
            video_generate_audio=job.generate_audio,
            created_at=job.updated_at or iso_now(),
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


# How far into the future a synthetic sample's created_at may sit before
# ingest, storage reads, and the status layer treat it as poison rather
# than evidence. 60s absorbs ordinary monitor/host clock skew. Shared here
# (next to the model) because all three layers must agree: a bound
# enforced in only one of them leaves the others trusting year-7748
# fixture rows — which happened, and permanently disabled the staleness
# detector on a live deployment.
FUTURE_SAMPLE_SKEW_SECONDS = 60


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
    dns_milliseconds: int | None = None
    tcp_connect_milliseconds: int | None = None
    tls_handshake_milliseconds: int | None = None
    gateway_processing_milliseconds: int | None = None
    connection_reused: bool | None = None
    protocol: str | None = None
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
            "dns_milliseconds": self.dns_milliseconds,
            "tcp_connect_milliseconds": self.tcp_connect_milliseconds,
            "tls_handshake_milliseconds": self.tls_handshake_milliseconds,
            "gateway_processing_milliseconds": self.gateway_processing_milliseconds,
            "connection_reused": self.connection_reused,
            "protocol": self.protocol,
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
    dns_histogram: dict[str, int] = field(default_factory=dict)
    tcp_connect_histogram: dict[str, int] = field(default_factory=dict)
    tls_handshake_histogram: dict[str, int] = field(default_factory=dict)
    gateway_processing_histogram: dict[str, int] = field(default_factory=dict)
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
class AcquisitionAttribution:
    """Privacy-bounded acquisition record for one workspace.

    Raw click identifiers are converted to keyed, non-reversible fingerprints
    before this record is created. Neither identifiers nor fingerprints are
    uploaded to ad platforms or copied into public APIs, generation metadata,
    or the prompt path.
    """

    workspace_id: str
    anonymous_id: str
    first_touch: dict[str, str]
    last_touch: dict[str, str]
    signup_provider: str
    starter_credit_microdollars: int = 0
    signup_at: str = field(default_factory=iso_now)
    milestones: dict[str, str] = field(default_factory=dict)
    purchase_count: int = 0
    purchase_microdollars: int = 0
    first_purchase_at: str | None = None
    last_purchase_at: str | None = None
    updated_at: str = field(default_factory=iso_now)


ACTIVATION_REMINDER_DELAYS_SECONDS: tuple[tuple[str, int], ...] = (
    ("10m", 10 * 60),
    ("24h", 24 * 60 * 60),
)


@dataclass(frozen=True)
class ActivationReminderTask:
    """A bounded, sortable activation reminder due for one workspace."""

    id: str
    workspace_id: str
    stage: str
    due_at: str
    created_at: str = field(default_factory=iso_now)


def activation_reminder_tasks(
    record: AcquisitionAttribution,
) -> tuple[ActivationReminderTask, ...]:
    signup_at = dt.datetime.fromisoformat(record.signup_at.replace("Z", "+00:00"))
    if signup_at.tzinfo is None:
        signup_at = signup_at.replace(tzinfo=dt.UTC)
    tasks: list[ActivationReminderTask] = []
    for stage, delay_seconds in ACTIVATION_REMINDER_DELAYS_SECONDS:
        due_at = (signup_at + dt.timedelta(seconds=delay_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        tasks.append(
            ActivationReminderTask(
                id=f"{due_at}#{record.workspace_id}#{stage}",
                workspace_id=record.workspace_id,
                stage=stage,
                due_at=due_at,
                created_at=record.signup_at,
            )
        )
    return tuple(tasks)


@dataclass
class BedrockGroupBuyPledge:
    """Private purchasing commitment for one signed-in user.

    This record is never returned by a public endpoint. Public totals come
    from aggregate shards and public comments come from the deliberately
    identity-free ``BedrockGroupBuyPublicMessage`` projection below.
    """

    user_id: str
    workspace_id: str
    full_name: str
    title: str
    company_name: str
    company_url: str
    monthly_minimum_microdollars: int
    expected_bedrock_monthly_microdollars: int
    expected_all_llm_monthly_microdollars: int
    aggregate_shard: int
    last_month_llm_spend_microdollars: int = 0
    last_month_spend_sources: tuple[str, ...] = ()
    public_message: str = ""
    publish_message: bool = False
    public_message_id: str = ""
    public_message_index_id: str = ""
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)
    accepted_at: str = field(default_factory=iso_now)

    def __post_init__(self) -> None:
        # JSON persistence decodes tuples as lists; keep the in-process model
        # immutable-shaped regardless of the storage backend that read it.
        self.last_month_spend_sources = tuple(self.last_month_spend_sources)


@dataclass
class BedrockGroupBuyAggregateShard:
    """One of the fixed shards used to avoid a hot global counter row."""

    shard_id: int
    active_pledge_count: int = 0
    monthly_minimum_microdollars: int = 0
    expected_bedrock_monthly_microdollars: int = 0
    expected_all_llm_monthly_microdollars: int = 0
    updated_at: str = field(default_factory=iso_now)


@dataclass
class BedrockGroupBuyAggregate:
    """Online pledge totals before the organizer-provided founding baseline."""

    active_pledge_count: int = 0
    monthly_minimum_microdollars: int = 0
    expected_bedrock_monthly_microdollars: int = 0
    expected_all_llm_monthly_microdollars: int = 0


@dataclass
class BedrockGroupBuyPublicMessage:
    """Identity-free projection that is safe to return on the public page."""

    id: str
    message: str
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)


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
    mail_class: str | None = None
    sender_profile: str | None = None
    acquisition_source: str | None = None
    acquisition_medium: str | None = None
    acquisition_campaign: str | None = None
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


@dataclass
class CreditTransfer:
    """One cross-plane credit movement, as seen by the SOURCE plane.

    The escrow record itself: while `state` is ESCROWED this row IS the value
    (it was debited from the workspace's spendable balance in the transaction
    that wrote it). See `trusted_router.credit_transfer` for the state machine,
    which plane holds the value in each state, and the conservation invariant.
    """

    id: str
    workspace_id: str
    amount_microdollars: int
    #: Free-text label for the destination plane, e.g. its base URL. AUDIT
    #: ONLY — never used for authorization, and never trusted from the wire.
    destination: str
    state: str
    created_at: str = field(default_factory=iso_now)
    resolved_at: str | None = None


#: A shadow workspace's display name. The home plane's allow-list does NOT
#: serve workspace names, so a real-looking one would be fabricated: a second
#: copy of a field nobody sent, free to drift, and easy for an operator to
#: mistake for the customer's actual workspace name.
FEDERATED_WORKSPACE_NAME = "federated"

#: Reason text shown when the shadow is paused. The home plane serves the
#: pause BIT, not a reason string; this makes the origin of the pause obvious
#: rather than implying a local operator paused it.
FEDERATED_WORKSPACE_PAUSE_REASON = "billing is paused on the home plane"


def federated_workspace_from_record(record: dict[str, Any]) -> Workspace:
    """Build the minimal local SHADOW of a home-plane workspace.

    Without this, every federated request 403s: `_authorize_gateway_sync` (and
    the validate / resolve-custom-model gates beside it) read
    `STORE.get_workspace(api_key.workspace_id)` and reject a missing one before
    credits are ever consulted.

    A shadow is a SECOND COPY of somebody else's record, so every field carried
    is a field that can drift. Only two things are carried, and only because
    something on this plane actually reads them:

      * `id` — every workspace-scoped read on the authorize path keys on it,
        and it is the join key back to the home plane for reconciliation.
      * `billing_paused` — the single workspace field the home plane's
        allow-list serves (`workspace_billing_paused`), and the only one
        `_authorize_gateway_sync` reads, via `assert_workspace_billing_active`.
        It is a RESTRICTION, not an entitlement: copying it can only ever
        refuse work. KNOWN DRIFT: nothing refreshes a federated record today
        (it is written on a cache MISS only), so an unpause on the home plane
        does not reach here until the key record is re-federated. Refusing to
        serve is the safe side of that staleness; the unsafe side — a pause
        that fails to arrive — is why a refresh path is still owed.

    Everything else is deliberately NOT carried:

      * `owner_user_id` is EMPTY. This plane has no user directory, so any
        value would either be invented or, worse, point at an unrelated local
        user and hand them a stranger's workspace.
      * No `Member` row is written. A shadow must never appear in a local
        console listing (`list_workspaces_for_user` reads members), because
        nobody here is a member of it.
      * `content_storage_enabled` stays False. It is an ENTITLEMENT the home
        plane does not serve; defaulting it off means a federated workspace's
        request content is never stored here, which is the fail-safe default.
      * No credits, of any kind. That is the whole point — see
        `trusted_router.credit_transfer`.
    """
    paused = bool(record.get("workspace_billing_paused", False))
    return Workspace(
        id=str(record.get("workspace_id") or ""),
        name=FEDERATED_WORKSPACE_NAME,
        owner_user_id="",
        deleted=False,
        content_storage_enabled=False,
        billing_paused=paused,
        billing_pause_reason=FEDERATED_WORKSPACE_PAUSE_REASON if paused else "",
        # Same marker convention as ApiKey.federated_home: the home record's
        # revision when it sent one, otherwise a constant. Non-empty is what
        # makes this distinguishable from a locally-issued workspace.
        federated_home=str(record.get("revision") or "") or "federated",
    )


def federated_api_key_from_record(record: dict[str, Any]) -> ApiKey:
    """Build a local ApiKey from a home plane's federated record.

    Two absences are the security design, not omissions:

      * salt / secret_hash are set to empty. A peer never holds
        home-issued key material, so the direct raw-bearer path (which
        verifies secret_hash) can never authenticate a federated key —
        only the attested gateway path, which matches on lookup_hash.
        verify_api_key against an empty secret_hash fails closed.
      * usage/byok counters start at ZERO and no credits come across.
        Identity is an assertion and copies safely; a balance is a
        quantity under a conservation law and copying it mints money.

    KNOWN CONSEQUENCE of that second absence, recorded because it reads as an
    oversight otherwise: the LIMITS copy but the counters do not, so a key
    capped at $10/day is capped at $10/day *per plane*. Home plus two AWS
    regions is $30/day, and `limit_microdollars` multiplies the same way. That
    cap is exactly the field a customer sets to bound a leak, so the
    multiplication is worth stating plainly.

    It is accepted rather than fixed because the alternative is worse: keeping
    a shared counter accurate across planes means a synchronous cross-plane
    read on every authorize, which is the coupling this whole design exists to
    remove, and federating the counters instead would federate a quantity —
    the thing the conservation law forbids. Total spend on a peer plane stays
    bounded by the credits explicitly TRANSFERRED to it, so this widens blast
    radius without breaking conservation. Per-plane caps should be sized with
    that in mind.
    """
    return ApiKey(
        hash=str(record.get("key_hash") or ""),
        salt="",
        secret_hash="",
        lookup_hash=str(record.get("lookup_hash") or ""),
        name=str(record.get("name") or ""),
        # Display label only. Derived inline rather than importing
        # security.key_label, which would create an import cycle.
        label=(str(record.get("name") or "federated"))[:24],
        workspace_id=str(record.get("workspace_id") or ""),
        creator_user_id=None,
        disabled=bool(record.get("disabled", False)),
        management=False,  # never federated; the home plane refuses to serve them
        limit_microdollars=record.get("limit_microdollars"),
        limit_daily_microdollars=record.get("limit_daily_microdollars"),
        limit_weekly_microdollars=record.get("limit_weekly_microdollars"),
        limit_monthly_microdollars=record.get("limit_monthly_microdollars"),
        budget_alert_only=bool(record.get("budget_alert_only", False)),
        include_byok_in_limit=bool(record.get("include_byok_in_limit", True)),
        expires_at=record.get("expires_at"),
        federated_home=str(record.get("revision") or "") or "federated",
    )
