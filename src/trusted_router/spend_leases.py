"""Stage A spend-lease protocol primitives.

Stage A artifacts are advisory only: every JWS minted here carries
``authoritative=false`` and no function in this module mutates credits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from trusted_router.catalog import (
    Model,
    ModelEndpoint,
    cache_token_prices_microdollars,
    effective_endpoint,
    endpoint_zero_data_retention,
)
from trusted_router.money import token_cost_microdollars
from trusted_router.openai_service_tiers import (
    OPENAI_PRIORITY_MAX_PROMPT_TOKENS,
    openai_priority_pricing,
)
from trusted_router.pricing import PriceTier
from trusted_router.receipt_keys import b64url_decode, b64url_encode, normalize_receipt_jwk
from trusted_router.types import UsageType

SPEND_LEASE_TYP = "spend-lease+jws"
SPEND_LEASE_COHORT = "credits-chat-v1"
SPEND_LEASE_ROUTE_TYPES = frozenset({"chat.completions", "responses"})
SPEND_LEASE_CATALOG_VERSION_PREFIX = "spend-lease-catalog-v1:"
BOOT_AUTH_DOMAIN = b"tr-authorize-v1"
SPEND_LEASE_BOOT_KIND = "spend_lease_boot"
SPEND_LEASE_GENERATION_KIND = "spend_lease_generation"
SPEND_LEASE_ACTIVE_GRANT_KIND = "spend_lease_active_grant"

LeaseStatus = Literal["active", "draining", "terminal", "expired"]
ShadowVerdict = Literal["accepted", "declined_funds", "declined_other"]
ShadowDivergence = Literal["none", "admit_diverged", "estimate_low", "echo_invalid"]
SpendLeaseEligibilityFailure = Literal[
    "boot_digest_not_accepted",
    "unpaid_workspace",
    "not_pilot",
    "route_type",
    "no_candidates",
    "candidate_not_credits",
    "custom_model",
    "user_model",
    "partner_mode",
    "additional_cost",
    "native_batch",
    "app_markup",
    "receipt_fee",
    "regional_lease",
    "key_window_limit",
]


FrozenSpendLeaseCatalog: TypeAlias = Mapping[
    str,
    str | list[dict[str, str | int | bool | None]],
]
SpendLeaseBindingFailure = Literal[
    "no_idempotency_key",
    "escrow_headroom",
    "scope_arbitrated",
    "predecessor_limit",
    "lease_transferred",
    "lease_expired",
    "stale_advisory",
    "ledger_unavailable",
    "window_open",
    "mint_lost",
    "deferred_settlement",
]
SpendLeaseNoLeaseReason = SpendLeaseEligibilityFailure | SpendLeaseBindingFailure
SpendLeaseBindingOutcome = Literal[
    "not_eligible",
    "ordinary",
    "replay",
    "reuse_bound",
    "mint_bound",
    "escrow_refused",
    "scope_claimed",
    "fence_lost_race",
    "fence_stale_advisory",
    "fence_count_exhausted",
    "fence_window_open",
    "mint_lost",
    "ledger_unavailable",
]


def spend_lease_binding_ineligibility_reason(
    stage_a_reason: SpendLeaseEligibilityFailure | None,
    *,
    deferred_settlement_applies: bool,
) -> SpendLeaseNoLeaseReason | None:
    """Extend Stage A only at the binding seam, leaving shadow issuance intact."""

    if stage_a_reason is not None:
        return stage_a_reason
    return "deferred_settlement" if deferred_settlement_applies else None


def spend_lease_scope_salt(idempotency_scope: str) -> str:
    """Return the stable four-hex-character Spanner arbitration key prefix."""

    return hashlib.sha256(idempotency_scope.encode("utf-8")).hexdigest()[:4]


@dataclass(frozen=True)
class SpendLeaseBoot:
    kid: str
    jwk: dict[str, str]
    approved: bool  # At-registration observation only; never an authorization gate.
    verified: bool
    image_digest: str
    attestation_kind: str
    registered_at: str


@dataclass(frozen=True)
class BootAuthHeader:
    kid: str
    signature: str


@dataclass(frozen=True)
class SpendLeaseArtifact:
    token: str
    lease_id: str
    cap_micro: int
    gen: int
    iat: int
    exp: int
    issuer_kid: str
    boot_kid: str
    catalog_version: str
    lease_status: LeaseStatus = "active"
    open_predecessor_count: int = 0
    local_admission_allowed: bool = False
    routing_policy_hash: str | None = None
    catalog: FrozenSpendLeaseCatalog | None = None


@dataclass(frozen=True)
class FrozenSpendLeaseCandidate:
    endpoint_id: str
    model: str
    provider: str
    region: str
    route_type: str
    service_tier: str | None
    price_tier_max_input_tokens: int | None
    input_price_micro_per_mtok: int
    output_price_micro_per_mtok: int
    request_price_micro: int
    cache_read_micro_per_mtok: int
    cache_write_micro_per_mtok: int
    upstream_model: str | None = None
    usage_type: str | None = None
    wafer_zdr_required: bool = False


@dataclass(frozen=True)
class SpendLeaseEchoValue:
    lease_id: str | None
    state: str
    remaining_micro: int | None
    enclave_estimate_micro: int | None
    catalog_version: str | None
    would_admit: bool | None


@dataclass(frozen=True)
class SpendLeaseShadowEvent:
    event_id: str
    created_at: str
    workspace_id: str
    key_hash: str
    boot_kid: str
    boot_verified: bool
    lease_id: str | None
    no_lease_reason: SpendLeaseNoLeaseReason | None
    binding_outcome: SpendLeaseBindingOutcome | None
    echo_state: str
    would_admit: bool | None
    enclave_estimate_micro: int | None
    server_estimate_micro: int | None
    server_verdict: ShadowVerdict
    catalog_version: str | None
    divergence: ShadowDivergence

    def payload(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}


def _has_window_limit(api_key: Any) -> bool:
    return any(
        getattr(api_key, field, None) is not None
        for field in (
            "limit_microdollars",
            "limit_daily_microdollars",
            "limit_weekly_microdollars",
            "limit_monthly_microdollars",
        )
    )


def spend_lease_ineligibility_reason(
    *,
    workspace_id: str,
    pilot_workspace_ids: frozenset[str],
    api_key: Any,
    route_type: str | None,
    endpoint_candidates: Sequence[tuple[Model, ModelEndpoint]],
    custom_model: Any | None,
    user_model: Any | None,
    partner_mode: Any | None,
    additional_cost_reservation_microdollars: int,
    native_batch_eligible: bool,
    app_markup_basis_points: int,
    receipt_fee_basis_points: int,
    regional_lease_authorization: bool,
) -> SpendLeaseEligibilityFailure | None:
    """Return the first failing Stage A cohort clause, or ``None`` if eligible.

    Keep the clauses visibly separate: each represents an independently tested
    security boundary and every fallback candidate must pass.
    """
    if workspace_id not in pilot_workspace_ids:
        return "not_pilot"
    if route_type not in SPEND_LEASE_ROUTE_TYPES:
        return "route_type"
    if not endpoint_candidates:
        return "no_candidates"
    if any(
        UsageType.for_endpoint(endpoint) != UsageType.CREDITS
        for _model, endpoint in endpoint_candidates
    ):
        return "candidate_not_credits"
    if custom_model is not None:
        return "custom_model"
    if user_model is not None:
        return "user_model"
    if partner_mode is not None:
        return "partner_mode"
    if additional_cost_reservation_microdollars != 0:
        return "additional_cost"
    if native_batch_eligible:
        return "native_batch"
    if app_markup_basis_points != 0:
        return "app_markup"
    if receipt_fee_basis_points != 0:
        return "receipt_fee"
    if regional_lease_authorization:
        return "regional_lease"
    if _has_window_limit(api_key):
        return "key_window_limit"
    return None


def spend_lease_eligible(
    *,
    workspace_id: str,
    pilot_workspace_ids: frozenset[str],
    api_key: Any,
    route_type: str | None,
    endpoint_candidates: Sequence[tuple[Model, ModelEndpoint]],
    custom_model: Any | None,
    user_model: Any | None,
    partner_mode: Any | None,
    additional_cost_reservation_microdollars: int,
    native_batch_eligible: bool,
    app_markup_basis_points: int,
    receipt_fee_basis_points: int,
    regional_lease_authorization: bool,
) -> bool:
    """Boolean compatibility wrapper for the Stage A cohort boundary."""
    return (
        spend_lease_ineligibility_reason(
            workspace_id=workspace_id,
            pilot_workspace_ids=pilot_workspace_ids,
            api_key=api_key,
            route_type=route_type,
            endpoint_candidates=endpoint_candidates,
            custom_model=custom_model,
            user_model=user_model,
            partner_mode=partner_mode,
            additional_cost_reservation_microdollars=(additional_cost_reservation_microdollars),
            native_batch_eligible=native_batch_eligible,
            app_markup_basis_points=app_markup_basis_points,
            receipt_fee_basis_points=receipt_fee_basis_points,
            regional_lease_authorization=regional_lease_authorization,
        )
        is None
    )


def _tiers(endpoint: ModelEndpoint) -> tuple[PriceTier, ...]:
    if endpoint.price_tiers:
        return endpoint.price_tiers
    return (
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=(
                endpoint.prompt_price_microdollars_per_million_tokens
            ),
            completion_price_microdollars_per_million_tokens=(
                endpoint.completion_price_microdollars_per_million_tokens
            ),
        ),
    )


def freeze_spend_lease_catalog(
    endpoint_candidates: Sequence[tuple[Model, ModelEndpoint]],
    *,
    region: str,
    route_type: str,
    service_tier: str | None,
    stage_c: bool = False,
) -> FrozenSpendLeaseCatalog:
    """Freeze complete applicability keys, retaining server tier order."""
    frozen: list[FrozenSpendLeaseCandidate] = []
    for model, endpoint in endpoint_candidates:
        endpoint = effective_endpoint(endpoint)
        priority = (
            openai_priority_pricing(endpoint.model_id)
            if service_tier in {"auto", "priority"} and endpoint.provider == "openai"
            else None
        )
        if priority is not None:
            frozen.append(
                FrozenSpendLeaseCandidate(
                    endpoint_id=endpoint.id,
                    model=model.id,
                    provider=endpoint.provider,
                    region=region,
                    route_type=route_type,
                    service_tier=service_tier,
                    price_tier_max_input_tokens=OPENAI_PRIORITY_MAX_PROMPT_TOKENS,
                    input_price_micro_per_mtok=(priority.prompt_microdollars_per_million_tokens),
                    output_price_micro_per_mtok=(
                        priority.completion_microdollars_per_million_tokens
                    ),
                    request_price_micro=endpoint.request_price_microdollars,
                    cache_read_micro_per_mtok=(
                        priority.cached_prompt_microdollars_per_million_tokens
                    ),
                    cache_write_micro_per_mtok=(
                        priority.cache_write_microdollars_per_million_tokens
                    ),
                    upstream_model=endpoint.upstream_id or model.id,
                    usage_type=UsageType.for_endpoint(endpoint).value,
                    wafer_zdr_required=(
                        endpoint.provider == "wafer"
                        and endpoint_zero_data_retention(endpoint) is True
                    ),
                )
            )
            continue
        for tier in _tiers(endpoint):
            cache_read, cache_write = cache_token_prices_microdollars(
                endpoint.provider,
                tier.prompt_price_microdollars_per_million_tokens,
            )
            if tier.prompt_cached_price_microdollars_per_million_tokens is not None:
                cache_read = tier.prompt_cached_price_microdollars_per_million_tokens
            frozen.append(
                FrozenSpendLeaseCandidate(
                    endpoint_id=endpoint.id,
                    model=model.id,
                    provider=endpoint.provider,
                    region=region,
                    route_type=route_type,
                    service_tier=service_tier,
                    price_tier_max_input_tokens=tier.max_prompt_tokens,
                    input_price_micro_per_mtok=(tier.prompt_price_microdollars_per_million_tokens),
                    output_price_micro_per_mtok=(
                        tier.completion_price_microdollars_per_million_tokens
                    ),
                    request_price_micro=endpoint.request_price_microdollars,
                    cache_read_micro_per_mtok=cache_read,
                    cache_write_micro_per_mtok=cache_write,
                    upstream_model=endpoint.upstream_id or model.id,
                    usage_type=UsageType.for_endpoint(endpoint).value,
                    wafer_zdr_required=(
                        endpoint.provider == "wafer"
                        and endpoint_zero_data_retention(endpoint) is True
                    ),
                )
            )
    candidates = [asdict(candidate) for candidate in frozen]
    if not stage_c:
        # Preserve Stage A/B token bytes until the Stage C acceptance flag is
        # enabled. These dispatch fields are consequential only to local
        # admission and therefore must not perturb flag-off leases.
        for candidate in candidates:
            for field in ("upstream_model", "usage_type", "wafer_zdr_required"):
                candidate.pop(field)
    canonical = json.dumps(candidates, separators=(",", ":"), sort_keys=True).encode()
    version = SPEND_LEASE_CATALOG_VERSION_PREFIX + hashlib.sha256(canonical).hexdigest()
    return {"version": version, "candidates": candidates}


def spend_lease_catalog_estimate(
    catalog: Mapping[str, Any],
    *,
    model: str,
    provider: str | None,
    route_type: str,
    region: str,
    service_tier: str | None,
    estimated_input_tokens: int,
    max_tokens: int | None,
) -> int | None:
    """Decision-5 estimator over a frozen catalog."""
    selected = spend_lease_catalog_candidates(
        catalog,
        model=model,
        provider=provider,
        route_type=route_type,
        region=region,
        service_tier=service_tier,
        estimated_input_tokens=estimated_input_tokens,
    )
    if selected is None:
        return None
    applicable: list[int] = []
    for raw in selected:
        request_price = int(raw["request_price_micro"])
        input_price = int(raw["input_price_micro_per_mtok"])
        output_price = int(raw["output_price_micro_per_mtok"])
        output_tokens = max_tokens if max_tokens is not None else 512
        cost = (
            request_price
            + token_cost_microdollars(estimated_input_tokens, input_price)
            + token_cost_microdollars(output_tokens, output_price)
        )
        positive = (
            request_price > 0
            or (estimated_input_tokens > 0 and input_price > 0)
            or (output_tokens > 0 and output_price > 0)
        )
        applicable.append(max(cost, 1) if positive else 0)
    return max(applicable) if applicable else None


def spend_lease_catalog_candidates(
    catalog: Mapping[str, Any],
    *,
    model: str,
    provider: str | None,
    route_type: str,
    region: str,
    service_tier: str | None,
    estimated_input_tokens: int,
) -> tuple[dict[str, Any], ...] | None:
    """Select one frozen price tier per endpoint, preserving snapshot order."""

    candidates = catalog.get("candidates")
    if not isinstance(candidates, list):
        return None
    applicable: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        endpoint_id = str(raw.get("endpoint_id") or "")
        if endpoint_id in seen_endpoints:
            continue
        if raw.get("model") != model:
            continue
        if provider is not None and raw.get("provider") != provider:
            continue
        if raw.get("route_type") != route_type:
            continue
        if raw.get("region") != region:
            continue
        if raw.get("service_tier") != service_tier:
            continue
        bound = raw.get("price_tier_max_input_tokens")
        if bound is not None and estimated_input_tokens > int(bound):
            continue
        seen_endpoints.add(endpoint_id)
        applicable.append({str(key): value for key, value in raw.items()})
    return tuple(applicable) or None


def parse_boot_auth_header(value: str | None) -> BootAuthHeader | None:
    """Parse the v1 boot-auth header, rejecting malformed or ambiguous values."""
    if value is None:
        return None
    fields: dict[str, str] = {}
    for part in value.split(","):
        name, separator, field_value = part.strip().partition("=")
        if not separator or name in fields:
            return None
        fields[name] = field_value
    if set(fields) != {"kid", "sig"}:
        return None
    kid = fields["kid"]
    signature = fields["sig"]
    if not kid or len(kid) > 128 or not signature or len(signature) > 256:
        return None
    return BootAuthHeader(kid=kid, signature=signature)


def boot_auth_digest(method: str, path: str, exact_body_bytes: bytes) -> bytes:
    body_digest = hashlib.sha256(exact_body_bytes).digest()
    material = (
        BOOT_AUTH_DOMAIN + method.upper().encode("utf-8") + path.encode("utf-8") + body_digest
    )
    return hashlib.sha256(material).digest()


def verify_boot_auth(
    *,
    boot: SpendLeaseBoot | None,
    auth: BootAuthHeader,
    method: str,
    path: str,
    exact_body_bytes: bytes,
    signed_lookup_hash: str | None,
    resolved_lookup_hash: str,
    accepted_image_digests: Collection[str],
) -> bool:
    """Verify a boot against the current trust config and its signed request."""
    return (
        _boot_auth_failure_reason(
            boot=boot,
            auth=auth,
            method=method,
            path=path,
            exact_body_bytes=exact_body_bytes,
            signed_lookup_hash=signed_lookup_hash,
            resolved_lookup_hash=resolved_lookup_hash,
            accepted_image_digests=accepted_image_digests,
        )
        is None
    )


def boot_auth_fails_only_on_digest_approval(
    *,
    boot: SpendLeaseBoot | None,
    auth: BootAuthHeader,
    method: str,
    path: str,
    exact_body_bytes: bytes,
    signed_lookup_hash: str | None,
    resolved_lookup_hash: str,
    accepted_image_digests: Collection[str],
) -> bool:
    """Return whether every boot-auth gate except current digest approval passed."""
    return (
        _boot_auth_failure_reason(
            boot=boot,
            auth=auth,
            method=method,
            path=path,
            exact_body_bytes=exact_body_bytes,
            signed_lookup_hash=signed_lookup_hash,
            resolved_lookup_hash=resolved_lookup_hash,
            accepted_image_digests=accepted_image_digests,
        )
        == "boot_digest_not_accepted"
    )


def _boot_auth_failure_reason(
    *,
    boot: SpendLeaseBoot | None,
    auth: BootAuthHeader,
    method: str,
    path: str,
    exact_body_bytes: bytes,
    signed_lookup_hash: str | None,
    resolved_lookup_hash: str,
    accepted_image_digests: Collection[str],
) -> Literal["boot_auth_invalid", "boot_digest_not_accepted"] | None:
    try:
        if boot is None or not boot.verified:
            return "boot_auth_invalid"
        if auth.kid != boot.kid:
            return "boot_auth_invalid"
        if signed_lookup_hash != resolved_lookup_hash:
            return "boot_auth_invalid"
        signature = b64url_decode(auth.signature)
        public_bytes = b64url_decode(normalize_receipt_jwk(boot.jwk)["x"])
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            boot_auth_digest(method, path, exact_body_bytes),
        )
        if boot.image_digest not in accepted_image_digests:
            return "boot_digest_not_accepted"
        return None
    except (TypeError, ValueError):
        return "boot_auth_invalid"
    except Exception:
        return "boot_auth_invalid"


class SpendLeaseSigner:
    """Lazy compact Ed25519 JWS signer backed by one 32-byte seed."""

    def __init__(self, seed_loader: Callable[[], bytes]) -> None:
        self._seed_loader = seed_loader
        self._private_key: Ed25519PrivateKey | None = None
        self._lock = threading.Lock()

    def _key(self) -> Ed25519PrivateKey:
        if self._private_key is None:
            with self._lock:
                if self._private_key is None:
                    seed = self._seed_loader()
                    if len(seed) != 32:
                        raise ValueError("spend-lease signing seed must be exactly 32 bytes")
                    self._private_key = Ed25519PrivateKey.from_private_bytes(seed)
        return self._private_key

    @property
    def kid(self) -> str:
        public = self._key().public_key().public_bytes_raw()
        return b64url_encode(hashlib.sha256(public).digest())

    def sign(self, claims: Mapping[str, Any]) -> str:
        header = {"alg": "EdDSA", "kid": self.kid, "typ": SPEND_LEASE_TYP}
        header_segment = b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
        )
        claims_segment = b64url_encode(
            json.dumps(dict(claims), separators=(",", ":"), sort_keys=True).encode()
        )
        signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
        return f"{header_segment}.{claims_segment}.{b64url_encode(self._key().sign(signing_input))}"


def decode_secret_seed(payload: bytes) -> bytes:
    stripped = payload.strip()
    if len(stripped) == 32:
        return stripped
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except ValueError:
        decoded = b""
    if len(decoded) == 32:
        return decoded
    try:
        decoded = bytes.fromhex(stripped.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        decoded = b""
    if len(decoded) != 32:
        raise ValueError("Secret Manager spend-lease seed is not 32 raw/base64/hex bytes")
    return decoded


def mint_shadow_spend_lease(
    *,
    signer: SpendLeaseSigner,
    key_hash: str,
    workspace_id: str,
    boot_kid: str,
    cap_micro: int,
    gen: int,
    catalog: Mapping[str, Any],
    ttl_seconds: int,
    now: int | None = None,
    trust_tier: int | None = None,
) -> SpendLeaseArtifact:
    issued_at = int(time.time()) if now is None else int(now)
    lease_id = str(uuid.uuid4())
    claims = {
        "v": 1,
        "typ": SPEND_LEASE_TYP,
        "authoritative": False,
        "lease_id": lease_id,
        "key_hash": key_hash,
        "workspace_id": workspace_id,
        "cohort": SPEND_LEASE_COHORT,
        "cap_micro": int(cap_micro),
        "gen": int(gen),
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
        "boot_kid": boot_kid,
        "catalog": dict(catalog),
    }
    if trust_tier is not None:
        claims["trust_tier"] = int(trust_tier)
    token = signer.sign(claims)
    return SpendLeaseArtifact(
        token=token,
        lease_id=lease_id,
        cap_micro=int(cap_micro),
        gen=int(gen),
        iat=issued_at,
        exp=issued_at + int(ttl_seconds),
        issuer_kid=signer.kid,
        boot_kid=boot_kid,
        catalog_version=str(catalog["version"]),
    )


def lease_status(artifact: SpendLeaseArtifact, *, now: int | None = None) -> LeaseStatus:
    if artifact.lease_status == "terminal":
        return "terminal"
    current = int(time.time()) if now is None else int(now)
    return "expired" if current >= artifact.exp else "active"


def build_spend_lease_shadow_event(
    *,
    event_id: str,
    created_at: str,
    workspace_id: str,
    key_hash: str,
    boot_kid: str,
    boot_verified: bool,
    no_lease_reason: SpendLeaseNoLeaseReason | None,
    binding_outcome: SpendLeaseBindingOutcome | None = None,
    echo: SpendLeaseEchoValue | None,
    server_estimate_micro: int | None,
    server_verdict: ShadowVerdict,
) -> SpendLeaseShadowEvent:
    divergence: ShadowDivergence = "none"
    if not boot_verified or echo is None:
        divergence = "echo_invalid"
    elif echo.would_admit is True and server_verdict != "accepted":
        divergence = "admit_diverged"
    elif (
        echo.enclave_estimate_micro is not None
        and server_estimate_micro is not None
        and echo.enclave_estimate_micro < server_estimate_micro
    ):
        divergence = "estimate_low"
    return SpendLeaseShadowEvent(
        event_id=event_id,
        created_at=created_at,
        workspace_id=workspace_id,
        key_hash=key_hash,
        boot_kid=boot_kid,
        boot_verified=boot_verified,
        lease_id=echo.lease_id if echo else None,
        no_lease_reason=no_lease_reason,
        binding_outcome=binding_outcome,
        echo_state=echo.state if echo else "missing",
        would_admit=echo.would_admit if echo else None,
        enclave_estimate_micro=echo.enclave_estimate_micro if echo else None,
        server_estimate_micro=server_estimate_micro,
        server_verdict=server_verdict,
        catalog_version=echo.catalog_version if echo else None,
        divergence=divergence,
    )
