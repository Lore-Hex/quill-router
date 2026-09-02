"""/internal/gateway/{authorize,settle,refund} — the cross-request
reservation handle the attested gateway uses.

Authorize reserves credits + per-key spend cap and returns an
authorization id. Settle and refund land on a separate request from
the authorize call (the enclave settles after streaming finishes).
The reservation is one-shot: a second settle on the same authorization
returns already_settled=True without double-charging.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from functools import lru_cache
from time import perf_counter
from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks, Request
from starlette.concurrency import run_in_threadpool

from trusted_router.acquisition import (
    record_free_credit_exhausted_safely,
    record_successful_api_call_safely,
)
from trusted_router.app_markup_billing import (
    APP_MARKUP_APP_ID_SETTLE_FIELD,
    APP_MARKUP_OWNER_SETTLE_FIELD,
    APP_MARKUP_PAYOUT_SETTLE_FIELD,
    app_markup_microdollars,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.auth import SettingsDep, is_api_key_expired
from trusted_router.byok_crypto import byok_cache_key, encrypted_secret_payload
from trusted_router.catalog import (
    MODELS,
    MONITOR_MODEL_ID,
    PROVIDERS,
    Model,
    ModelEndpoint,
    cache_token_prices_microdollars,
    default_endpoint_for_model,
    effective_endpoint,
    endpoint_for_id,
    endpoint_zero_data_retention,
)
from trusted_router.client_context import parse_client_context, parse_gateway_request_id
from trusted_router.config import Settings, get_settings
from trusted_router.custom_model_billing import (
    USER_MODEL_ID_SETTLE_FIELD,
    USER_MODEL_OWNER_SETTLE_FIELD,
    USER_MODEL_PAYOUT_SETTLE_FIELD,
    custom_model_cost_microdollars,
    owner_share_microdollars,
    user_model_payout_event_id,
)
from trusted_router.errors import api_error, assert_workspace_billing_active
from trusted_router.money import money_pair, token_cost_microdollars
from trusted_router.openai_service_tiers import (
    OPENAI_PRIORITY_MAX_PROMPT_TOKENS,
    OPENAI_SERVICE_TIERS,
    openai_priority_cost_microdollars,
    openai_priority_pricing,
)
from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PARTNER_OPERATOR_COST_SETTLE_FIELD,
    PartnerBillingMode,
    partner_billing_mode,
    partner_cost_microdollars,
)
from trusted_router.pricing import (
    SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
    resolve_request_rates,
    signed_receipt_price_microdollars,
)
from trusted_router.provider_compat import byok_storage_provider_candidates
from trusted_router.provider_contracts import (
    SAKANA_FUGU_MODEL_ID,
    provider_model_available_from_gateway_region,
    provider_model_requires_exact_global_settlement,
)
from trusted_router.provider_types import estimate_tokens_from_text
from trusted_router.receipt_keys import (
    AWS_ATTESTATION_KIND,
    AZURE_ATTESTATION_KIND,
    GCP_ATTESTATION_KIND,
    attestation_commits_to_jwk,
    gcp_attestation_image_digest,
    normalize_receipt_jwk,
    receipt_kid,
    verify_gcp_attestation_chain,
)
from trusted_router.regional_quota_ledger import RegionalLeaseLedgerError
from trusted_router.regions import choose_region, region_payload
from trusted_router.request_attribution import (
    InvalidAttribution,
    validate_request_attribution,
)
from trusted_router.request_tags import InvalidTags, merge_tags, tags_match, validate_tags
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.routing import (
    chat_route_endpoint_candidates,
    embeddings_route_endpoint_candidates,
    image_route_endpoint_candidates,
    provider_route_preferences,
    resolved_route_preferences,
    video_route_endpoint_candidates,
)
from trusted_router.schemas import (
    GatewayAuthorizeRequest,
    GatewayResolveCustomModelRequest,
    GatewaySettleRequest,
    GatewayValidateRequest,
    SpendLeaseBootRegistrationRequest,
)
from trusted_router.scopes import SCOPE_INFERENCE
from trusted_router.security import lookup_hash_api_key
from trusted_router.services import federation
from trusted_router.services.broadcast import (
    drain_broadcast_queue,
    enqueue_metadata_broadcast,
    gateway_destination_payload,
    should_drain_inline,
)
from trusted_router.services.federation import FederationClient, FederationUnavailable
from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission
from trusted_router.services.receipt_key_collector import collect_receipt_keys
from trusted_router.services.regional_quota_leases import LeaseSettlementError
from trusted_router.services.settle_outbox_apply import normalized_prompt_accounting
from trusted_router.services.settle_outbox_drain import (
    drain_settle_outbox,
    spanner_settle_outbox,
)
from trusted_router.services.spend_lease_settlement import (
    clamp_spend_lease_charge,
    mirror_finalized_spend_lease_best_effort,
)
from trusted_router.services.spend_lease_shadow_dispatch import (
    SpendLeaseShadowDispatcher,
)
from trusted_router.services.user_model_gateway_health import (
    record_user_model_gateway_result,
)
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SECRET_NAMESPACE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.services.user_model_slots import (
    acquire_user_model_slot,
    release_user_model_slot,
)
from trusted_router.spend_leases import (
    BootAuthHeader,
    SpendLeaseArtifact,
    SpendLeaseBoot,
    SpendLeaseEchoValue,
    SpendLeaseNoLeaseReason,
    SpendLeaseSigner,
    boot_auth_fails_only_on_digest_approval,
    build_spend_lease_shadow_event,
    freeze_spend_lease_catalog,
    mint_shadow_spend_lease,
    parse_boot_auth_header,
    spend_lease_binding_ineligibility_reason,
    spend_lease_ineligibility_reason,
    verify_boot_auth,
)
from trusted_router.storage import (
    STORE,
    Generation,
    ProviderBenchmarkSample,
    typed_billing_store,
)
from trusted_router.storage_custom_models import is_custom_model_id, normalize_custom_model_id
from trusted_router.storage_errors import (
    DeferredSettlementCapReached,
    StoreConflict,
    conflict_store_error_types,
    transient_store_error_types,
)
from trusted_router.storage_gcp_io import spanner_rpc_budget
from trusted_router.storage_gcp_secrets import secret_manager_seed_loader
from trusted_router.storage_models import (
    AppMarkupPayout,
    GatewayAuthorization,
    SettleOutboxRow,
    TypedFinalizeResult,
    UserModelPayout,
    UserProvidedModel,
    iso_now,
)
from trusted_router.synthetic.fleet import record_heartbeat
from trusted_router.synthetic.funding import ensure_monitor_funding, monitor_lookup_hash
from trusted_router.types import ErrorType, UsageType
from trusted_router.user_model_rules import (
    GATEWAY_RESERVATION_TTL_SECONDS,
    dispatch_budget,
    is_owner_fault,
    user_model_gateway_pair,
    user_model_is_on_the_clock,
)

logger = logging.getLogger(__name__)
REQUEST_METADATA_VERSION = 1
# The enclave stops waiting for response headers after 25 seconds. Preserve the
# transaction layer's 20-second retry budget while leaving five seconds for this
# process to serialize a structured retryable 503 and for network transit.
_BILLING_PATH_SPANNER_BUDGET_SECONDS = 20.0
# Spend-lease shadow rows are non-authoritative rollout evidence. The dispatcher
# keeps their outbox transaction off the paid authorize thread, so cross-region
# Spanner retries can use a realistic budget without adding customer latency.
# Its bounded queue still caps memory and sheds oldest evidence during an outage.
_SPEND_LEASE_SHADOW_SPANNER_BUDGET_SECONDS = 5.0
_AUTHORIZE_ADMISSION = KeyedConcurrencyAdmission()
# Process-local harm limitation: this keeps one key from exhausting this
# instance's Spanner session pool. It cannot stop a fleet-wide settle convoy;
# at the 2026-08-25 incident scale, roughly 76 concurrent settles still survive
# the independent per-instance gates and can contend on the same Spanner key.
_SETTLE_ADMISSION = KeyedConcurrencyAdmission()
_BROADCAST_EMPTY_CACHE_TTL_SECONDS = 60.0
_BROADCAST_EMPTY_CACHE_MAX_ENTRIES = 1_024
_BROADCAST_EMPTY_CACHE: OrderedDict[str, float] = OrderedDict()
_BROADCAST_EMPTY_CACHE_LOCK = threading.Lock()
_SPEND_LEASE_SIGNERS: dict[tuple[str, str], SpendLeaseSigner] = {}
_SPEND_LEASE_SIGNERS_LOCK = threading.Lock()
_SPEND_LEASE_WIRE_ATTESTATION_KINDS = {
    "aws": AWS_ATTESTATION_KIND,
    "azure": AZURE_ATTESTATION_KIND,
    "gcp": GCP_ATTESTATION_KIND,
}
_NATIVE_BATCH_ROUTE_PREFIX = "batch.native."
_ADDITIONAL_COST_ROUTE_TYPES = frozenset(
    {
        "responses.web_search.planner",
        "chat.completions.web_search.planner",
        "images",
        "videos",
    }
)
_NATIVE_BATCH_BILLED_FRACTION_BPS = {
    "openai": 5_000,
    "parasail": 5_000,
}
_NATIVE_BATCH_PROVIDER_FIELDS = frozenset(
    {
        "order",
        "allow_fallbacks",
        "require_parameters",
        "data_collection",
        "min_privacy",
        "jurisdiction",
        "usage",
        "only",
        "ignore",
        "quantizations",
        "sort",
        "max_price",
    }
)
_SETTLE_REPAIR_FIELDS = frozenset(
    {
        "authorization_id",
        "client",
        "gateway_request_id",
        "actual_input_tokens",
        "actual_output_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "service_tier",
        "request_id",
        "finish_reason",
        "status",
        "streamed",
        "usage_estimated",
        "elapsed_seconds",
        "first_token_seconds",
        "first_byte_seconds",
        "time_to_first_token_seconds",
        "time_to_first_byte_seconds",
        "cached_input_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "error_status",
        "error_type",
        "app",
        "model",
        "selected_model",
        "endpoint",
        "selected_endpoint",
        "user",
        "session_id",
        "http_referer",
        "app_categories",
        "route_type",
        "additional_cost_microdollars",
        "video_input_mode",
        "video_duration_seconds",
        "video_resolution",
        "video_aspect_ratio",
        "video_generate_audio",
    }
)


def _spend_lease_signer(settings: Settings) -> SpendLeaseSigner:
    cache_key = (settings.gcp_project_id, settings.spend_lease_signing_secret_name)
    with _SPEND_LEASE_SIGNERS_LOCK:
        signer = _SPEND_LEASE_SIGNERS.get(cache_key)
        if signer is None:
            signer = SpendLeaseSigner(
                secret_manager_seed_loader(
                    project_id=settings.gcp_project_id,
                    secret_name=settings.spend_lease_signing_secret_name,
                )
            )
            _SPEND_LEASE_SIGNERS[cache_key] = signer
        return signer


def _register_spend_lease_boot_sync(
    request: Request,
    body: SpendLeaseBootRegistrationRequest,
    settings: Settings,
) -> dict[str, Any]:
    require_internal_gateway(request, settings)
    try:
        jwk = normalize_receipt_jwk(body.receipt_public_key)
        kid = receipt_kid(jwk)
        if body.kid != kid:
            raise ValueError("kid does not match the receipt public key")
        attestation_kind = _SPEND_LEASE_WIRE_ATTESTATION_KINDS.get(
            body.attestation_kind,
            body.attestation_kind,
        )
        if not attestation_commits_to_jwk(
            body.attestation_evidence,
            attestation_kind,
            jwk,
        ):
            raise ValueError("attestation does not commit to the receipt public key")
        verified = False
        approved_at_registration = False
        image_digest = ""
        if attestation_kind == GCP_ATTESTATION_KIND:
            verify_gcp_attestation_chain(body.attestation_evidence)
            image_digest = gcp_attestation_image_digest(body.attestation_evidence)
            verified = True
            approved_at_registration = image_digest in settings.spend_lease_accepted_gcp_digests
        record = SpendLeaseBoot(
            kid=kid,
            jwk=jwk,
            approved=approved_at_registration,
            verified=verified,
            image_digest=image_digest,
            attestation_kind=attestation_kind,
            registered_at=iso_now(),
        )
        stored = STORE.observe_spend_lease_boot(record)
    except ValueError as exc:
        raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
    return {"data": {"verified": stored.verified}}


async def authorize_gateway(
    request: Request,
    body: GatewayAuthorizeRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Async entrypoint for the authorize path.

    The work below is entirely synchronous storage IO (no awaits), so run it in a
    worker thread via ``run_in_threadpool``. Left on the event loop, ONE contended
    workspace's slow authorize/reserve transaction stalls EVERY in-flight request
    sharing the loop (head-of-line blocking) — so unrelated requests fail even
    though they have nothing to do with the slow one. Offloading keeps the loop
    free while this request's blocking storage runs; the response is byte-identical.
    Kept ``async`` so callers/tests await it unchanged.
    """
    subject = body.api_key_lookup_hash or body.api_key_hash
    assert subject is not None  # GatewayAuthorizeRequest validates one identifier.
    if not _AUTHORIZE_ADMISSION.try_acquire(
        subject,
        limit=settings.gateway_authorize_max_in_flight_per_key,
    ):
        logger.warning(
            "billing.authorize_admission_limited",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "key_subject_fingerprint": hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16],
                "limit": settings.gateway_authorize_max_in_flight_per_key,
            },
        )
        raise api_error(
            429,
            "Too many concurrent requests for this API key",
            ErrorType.RATE_LIMITED,
            headers={"Retry-After": "1"},
        )
    try:
        try:
            raw_body = await request.body()
        except RuntimeError:
            # Direct unit callers may construct a Request without an ASGI receive
            # channel. Real routed requests always provide the exact cached bytes.
            raw_body = b""
        return await run_in_threadpool(
            _authorize_gateway_sync,
            request,
            body,
            settings,
            raw_body,
        )
    finally:
        _AUTHORIZE_ADMISSION.release(subject)


async def settle_gateway(
    request: Request,
    body: GatewaySettleRequest,
    settings: Settings,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    """Run one settlement off-loop behind the process-local per-key gate."""
    require_internal_gateway(request, settings)
    return await run_in_threadpool(
        _settle_gateway_with_admission_sync,
        body,
        settings=settings,
        background_tasks=background_tasks,
    )


@spanner_rpc_budget(_BILLING_PATH_SPANNER_BUDGET_SECONDS)
def _authorize_gateway_sync(
    request: Request,
    body: GatewayAuthorizeRequest,
    settings: Settings,
    raw_body: bytes = b"",
) -> dict[str, Any]:
    boot_auth_headers = request.headers.getlist("X-TR-Boot-Auth")
    boot_auth = (
        parse_boot_auth_header(boot_auth_headers[0]) if len(boot_auth_headers) == 1 else None
    )
    echo = (
        SpendLeaseEchoValue(**body.spend_lease_echo.model_dump())
        if body.spend_lease_echo is not None
        else None
    )
    context: dict[str, Any] = {
        "raw_body": raw_body,
        "workspace_id": "",
        "key_hash": str(body.api_key_hash or body.api_key_lookup_hash or ""),
        "boot_auth": boot_auth,
        "boot_kid": boot_auth.kid if boot_auth is not None else "",
        "boot_verified": False,
        "boot_failure_reason": None,
        "no_lease_reason": None,
        "echo": echo,
        "server_estimate_micro": None,
    }
    try:
        response = _authorize_gateway_sync_impl(request, body, settings, context)
    except Exception as exc:
        if settings.spend_lease_issuance_enabled:
            status = int(getattr(exc, "status_code", 500))
            verdict = "declined_funds" if status in {402, 429} else "declined_other"
            _record_spend_lease_shadow(context, verdict=verdict)
        raise
    if settings.spend_lease_issuance_enabled:
        data = response.get("data")
        if isinstance(data, dict):
            context["event_id"] = str(data.get("authorization_id") or uuid.uuid4())
        _record_spend_lease_shadow(context, verdict="accepted")
    return response


@spanner_rpc_budget(_SPEND_LEASE_SHADOW_SPANNER_BUDGET_SECONDS)
def _persist_spend_lease_shadow(event_id: str, payload: dict[str, Any]) -> None:
    STORE.record_spend_lease_shadow(event_id, payload)


_SPEND_LEASE_SHADOW_DISPATCHER = SpendLeaseShadowDispatcher(_persist_spend_lease_shadow)


def _record_spend_lease_shadow(
    context: dict[str, Any],
    *,
    verdict: str,
) -> None:
    try:
        event = build_spend_lease_shadow_event(
            event_id=str(context.get("event_id") or uuid.uuid4()),
            created_at=iso_now(),
            workspace_id=str(context.get("workspace_id") or ""),
            key_hash=str(context.get("key_hash") or ""),
            boot_kid=str(context.get("boot_kid") or ""),
            boot_verified=bool(context.get("boot_verified")),
            no_lease_reason=cast(
                SpendLeaseNoLeaseReason | None,
                context.get("no_lease_reason"),
            ),
            binding_outcome=cast(Any, context.get("binding_outcome")),
            echo=cast(SpendLeaseEchoValue | None, context.get("echo")),
            server_estimate_micro=cast(int | None, context.get("server_estimate_micro")),
            server_verdict=cast(Any, verdict),
        )
        _SPEND_LEASE_SHADOW_DISPATCHER.submit(event.event_id, event.payload())
    except Exception:
        logger.exception("spend_lease_shadow_dispatch_failed")


def _authorize_gateway_sync_impl(
    request: Request,
    body: GatewayAuthorizeRequest,
    settings: Settings,
    spend_context: dict[str, Any],
) -> dict[str, Any]:
    """Core gateway-authorize logic, extracted from the route closure so it is a
    named, directly unit-testable function (#40). The registered route handler is
    a thin wrapper; behavior is byte-identical to the prior inline handler."""
    require_internal_gateway(request, settings)
    api_key = _api_key_for_gateway_authorization(body)
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    _assert_gateway_key_scope(api_key)
    spend_context["workspace_id"] = api_key.workspace_id
    spend_context["key_hash"] = api_key.hash
    boot_auth = cast(BootAuthHeader | None, spend_context["boot_auth"])
    if settings.spend_lease_issuance_enabled and boot_auth is not None:
        boot = STORE.get_spend_lease_boot(boot_auth.kid)
        accepted_image_digests = settings.spend_lease_accepted_gcp_digests
        spend_context["boot_verified"] = verify_boot_auth(
            boot=boot,
            auth=boot_auth,
            method=request.method,
            path=request.url.path,
            exact_body_bytes=cast(bytes, spend_context["raw_body"]),
            signed_lookup_hash=body.api_key_lookup_hash,
            resolved_lookup_hash=api_key.lookup_hash,
            accepted_image_digests=accepted_image_digests,
        )
        if not spend_context["boot_verified"] and boot_auth_fails_only_on_digest_approval(
            boot=boot,
            auth=boot_auth,
            method=request.method,
            path=request.url.path,
            exact_body_bytes=cast(bytes, spend_context["raw_body"]),
            signed_lookup_hash=body.api_key_lookup_hash,
            resolved_lookup_hash=api_key.lookup_hash,
            accepted_image_digests=accepted_image_digests,
        ):
            spend_context["boot_failure_reason"] = "boot_digest_not_accepted"
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
    if workspace.billing_paused:
        # Keep tenant attribution in the rendered message because Cloud Run's
        # stderr collector does not preserve arbitrary LogRecord extras. This
        # is billing metadata only: never include the raw key, body, or content.
        logger.warning(
            "billing.authorize_workspace_paused workspace_id=%s request_id=%s",
            workspace.id,
            getattr(request.state, "request_id", None),
        )
    assert_workspace_billing_active(workspace)
    # The synthetic monitor funds itself: a monthly idempotent grant applied
    # on its own authorize path, so a dry monitor self-heals on the next
    # probe instead of failing 402 for days (invisible availability loss —
    # the deep probes stop proving anything). One set-lookup for monitor
    # traffic, zero cost for everyone else.
    monitor_hash = monitor_lookup_hash(settings)
    if monitor_hash is not None and body.api_key_lookup_hash == monitor_hash:
        ensure_monitor_funding(STORE, settings, workspace.id)
    body_dict = body.model_dump(exclude_none=True)
    # Preserve pre-web-search idempotency fingerprints byte-for-byte for every
    # ordinary request. A nonzero hosted-tool reservation remains fingerprinted.
    if not body.additional_cost_reservation_microdollars:
        body_dict.pop("additional_cost_reservation_microdollars", None)
    if not body.inference_receipt:
        # Keep pre-receipt idempotency fingerprints stable for ordinary calls.
        body_dict.pop("inference_receipt", None)
    try:
        request_tags = validate_tags(body.tags)
        effective_tags = merge_tags(api_key.tags, body.tags)
    except InvalidTags as exc:
        raise api_error(400, str(exc), ErrorType.INVALID_TAGS) from exc
    try:
        attribution = validate_request_attribution(
            user=body.user,
            session_id=body.session_id,
            trace=body.trace,
            app=body.app,
            http_referer=body.http_referer,
            app_categories=body.app_categories,
        )
    except InvalidAttribution as exc:
        raise api_error(400, str(exc), ErrorType.INVALID_REQUEST_METADATA) from exc
    for key in ("user", "session_id", "trace", "app", "http_referer", "app_categories"):
        body_dict.pop(key, None)
    body_dict.update(attribution.body_fields())
    _require_monitor_model_key(body_dict, api_key.lookup_hash, settings)
    requested_model_id = body.model
    if any(is_custom_model_id(model_id) for model_id in (body.models or [])):
        raise api_error(
            400,
            "Custom models cannot be used with models fallback arrays in v1",
            ErrorType.BAD_REQUEST,
        )
    custom_model = None
    user_model = None
    if is_custom_model_id(requested_model_id):
        normalized = normalize_custom_model_id(requested_model_id)
        custom_model = STORE.get_custom_model(normalized)
        if custom_model is not None:
            if not custom_model.enabled:
                raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
            body_dict["model"] = custom_model.base_model_id
            body_dict.pop("models", None)
            body_dict["custom_model_id"] = custom_model.id
            body_dict["custom_model_revision"] = custom_model.revision
            _force_custom_model_credit_routes(body_dict)
        else:
            user_model = STORE.get_user_model(normalized)
            if (
                user_model is None
                or not user_model.enabled
                or user_model.status != "active"
                # Serving is gated until settle/refund exist for these
                # authorizations; an unreleasable hold is worse than a 404.
                or not settings.user_models_dispatch_enabled
            ):
                raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
            if not user_model_is_on_the_clock(user_model, datetime.now(dt.UTC)):
                raise api_error(
                    503,
                    f"User-provided {user_model.kind} model {user_model.id} is off the clock",
                    ErrorType.MODEL_OFF_THE_CLOCK,
                )
            # Same fingerprint discipline as prompt wrappers: a same-key retry
            # after a material edit must 409, not replay stale frozen prices.
            body_dict.pop("models", None)
            body_dict["custom_model_id"] = user_model.id
            body_dict["custom_model_revision"] = user_model.revision
            _force_custom_model_credit_routes(
                body_dict,
                error_message="User-provided models do not support BYOK routes",
            )
    presented_idempotency_key = _gateway_idempotency_key(request, body)
    request_idempotency_key = presented_idempotency_key or str(uuid.uuid4())
    _require_native_batch_route_binding(body.route_type, request_idempotency_key)
    partner_mode = _partner_billing_mode_or_error(
        requested_model_id=requested_model_id,
        route_type=body.route_type,
        idempotency_key=request_idempotency_key,
    )
    if partner_mode is not None:
        _force_partner_credit_routes(body_dict)
    native_retention_allowed = _native_batch_request_allows_retention(body_dict, settings)
    # Embedding-only models can't go through the chat resolver (it
    # rejects supports_chat=False). Route them to the embeddings
    # resolver so the attested enclave can authorize + bill an
    # embeddings call exactly like a chat one.
    route_model_id = str(body_dict.get("model") or body.model)
    if body.additional_cost_reservation_microdollars and (
        _is_web_search_restricted_model(route_model_id)
        or _is_web_search_restricted_provider(body_dict.get("provider"))
    ):
        raise api_error(
            400,
            "web_search is not available for this privacy tier",
            ErrorType.BAD_REQUEST,
        )
    route_preferences = provider_route_preferences(body_dict)
    requested_model = MODELS.get(route_model_id) if route_model_id else None
    is_video_request = body.route_type == "videos"
    is_image_request = body.route_type == "images"
    is_embeddings_request = (
        requested_model is not None
        and requested_model.supports_embeddings
        and not requested_model.supports_chat
    )
    if user_model is not None:
        if is_image_request:
            raise api_error(
                400,
                "User-provided models do not support image generation",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        endpoint_candidates = [_user_model_gateway_candidate(user_model)]
    elif is_video_request:
        endpoint_candidates = video_route_endpoint_candidates(
            body_dict,
            settings,
            defer_no_fallback_selection=True,
        )
    elif is_image_request:
        if custom_model is not None:
            raise api_error(
                400,
                "Custom models do not support image generation",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        endpoint_candidates = image_route_endpoint_candidates(
            body_dict,
            settings,
            defer_no_fallback_selection=True,
        )
    elif is_embeddings_request:
        endpoint_candidates = embeddings_route_endpoint_candidates(
            body_dict,
            settings,
            defer_no_fallback_selection=True,
        )
        if not endpoint_candidates:
            raise api_error(400, "Model does not support embeddings", ErrorType.MODEL_NOT_SUPPORTED)
    else:
        endpoint_candidates = chat_route_endpoint_candidates(
            body_dict,
            settings,
            defer_no_fallback_selection=True,
        )
        if not endpoint_candidates:
            raise api_error(
                400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED
            )
    region = choose_region(settings, body.region or None)
    endpoint_candidates = [
        (candidate_model, candidate_endpoint)
        for candidate_model, candidate_endpoint in endpoint_candidates
        if provider_model_available_from_gateway_region(
            candidate_endpoint.provider,
            candidate_endpoint.model_id,
            region,
        )
    ]
    endpoint_candidates = _eligible_gateway_endpoint_candidates(endpoint_candidates, workspace.id)
    input_tokens = body.estimated_input_tokens
    if custom_model is not None and custom_model.hidden_prompt.strip():
        input_tokens += estimate_tokens_from_text(custom_model.hidden_prompt)
    service_tier = _requested_service_tier_or_error(body.service_tier)
    endpoint_candidates = _service_tier_endpoint_candidates_or_error(
        endpoint_candidates,
        service_tier=service_tier,
        estimated_input_tokens=input_tokens,
    )
    additional_cost_reservation = body.additional_cost_reservation_microdollars
    if additional_cost_reservation:
        if body.route_type not in _ADDITIONAL_COST_ROUTE_TYPES:
            raise api_error(
                400,
                "additional cost reservations are only available for hosted search, image, or video",
                ErrorType.BAD_REQUEST,
            )
        # Hosted tools and asynchronous media are operator-funded, so their
        # fixed cost must settle against Credits rather than a BYOK route.
        endpoint_candidates = [
            (candidate_model, candidate_endpoint)
            for candidate_model, candidate_endpoint in endpoint_candidates
            if UsageType.for_endpoint(candidate_endpoint) == UsageType.CREDITS
        ]
    # ``allow_fallbacks=false`` removes alternate models in the resolver, but
    # provider selection must happen after regional, workspace/BYOK, and
    # service-tier eligibility. Truncating the raw catalog first can pin an
    # unusable endpoint and reject a model another eligible provider serves.
    if not route_preferences.allow_fallbacks:
        endpoint_candidates = endpoint_candidates[:1]
    if not endpoint_candidates:
        raise api_error(
            400,
            "No authorized route candidates are available for this workspace",
            ErrorType.PROVIDER_NOT_SUPPORTED,
        )
    model, endpoint = endpoint_candidates[0]

    output_tokens = body.output_estimate
    model_estimate = (
        custom_model_cost_microdollars(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_price=user_model.prompt_price_microdollars_per_million_tokens,
            completion_price=user_model.completion_price_microdollars_per_million_tokens,
        )
        if user_model is not None
        else partner_cost_microdollars(
            partner_mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if partner_mode is not None
        else max(
            _endpoint_cost_microdollars(
                candidate_endpoint,
                input_tokens,
                output_tokens,
                service_tier=service_tier,
                reserve_auto=True,
            )
            for _candidate_model, candidate_endpoint in endpoint_candidates
        )
    )
    model_usage_type = UsageType.for_endpoint(endpoint)
    has_credit_candidate = any(
        UsageType.for_endpoint(candidate_endpoint) == UsageType.CREDITS
        for _candidate_model, candidate_endpoint in endpoint_candidates
    )
    reservation_usage_type = UsageType.CREDITS if has_credit_candidate else UsageType.BYOK
    receipt_fee_basis_points = (
        SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS if body.inference_receipt else 0
    )
    if receipt_fee_basis_points and user_model is None and partner_mode is None:
        model_estimate = signed_receipt_price_microdollars(model_estimate, receipt_fee_basis_points)
    estimate = model_estimate + additional_cost_reservation
    broadcast_destinations = [
        payload
        for destination in _broadcast_destinations_for_authorize(workspace.id)
        if (payload := gateway_destination_payload(destination)) is not None
    ]
    native_batch_eligible = _native_batch_eligibility(
        route_type=body.route_type,
        retention_allowed=native_retention_allowed,
        model_usage_type=model_usage_type,
        custom_model=custom_model,
        broadcast_destinations=broadcast_destinations,
        requested_model_id=requested_model_id,
        endpoint=endpoint,
    )
    fingerprint_body = dict(body_dict)
    if is_video_request or is_image_request:
        # Provider quotes can change between retries. The enclave supplies a
        # keyed content fingerprint, so media idempotency binds to the logical
        # request without storing content or coupling replay to a fresh quote.
        fingerprint_body.pop("additional_cost_reservation_microdollars", None)
    # Preserve the pre-tagging router's distinction between an absent tags
    # field and an explicitly supplied empty object. That lets an idempotent
    # retry carrying tags={} replay an authorization created before rollout.
    if body.tags is not None:
        fingerprint_body["tags"] = request_tags
    else:
        fingerprint_body.pop("tags", None)
    body_dict["tags"] = effective_tags
    request_fingerprint = _gateway_authorize_fingerprint(
        workspace_id=workspace.id,
        key_hash=api_key.hash,
        body=fingerprint_body,
        idempotency_key=request_idempotency_key,
    )

    def _replay_response(existing_authorization: Any) -> dict[str, Any]:
        # Build the replay response from the STORED authorization (NOT current
        # routing), so a replay across catalog/pricing/BYOK drift advertises
        # the endpoint that was actually authorized (codex 3e route review #1).
        existing_candidates = _authorization_endpoint_candidates(
            existing_authorization, endpoint_candidates
        )
        existing_model, existing_endpoint = existing_candidates[0]
        existing_usage_type = UsageType.for_endpoint(existing_endpoint)
        byok_config = (
            _get_byok_provider(workspace.id, existing_endpoint.provider)
            if existing_usage_type.is_byok()
            else None
        )
        return _gateway_authorize_response(
            authorization=existing_authorization,
            workspace_id=workspace.id,
            key_hash=api_key.hash,
            model=existing_model,
            endpoint=existing_endpoint,
            requested_model_id=requested_model_id,
            model_usage_type=existing_usage_type,
            limit_usage_type=UsageType.coerce(existing_authorization.usage_type),
            estimate=existing_authorization.estimated_microdollars,
            credit_reservation_id=existing_authorization.credit_reservation_id,
            byok_config=byok_config,
            region=existing_authorization.region or region,
            settings=settings,
            broadcast_destinations=broadcast_destinations,
            endpoint_candidates=existing_candidates,
            idempotent_replay=True,
            custom_model=custom_model,
        )

    _typed_store = typed_billing_store(STORE)
    if _typed_store is None:
        # Non-typed stores still use their legacy authorization index. Typed
        # Spanner never writes that entity index on the production route after
        # C1; its reservation transaction below owns idempotency and money
        # atomically, so probing either index here only adds happy-path RPCs.
        existing_authorization = STORE.get_gateway_authorization_by_idempotency_key(
            workspace.id, api_key.hash, request_idempotency_key
        )
        if existing_authorization is not None:
            if existing_authorization.idempotency_fingerprint != request_fingerprint:
                raise api_error(
                    409,
                    "Idempotency key was already used for a different gateway request",
                    ErrorType.CONFLICT,
                )
            return _replay_response(existing_authorization)
    # Suspension stops NEW authorizations. Resolve replay first so a lost
    # response can be recovered and in-flight work can settle under frozen terms.
    app_markup_basis_points, app_owner_user_id = _oauth_app_terms_for_key(api_key)
    if app_markup_basis_points > 0:
        # Settle-side caps compare against this frozen hold. If authorize held
        # only the base, those caps would claw the app markup back at settle.
        estimate += app_markup_microdollars(estimate, app_markup_basis_points)
    spend_context["server_estimate_micro"] = estimate
    # Minted up front so a user-model concurrency slot can be keyed by it
    # before the reservation exists. It is a fresh uuid — never derived from
    # the caller's Idempotency-Key: a deterministic id would outlive its
    # 30-day authorization row inside the 400-day tr_credit_movement PK
    # (silently dropping a later payout) and let a mismatching concurrent
    # request release the winner's slot.
    authorization_id = _new_gateway_authorization_id()
    regional_authorize = getattr(_typed_store, "authorize_gateway_regional", None)
    regional_eligible = (
        settings.regional_quota_leases_enabled
        and settings.regional_quota_lease_issuance_enabled
        and workspace.id in settings.regional_quota_lease_pilot_workspaces
        and callable(regional_authorize)
        and estimate > 0
        and body.route_type in {None, "chat.completions", "responses"}
        and all(
            UsageType.for_endpoint(candidate_endpoint) == UsageType.CREDITS
            for _candidate_model, candidate_endpoint in endpoint_candidates
        )
        and not any(
            provider_model_requires_exact_global_settlement(
                candidate_endpoint.provider,
                candidate_endpoint.model_id,
            )
            for _candidate_model, candidate_endpoint in endpoint_candidates
        )
        and api_key.limit_microdollars is None
        and api_key.limit_daily_microdollars is None
        and api_key.limit_weekly_microdollars is None
        and api_key.limit_monthly_microdollars is None
        and custom_model is None
        and user_model is None
        and partner_mode is None
        and additional_cost_reservation == 0
        and not native_batch_eligible
        and app_markup_basis_points == 0
        and receipt_fee_basis_points == 0
    )
    spend_lease: SpendLeaseArtifact | None = None
    spend_lease_binding_plan: Any | None = None
    no_lease_reason = spend_lease_ineligibility_reason(
        workspace_id=workspace.id,
        pilot_workspace_ids=settings.spend_lease_pilot_workspaces,
        api_key=api_key,
        route_type=body.route_type,
        endpoint_candidates=endpoint_candidates,
        custom_model=custom_model,
        user_model=user_model,
        partner_mode=partner_mode,
        additional_cost_reservation_microdollars=additional_cost_reservation,
        native_batch_eligible=native_batch_eligible,
        app_markup_basis_points=app_markup_basis_points,
        receipt_fee_basis_points=receipt_fee_basis_points,
        regional_lease_authorization=bool(regional_eligible),
    )
    spend_context["no_lease_reason"] = no_lease_reason or spend_context.get("boot_failure_reason")
    if (
        settings.spend_lease_issuance_enabled
        and not settings.spend_lease_binding_enabled
        and bool(spend_context["boot_verified"])
        and boot_auth is not None
        and no_lease_reason is None
    ):
        try:
            boot_kid = boot_auth.kid
            echo = cast(SpendLeaseEchoValue | None, spend_context.get("echo"))
            active = STORE.get_active_spend_lease(api_key.hash, boot_kid)
            echo_ends_active = bool(
                active is not None
                and echo is not None
                and echo.lease_id == active.lease_id
                and echo.state in {"exhausted", "expired", "terminal"}
            )
            active_expired = active is not None and active.exp <= int(time.time())
            replace_active = echo_ends_active or active_expired
            if active is not None and not replace_active:
                spend_lease = active
            else:
                snapshot_reader = getattr(STORE, "typed_credit_snapshot", None)
                snapshot = snapshot_reader(workspace.id) if callable(snapshot_reader) else None
                if snapshot is None:
                    memory_snapshot = getattr(STORE, "credit_money_snapshot", None)
                    snapshot = memory_snapshot(workspace.id) if callable(memory_snapshot) else None
                if snapshot is not None:
                    available = max(0, int(snapshot[0]) - int(snapshot[1]) - int(snapshot[2]))
                    post_request_headroom = max(0, available - estimate)
                    cap_micro = min(
                        settings.spend_lease_max_microdollars,
                        post_request_headroom
                        * settings.spend_lease_max_available_basis_points
                        // 10_000,
                    )
                    if cap_micro > 0:
                        generation = STORE.next_spend_lease_generation(
                            api_key.hash,
                            boot_kid,
                        )
                        catalog = freeze_spend_lease_catalog(
                            endpoint_candidates,
                            region=region,
                            route_type=cast(str, body.route_type),
                            service_tier=service_tier,
                        )
                        candidate = mint_shadow_spend_lease(
                            signer=_spend_lease_signer(settings),
                            key_hash=api_key.hash,
                            workspace_id=workspace.id,
                            boot_kid=boot_kid,
                            cap_micro=cap_micro,
                            gen=generation,
                            catalog=catalog,
                            ttl_seconds=settings.spend_lease_ttl_seconds,
                        )
                        spend_lease = STORE.retain_spend_lease(
                            api_key.hash,
                            boot_kid,
                            candidate,
                            replace=replace_active,
                        )
        except Exception:
            # Signer, Secret Manager, catalog, and generation failures all
            # degrade to today's synchronous authorization with no lease.
            logger.exception("spend_lease_mint_failed")
    binding_no_lease_reason = spend_lease_binding_ineligibility_reason(
        no_lease_reason,
        deferred_settlement_applies=(
            settings.spend_lease_binding_enabled
            and _deferred_settlement_applies(settings, api_key)
        ),
    )
    if binding_no_lease_reason != no_lease_reason:
        # Binding-only: Stage A eligibility also drives flag-off shadow issuance,
        # which must remain unchanged when deferred settlement is available.
        spend_context["no_lease_reason"] = binding_no_lease_reason
    if (
        settings.spend_lease_binding_enabled
        and settings.spend_lease_issuance_enabled
        and bool(spend_context["boot_verified"])
        and boot_auth is not None
        and binding_no_lease_reason is None
    ):
        prepare_binding = getattr(_typed_store, "prepare_gateway_spend_lease_binding", None)
        if callable(prepare_binding):
            echo = cast(SpendLeaseEchoValue | None, spend_context.get("echo"))
            catalog = freeze_spend_lease_catalog(
                endpoint_candidates,
                region=region,
                route_type=cast(str, body.route_type),
                service_tier=service_tier,
            )
            spend_lease_binding_plan, binding_reason = prepare_binding(
                workspace_id=workspace.id,
                key_hash=api_key.hash,
                authorization_id=authorization_id,
                idempotency_key=presented_idempotency_key,
                idempotency_fingerprint=request_fingerprint,
                estimate=estimate,
                boot_kid=boot_auth.kid,
                region=region,
                signer=_spend_lease_signer(settings),
                catalog=catalog,
                ttl_seconds=settings.spend_lease_ttl_seconds,
                skew_seconds=settings.spend_lease_skew_seconds,
                max_microdollars=settings.spend_lease_max_microdollars,
                max_available_basis_points=(
                    settings.spend_lease_max_available_basis_points
                ),
                echo_lease_id=echo.lease_id if echo is not None else None,
                echo_state=echo.state if echo is not None else None,
            )
            if binding_reason is not None:
                spend_context["no_lease_reason"] = binding_reason
                spend_context["binding_outcome"] = "ordinary"
        else:
            spend_context["no_lease_reason"] = "ledger_unavailable"
            spend_context["binding_outcome"] = "ledger_unavailable"
    user_model_slot_acquired = False
    if user_model is not None:
        user_model_slot_acquired = acquire_user_model_slot(
            user_model.id,
            authorization_id,
            limit=user_model.max_concurrency,
            kind=user_model.kind,
        )
        if not user_model_slot_acquired:
            raise api_error(
                429,
                f"User-provided model {user_model.id} is at capacity "
                f"({user_model.max_concurrency} concurrent)",
                ErrorType.RATE_LIMITED,
            )

    def release_user_model_slot_after_error() -> None:
        if user_model is not None and user_model_slot_acquired:
            release_user_model_slot(user_model.id, authorization_id)

    credit_reservation_id: str | None = None
    idempotent_replay = False
    # C1 removed the workspace cohort/denylist brake: GCP now always uses typed
    # billing when the store exposes that capability. Emergency rollback is the
    # previous deploy revision; the memory store below remains the test twin.
    if _typed_store is not None:
        import datetime as _dt

        from trusted_router.spend_windows import (
            enforced_window_limits,
            remember_spend_window_decision,
            spend_window_headers,
            spend_window_limit_error_message,
        )
        from trusted_router.storage_gcp_authorize import AuthorizeOutcome
        from trusted_router.storage_gcp_counters import key_usage_shard_count

        # Provider-native batches have a 24-hour completion window. Keep their
        # holds alive for two extra hours so a completion at the deadline can
        # still settle idempotently; ordinary request reservations remain 2h.
        reservation_ttl_seconds = _authorization_ttl_seconds(body.route_type)
        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=reservation_ttl_seconds)
        # Per-window key caps (approximate). Omitted entirely for a BYOK
        # request on a key that excludes BYOK from its caps — same rule the
        # lifetime cap applies (authorize_atomic's window_limits contract).
        is_byok_request = not has_credit_candidate
        window_limits = (
            {}
            if is_byok_request and not api_key.include_byok_in_limit
            else enforced_window_limits(api_key)  # {} in alert mode → never blocks
        )
        key_usage_shards = key_usage_shard_count(api_key)
        try:
            outcome = "unavailable"
            authorization = None
            if regional_eligible:
                assert callable(regional_authorize)
                outcome, authorization = regional_authorize(
                    authorization_id=authorization_id,
                    workspace_id=workspace.id,
                    key_hash=api_key.hash,
                    key_usage_shards=key_usage_shards,
                    estimate=estimate,
                    model_id=model.id,
                    provider=endpoint.provider,
                    requested_model_id=requested_model_id,
                    candidate_model_ids=[m.id for m, _e in endpoint_candidates],
                    region=region,
                    endpoint_id=endpoint.id,
                    candidate_endpoint_ids=[e.id for _m, e in endpoint_candidates],
                    idempotency_key=request_idempotency_key,
                    idempotency_fingerprint=request_fingerprint,
                    app_id=api_key.app_id,
                    tags=effective_tags,
                    expires_at=expires_at,
                    lease_ttl_seconds=settings.regional_quota_lease_ttl_seconds,
                    lease_max_microdollars=settings.regional_quota_lease_max_microdollars,
                    lease_max_available_basis_points=(
                        settings.regional_quota_lease_max_available_basis_points
                    ),
                    lease_shard_count=settings.regional_quota_lease_shard_count,
                )
            if outcome == "unavailable":
                outcome, authorization = _typed_store.authorize_gateway_typed(
                    workspace_id=workspace.id,
                    key_hash=api_key.hash,
                    authorization_id=authorization_id,
                    estimate=estimate,
                    has_credit_candidate=has_credit_candidate,
                    reservation_usage_type=reservation_usage_type,
                    model_id=model.id,
                    provider=endpoint.provider,
                    requested_model_id=requested_model_id,
                    candidate_model_ids=[m.id for m, _e in endpoint_candidates],
                    region=region,
                    endpoint_id=endpoint.id,
                    candidate_endpoint_ids=[e.id for _m, e in endpoint_candidates],
                    idempotency_key=request_idempotency_key,
                    tags=effective_tags,
                    idempotency_fingerprint=request_fingerprint,
                    app_id=api_key.app_id,
                    app_markup_basis_points=app_markup_basis_points,
                    receipt_fee_basis_points=receipt_fee_basis_points,
                    app_owner_user_id=app_owner_user_id,
                    key_usage_shards=key_usage_shards,
                    custom_model_id=custom_model.id if custom_model else None,
                    custom_model_revision=custom_model.revision if custom_model else None,
                    user_provided_model_id=user_model.id if user_model else None,
                    user_provided_model_revision=user_model.revision if user_model else None,
                    user_model_prompt_price_microdollars_per_m=(
                        user_model.prompt_price_microdollars_per_million_tokens
                        if user_model
                        else None
                    ),
                    user_model_completion_price_microdollars_per_m=(
                        user_model.completion_price_microdollars_per_million_tokens
                        if user_model
                        else None
                    ),
                    user_model_owner_user_id=user_model.owner_user_id if user_model else None,
                    additional_cost_reservation_microdollars=additional_cost_reservation,
                    native_batch_eligible=native_batch_eligible,
                    expires_at=expires_at,
                    window_limits=window_limits or None,
                    spend_lease=spend_lease,
                    spend_lease_binding_plan=spend_lease_binding_plan,
                )
        except conflict_store_error_types() as exc:
            release_user_model_slot_after_error()
            # The generic 503 request log cannot identify a tenant hot row.
            # Emit only safe billing metadata so an operator can reshard the
            # affected workspace without ever logging a key, prompt, or body.
            logger.warning(
                "billing.authorize_contention",
                extra={
                    "request_id": getattr(request.state, "request_id", None),
                    "workspace_id": workspace.id,
                    "requested_model": requested_model_id,
                    "estimated_microdollars": estimate,
                    "candidate_count": len(endpoint_candidates),
                    "key_usage_shards": key_usage_shards,
                    "error_class": type(exc).__name__,
                },
            )
            raise
        except transient_store_error_types() as exc:
            release_user_model_slot_after_error()
            # Keep enough tenant-safe context to distinguish a Spanner
            # deadline, service outage, session exhaustion, or retry failure.
            # The request body, raw key, prompt, and output stay out of logs.
            logger.warning(
                "billing.authorize_storage_unavailable",
                extra={
                    "request_id": getattr(request.state, "request_id", None),
                    "workspace_id": workspace.id,
                    "requested_model": requested_model_id,
                    "estimated_microdollars": estimate,
                    "candidate_count": len(endpoint_candidates),
                    "key_usage_shards": key_usage_shards,
                    "error_class": type(exc).__name__,
                },
            )
            raise
        except BaseException:
            release_user_model_slot_after_error()
            raise
        window_decision = getattr(outcome, "rate_limit", None)
        if settings.spend_lease_binding_enabled:
            spend_context["no_lease_reason"] = getattr(
                outcome, "no_lease_reason", None
            ) or spend_context.get("no_lease_reason")
            spend_context["binding_outcome"] = getattr(
                outcome, "spend_lease_outcome", None
            ) or spend_context.get("binding_outcome")
        remember_spend_window_decision(request, window_decision)
        if outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS:
            release_user_model_slot_after_error()
            record_free_credit_exhausted_safely(workspace.id)
            raise _insufficient_credits_error(workspace)
        if outcome.startswith(AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED):
            release_user_model_slot_after_error()
            if window_decision is None:
                raise RuntimeError("typed window rejection omitted its rate-limit verdict")
            raise api_error(
                429,
                spend_window_limit_error_message(window_decision),
                ErrorType.KEY_WINDOW_LIMIT_EXCEEDED,
                headers=spend_window_headers(window_decision, retry_after=True),
            )
        if outcome in (AuthorizeOutcome.KEY_LIMIT_EXCEEDED, AuthorizeOutcome.KEY_MISSING):
            release_user_model_slot_after_error()
            raise api_error(402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED)
        if outcome == AuthorizeOutcome.IDEMPOTENCY_MISMATCH:
            release_user_model_slot_after_error()
            raise api_error(
                409,
                "Idempotency key was already used for a different gateway request",
                ErrorType.CONFLICT,
            )
        if authorization is None:
            release_user_model_slot_after_error()
            raise api_error(500, "gateway authorize failed", ErrorType.INTERNAL_ERROR)
        if outcome == AuthorizeOutcome.REPLAY:
            # concurrent-race replay: respond from the STORED authorization.
            # Our provisional slot belongs to the id that lost the race, not
            # to the stored authorization — give it back.
            release_user_model_slot_after_error()
            return _replay_response(authorization)
        credit_reservation_id = authorization.credit_reservation_id
    else:
        from trusted_router.spend_windows import (
            KeyLimitExceeded,
            KeyWindowLimitExceeded,
            remember_spend_window_decision,
            spend_window_headers,
            spend_window_limit_error_message,
        )

        try:
            window_decision = STORE.reserve_key_limit(
                api_key.hash,
                estimate,
                usage_type=reservation_usage_type,
            )
            remember_spend_window_decision(request, window_decision)
        except KeyWindowLimitExceeded as exc:
            release_user_model_slot_after_error()
            remember_spend_window_decision(request, exc.decision)
            # InMemory twin of the typed window rejection (same 429 shape).
            raise api_error(
                429,
                spend_window_limit_error_message(exc.decision),
                ErrorType.KEY_WINDOW_LIMIT_EXCEEDED,
                headers=spend_window_headers(exc.decision, retry_after=True),
            ) from exc
        except KeyLimitExceeded as exc:
            release_user_model_slot_after_error()
            remember_spend_window_decision(request, exc.decision)
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc
        except ValueError as exc:
            release_user_model_slot_after_error()
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc

        settlement = "local"
        authorization_expires_at = (
            (
                dt.datetime.now(dt.UTC)
                + dt.timedelta(seconds=_authorization_ttl_seconds(body.route_type))
            )
            .isoformat()
            .replace("+00:00", "Z")
            if _is_native_batch_route(body.route_type)
            else None
        )
        if has_credit_candidate:
            try:
                credit_reservation = STORE.reserve(
                    workspace.id,
                    api_key.hash,
                    estimate,
                    idempotency_key=request_idempotency_key,
                )
                credit_reservation_id = credit_reservation.id
            except ValueError as exc:
                # The local balance refused. On a peer plane with deferred
                # settlement on, a FEDERATED key falls back to spending on
                # credit at the home plane's ledger rather than 402ing.
                #
                # Local reserve is tried FIRST and this is only the fallback:
                # a workspace that has been explicitly pre-funded by credit
                # transfer must keep spending those transferred credits with
                # local settlement. Reversing the order would strand real
                # money that was deliberately moved here.
                if not _deferred_settlement_applies(settings, api_key):
                    release_user_model_slot_after_error()
                    STORE.refund_key_limit(
                        api_key.hash, estimate, usage_type=reservation_usage_type
                    )
                    raise _insufficient_credits_error(workspace) from exc
                settlement = _DEFERRED_HOME_SETTLEMENT
                authorization_expires_at = _deferred_expires_at(settings)

        create_authorization = functools.partial(
            STORE.create_gateway_authorization,
            workspace_id=workspace.id,
            key_hash=api_key.hash,
            model_id=model.id,
            provider=endpoint.provider,
            usage_type=reservation_usage_type,
            estimated_microdollars=estimate,
            credit_reservation_id=credit_reservation_id,
            authorization_id=authorization_id,
            requested_model_id=requested_model_id,
            candidate_model_ids=[
                candidate_model.id for candidate_model, _endpoint in endpoint_candidates
            ],
            region=region,
            endpoint_id=endpoint.id,
            candidate_endpoint_ids=[
                candidate_endpoint.id
                for _candidate_model, candidate_endpoint in endpoint_candidates
            ],
            idempotency_key=request_idempotency_key,
            tags=effective_tags,
            idempotency_fingerprint=request_fingerprint,
            app_id=api_key.app_id,
            app_markup_basis_points=app_markup_basis_points,
            receipt_fee_basis_points=receipt_fee_basis_points,
            app_owner_user_id=app_owner_user_id,
            custom_model_id=custom_model.id if custom_model else None,
            custom_model_revision=custom_model.revision if custom_model else None,
            user_provided_model_id=user_model.id if user_model else None,
            user_provided_model_revision=user_model.revision if user_model else None,
            user_model_prompt_price_microdollars_per_m=(
                user_model.prompt_price_microdollars_per_million_tokens if user_model else None
            ),
            user_model_completion_price_microdollars_per_m=(
                user_model.completion_price_microdollars_per_million_tokens if user_model else None
            ),
            user_model_owner_user_id=user_model.owner_user_id if user_model else None,
            additional_cost_reservation_microdollars=additional_cost_reservation,
            native_batch_eligible=native_batch_eligible,
            settlement=settlement,
            expires_at=authorization_expires_at,
            # The outstanding increment rides the SAME transaction that
            # inserts the authorization, so an idempotent replay (which
            # returns the pre-existing row and writes nothing) cannot
            # double-count, and a cap refusal cannot leave an authorization
            # behind. Doing it as a separate call could do both.
            deferred_cap_microdollars=(
                settings.federation_deferred_max_outstanding_microdollars
                if settlement == _DEFERRED_HOME_SETTLEMENT
                else None
            ),
            spend_lease=spend_lease,
        )
        try:
            authorization = create_authorization()
        except DeferredSettlementCapReached as cap_exc:
            release_user_model_slot_after_error()
            # The key-limit escrow taken above must come back: nothing on this
            # plane would ever release it for a request that never became an
            # authorization (the reaper only sees authorizations).
            STORE.refund_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
            raise api_error(
                402,
                "This plane is holding the maximum unsettled spend for this workspace "
                "while the home plane is unreachable; it will clear as settlements are "
                "delivered.",
                ErrorType.DEFERRED_SETTLEMENT_CAP,
                headers={"Retry-After": "30"},
            ) from cap_exc
        except StoreConflict as conflict_exc:
            release_user_model_slot_after_error()
            # DSQL OCC exhausted its retries (a hot outstanding row under
            # burst load will do this). Same rule as the cap arm: the escrow
            # committed OUTSIDE this transaction, so nothing downstream will
            # ever release it for a request that produced no authorization —
            # swallowing this into a bare 503 leaks it silently, forever.
            STORE.refund_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
            raise api_error(
                503,
                "Authorization contention; retry shortly.",
                ErrorType.SERVICE_UNAVAILABLE,
                headers={"Retry-After": "1"},
            ) from conflict_exc
        except BaseException:
            release_user_model_slot_after_error()
            raise
    byok_config = (
        _get_byok_provider(workspace.id, endpoint.provider) if model_usage_type.is_byok() else None
    )
    return _gateway_authorize_response(
        authorization=authorization,
        workspace_id=workspace.id,
        key_hash=api_key.hash,
        model=model,
        endpoint=endpoint,
        requested_model_id=requested_model_id,
        model_usage_type=model_usage_type,
        limit_usage_type=reservation_usage_type,
        estimate=estimate,
        credit_reservation_id=credit_reservation_id,
        byok_config=byok_config,
        region=region,
        settings=settings,
        broadcast_destinations=broadcast_destinations,
        endpoint_candidates=endpoint_candidates,
        idempotent_replay=idempotent_replay,
        custom_model=custom_model,
    )


def _gateway_validate_sync(
    request: Request,
    body: GatewayValidateRequest,
    settings: Settings,
) -> dict[str, Any]:
    require_internal_gateway(request, settings)
    api_key = _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    _assert_gateway_key_scope(api_key)
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
    assert_workspace_billing_active(workspace)
    return {
        "data": {
            "workspace_id": workspace.id,
            "api_key_hash": api_key.hash,
            "route_type": body.route_type,
        }
    }


def _gateway_key_info_sync(
    request: Request,
    body: GatewayValidateRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Key self-introspection for the enclave: the /v1/key passthrough.

    The enclave NEVER forwards the raw bearer to the control plane (the
    attested contract; authorize sends a lookup hash) — so agent budget
    reads come through here keyed by the same lookup hash + internal
    token. Deliberately no billing-pause gate: reading your own limits
    while paused is a harmless, useful read."""
    require_internal_gateway(request, settings)
    api_key = _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    _assert_gateway_key_scope(api_key)
    from trusted_router.routes.keys import self_key_shape

    return {"data": self_key_shape(api_key)}


def _gateway_resolve_custom_model_sync(
    request: Request,
    body: GatewayResolveCustomModelRequest,
    settings: Settings,
) -> dict[str, Any]:
    require_internal_gateway(request, settings)
    api_key = _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    _assert_gateway_key_scope(api_key)
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
    assert_workspace_billing_active(workspace)
    if not is_custom_model_id(body.model):
        raise api_error(400, "Model is not a custom model", ErrorType.BAD_REQUEST)
    normalized = normalize_custom_model_id(body.model)
    custom_model = STORE.get_custom_model(normalized)
    if custom_model is None:
        user_model = STORE.get_user_model(normalized)
        if (
            user_model is None
            or not user_model.enabled
            or user_model.status != "active"
            or not settings.user_models_dispatch_enabled
        ):
            raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
        budget = dispatch_budget(user_model.kind)
        return {
            "data": {
                "workspace_id": workspace.id,
                "api_key_hash": api_key.hash,
                "route_type": body.route_type,
                "custom_model": {
                    "id": user_model.id,
                    "name": user_model.name,
                    "kind": "user_provided",
                    "user_model_kind": user_model.kind,
                    # The envelopes below are bound (AAD) to the OWNER's
                    # workspace, a per-secret purpose, and the user_model
                    # namespace — not to the caller's workspace above; the
                    # enclave must decrypt with exactly these.
                    "secret_namespace": USER_MODEL_SECRET_NAMESPACE,
                    "owner_workspace_id": user_model.owner_workspace_id,
                    "owner_user_id": user_model.owner_user_id,
                    "endpoint_url": user_model.endpoint_url,
                    "upstream_model_id": user_model.upstream_model_id,
                    "revision": user_model.revision,
                    "supports_streaming": user_model.supports_streaming,
                    "endpoint_encrypted_secret": encrypted_secret_payload(
                        user_model.encrypted_endpoint_api_key
                    ),
                    "endpoint_secret_purpose": USER_MODEL_ENDPOINT_KEY_PURPOSE,
                    "signing_encrypted_secret": encrypted_secret_payload(
                        user_model.encrypted_signing_secret
                    ),
                    "signing_secret_purpose": USER_MODEL_SIGNING_PURPOSE,
                    "connect_timeout_seconds": budget.connect,
                    "first_byte_timeout_seconds": budget.first_byte,
                    "idle_timeout_seconds": budget.idle,
                    "total_timeout_seconds": budget.total,
                },
            }
        }
    if not custom_model.enabled:
        raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
    return {
        "data": {
            "workspace_id": workspace.id,
            "api_key_hash": api_key.hash,
            "route_type": body.route_type,
            "custom_model": {
                "id": custom_model.id,
                "name": custom_model.name,
                "kind": "prompt_wrapper",
                "base_model_id": custom_model.base_model_id,
                "hidden_prompt": custom_model.hidden_prompt,
                "revision": custom_model.revision,
            },
        }
    }


def register(router: APIRouter) -> None:
    # Every handler below does synchronous storage IO. They run it via
    # run_in_threadpool so a slow/contended transaction on one request never
    # blocks the shared event loop for all others.
    #
    # These share AnyIO's default worker pool (40 tokens) with FastAPI's other
    # sync dependencies — deliberately, NOT a dedicated CapacityLimiter. Cloud
    # Run runs this service at --concurrency=2 (rollout.sh), so at most ~2
    # offloads are ever in flight per instance (far under 40); load scales out
    # across instances, not up per-instance, and prod inference never touches
    # this service (it goes through the enclave). Give gateway storage its own
    # limiter only if TR_CLOUD_RUN_CONCURRENCY is raised toward the pool size.
    @router.post("/internal/gateway/validate")
    async def gateway_validate(
        request: Request,
        body: GatewayValidateRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_gateway_validate_sync, request, body, settings)

    @router.post("/internal/gateway/key")
    async def gateway_key_info(
        request: Request,
        body: GatewayValidateRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_gateway_key_info_sync, request, body, settings)

    @router.post("/internal/gateway/resolve-custom-model")
    async def gateway_resolve_custom_model(
        request: Request,
        body: GatewayResolveCustomModelRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_gateway_resolve_custom_model_sync, request, body, settings)

    @router.post("/internal/gateway/authorize")
    async def gateway_authorize(
        request: Request,
        body: GatewayAuthorizeRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await authorize_gateway(request, body, settings)

    @router.post("/internal/gateway/spend-lease/register-boot")
    async def gateway_spend_lease_boot_register(
        request: Request,
        body: SpendLeaseBootRegistrationRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            _register_spend_lease_boot_sync,
            request,
            body,
            settings,
        )

    @router.post("/internal/gateway/settle")
    async def gateway_settle(
        request: Request,
        body: GatewaySettleRequest,
        settings: SettingsDep,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        return await settle_gateway(request, body, settings, background_tasks)

    @router.post("/internal/gateway/refund")
    async def gateway_refund(
        request: Request,
        body: GatewaySettleRequest,
        settings: SettingsDep,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        return await run_in_threadpool(
            _settle_gateway_authorization,
            body,
            success=False,
            settings=settings,
            background_tasks=background_tasks,
        )

    @router.post("/internal/gateway/settle-outbox/drain")
    async def gateway_settle_outbox_drain(
        request: Request,
        settings: SettingsDep,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        result = await run_in_threadpool(drain_settle_outbox, limit)
        # Cloud Scheduler drives this on a cadence; the heartbeat makes that
        # cadence visible on /fleet so a silently-dead scheduler is seen.
        await run_in_threadpool(record_heartbeat, "job:settle-outbox-drain", settings=settings)
        return result

    @router.post("/internal/gateway/receipt-keys/collect")
    async def gateway_receipt_keys_collect(
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, int]:
        """Cloud Scheduler pass for the append-only receipt-key log."""

        require_internal_gateway(request, settings)
        result = await run_in_threadpool(collect_receipt_keys, settings)
        await run_in_threadpool(
            record_heartbeat,
            "job:receipt-key-collector",
            settings=settings,
        )
        return result

    @router.post("/internal/gateway/regional-quota/reconcile")
    async def gateway_regional_quota_reconcile(
        request: Request,
        settings: SettingsDep,
        limit: int = 100,
    ) -> dict[str, int]:
        require_internal_gateway(request, settings)
        method = getattr(STORE, "reconcile_regional_quota_leases", None)
        if not settings.regional_quota_leases_enabled or not callable(method):
            return {"inspected": 0, "reconciled": 0, "closed": 0, "errors": 0}
        result = await run_in_threadpool(method, limit=max(1, min(limit, 1000)))
        if int(result.get("errors", 0)):
            logger.error(
                "regional_quota.reconcile_incomplete",
                extra={
                    "inspected": int(result.get("inspected", 0)),
                    "reconciled": int(result.get("reconciled", 0)),
                    "closed": int(result.get("closed", 0)),
                    "errors": int(result.get("errors", 0)),
                },
            )
            raise api_error(
                503,
                "Regional quota reconciliation is incomplete",
                ErrorType.SERVICE_UNAVAILABLE,
            )
        await run_in_threadpool(
            record_heartbeat,
            "job:regional-quota-reconcile",
            settings=settings,
        )
        return cast(dict[str, int], result)

    @router.post("/internal/gateway/home-settlement/drain")
    async def gateway_home_settlement_drain(
        request: Request,
        settings: SettingsDep,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run one forwarding pass of recorded debt to the home plane.

        The in-process scheduler drives this on its own; the endpoint is the
        operator's lever for "drain it NOW" (after a home outage clears) and
        the observability surface (the returned counts are the backlog
        story: forwarded / dead_lettered / clamped / outage).
        """
        require_internal_gateway(request, settings)
        from trusted_router.services.home_settlement import drain_home_settlements

        return {
            "data": await run_in_threadpool(lambda: drain_home_settlements(settings, limit=limit))
        }

    @router.post("/internal/gateway/deferred/reap")
    async def gateway_deferred_reap(
        request: Request,
        settings: SettingsDep,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Reclaim deferred authorizations whose settle never arrived.

        A reaper that exists but is never run is the same as no reaper: each
        abandoned authorization (the enclave dying between authorize and
        settle — every deploy) leaks its estimate into the outstanding
        counter until the workspace hits the cap and 402s forever. This
        endpoint is the operator's lever today and the scheduler's target
        when the forwarder increment wires periodic passes.
        """
        require_internal_gateway(request, settings)
        reap = getattr(STORE, "reap_expired_deferred_authorizations", None)
        if reap is None:
            raise api_error(
                501,
                "This plane's store has no deferred-settlement reaper",
                ErrorType.ENDPOINT_NOT_SUPPORTED,
            )
        return {"data": await run_in_threadpool(lambda: reap(limit=limit))}


def _api_key_for_gateway_authorization(body: GatewayAuthorizeRequest) -> Any | None:
    return _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )


def _gateway_idempotency_key(request: Request, body: GatewayAuthorizeRequest) -> str | None:
    raw = body.idempotency_key or request.headers.get("idempotency-key")
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > 256:
        raise api_error(400, "idempotency-key is too long", ErrorType.BAD_REQUEST)
    return key


def _gateway_authorize_fingerprint(
    *,
    workspace_id: str,
    key_hash: str,
    body: dict[str, Any],
    idempotency_key: str | None = None,
) -> str:
    # Standard idempotency semantics: the key can replay the same logical
    # request, but a caller cannot reuse it for a different request body.
    # Keep dynamic catalog/routing output out of this fingerprint so a replay
    # across a deploy can still recover the original authorization record.
    material = {
        key: value
        for key, value in body.items()
        if key
        not in {
            "api_key_hash",
            "api_key_lookup_hash",
            "idempotency_key",
        }
    }
    if _is_native_batch_idempotency_key(idempotency_key):
        # One encrypted Batch object is claimed across regions and rolling
        # enclave revisions. These fields are gateway estimates or execution
        # locality, not customer request identity. Keeping them in the hash can
        # turn a lost authorize response into an unrecoverable cross-region 409
        # and leave its hold live until TTL.
        for dynamic_key in {"region", "estimated_input_tokens", "max_output_tokens"}:
            material.pop(dynamic_key, None)
    material["workspace_id"] = workspace_id
    material["key_hash"] = key_hash
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_gateway_authorization_id() -> str:
    """One fresh authorization id (same shape the typed store mints)."""
    return f"gwa-{uuid.uuid4().hex}"


def _authorization_endpoint_candidates(
    authorization: Any,
    fallback: list[tuple[Model, ModelEndpoint]],
) -> list[tuple[Model, ModelEndpoint]]:
    user_model_pair = _authorized_user_model_pair(authorization)
    if user_model_pair is not None:
        return [user_model_pair]
    candidates: list[tuple[Model, ModelEndpoint]] = []
    endpoint_ids = authorization.candidate_endpoint_ids or []
    if not endpoint_ids and authorization.endpoint_id:
        endpoint_ids = [authorization.endpoint_id]
    for endpoint_id in endpoint_ids:
        endpoint = _endpoint_for_id_compat(endpoint_id)
        if endpoint is None:
            continue
        model = MODELS.get(endpoint.model_id)
        if model is None:
            continue
        candidates.append((model, endpoint))
    return candidates or fallback


def _broadcast_destinations_for_authorize(workspace_id: str) -> list[Any]:
    """Skip the destination-index RPC briefly after an observed empty result."""
    now = time.monotonic()
    with _BROADCAST_EMPTY_CACHE_LOCK:
        expires_at = _BROADCAST_EMPTY_CACHE.get(workspace_id)
        if expires_at is not None:
            if now < expires_at:
                _BROADCAST_EMPTY_CACHE.move_to_end(workspace_id)
                return []
            del _BROADCAST_EMPTY_CACHE[workspace_id]

    destinations = STORE.list_broadcast_destinations(workspace_id)
    with _BROADCAST_EMPTY_CACHE_LOCK:
        if destinations:
            # Positive results are deliberately uncached: destination changes
            # must take effect on the next authorize. A newly-created first
            # destination can be hidden only by a previously cached empty result,
            # delaying response metadata and native-batch eligibility by at most
            # this cache's 60-second TTL.
            _BROADCAST_EMPTY_CACHE.pop(workspace_id, None)
        else:
            _BROADCAST_EMPTY_CACHE[workspace_id] = now + _BROADCAST_EMPTY_CACHE_TTL_SECONDS
            _BROADCAST_EMPTY_CACHE.move_to_end(workspace_id)
            while len(_BROADCAST_EMPTY_CACHE) > _BROADCAST_EMPTY_CACHE_MAX_ENTRIES:
                _BROADCAST_EMPTY_CACHE.popitem(last=False)
    return destinations


def _gateway_authorize_response(
    *,
    authorization: Any,
    workspace_id: str,
    key_hash: str,
    model: Model,
    endpoint: ModelEndpoint,
    requested_model_id: str,
    model_usage_type: UsageType,
    limit_usage_type: UsageType,
    estimate: int,
    credit_reservation_id: str | None,
    byok_config: Any | None,
    region: str,
    settings: Settings,
    broadcast_destinations: list[dict[str, Any]],
    endpoint_candidates: list[tuple[Model, ModelEndpoint]],
    idempotent_replay: bool,
    custom_model: Any | None,
) -> dict[str, Any]:
    return {
        "data": {
            "authorization_id": authorization.id,
            "workspace_id": workspace_id,
            "api_key_hash": key_hash,
            "model": model.id,
            "upstream_model": endpoint.upstream_id or model.id,
            "endpoint_id": endpoint.id,
            "provider": endpoint.provider,
            "provider_name": PROVIDERS[endpoint.provider].name,
            **_gateway_provider_route_payload(endpoint),
            "requested_model": requested_model_id,
            "usage_type": model_usage_type.value,
            "limit_usage_type": limit_usage_type.value,
            **money_pair("estimated_cost", estimate),
            "credit_reservation_id": credit_reservation_id,
            **_gateway_byok_payload(byok_config, workspace_id),
            "content_storage_enabled": False,
            "region": region,
            "regions": region_payload(settings),
            "broadcast_destinations": broadcast_destinations,
            "idempotent_replay": idempotent_replay,
            "additional_cost_reservation_microdollars": (
                authorization.additional_cost_reservation_microdollars
            ),
            "receipt_fee_basis_points": authorization.receipt_fee_basis_points,
            "request_metadata_version": REQUEST_METADATA_VERSION,
            "native_batch_eligible": authorization.native_batch_eligible,
            **_gateway_spend_lease_payload(authorization),
            "tags": dict(authorization.tags),
            "custom_model": None
            if custom_model is None
            else {
                "id": custom_model.id,
                "name": custom_model.name,
                "base_model_id": custom_model.base_model_id,
                "hidden_prompt": custom_model.hidden_prompt,
                "revision": custom_model.revision,
            },
            "route_candidates": [
                _gateway_candidate_payload(
                    candidate_model, candidate_endpoint, workspace_id, region
                )
                for candidate_model, candidate_endpoint in endpoint_candidates
            ],
        }
    }


def _gateway_spend_lease_payload(authorization: GatewayAuthorization) -> dict[str, Any]:
    token = authorization.spend_lease_token
    if not token:
        return {}
    stored_status = authorization.spend_lease_status or "active"
    if stored_status == "terminal":
        status = "terminal"
    else:
        expires_at = int(authorization.spend_lease_exp or 0)
        status = "expired" if int(time.time()) >= expires_at else "active"
    return {"spend_lease": {"token": token, "lease_status": status}}


#: GatewayAuthorization.settlement value for spend booked as debt to the home
#: plane's ledger. Mirrors storage_postgres._DEFERRED_HOME_SETTLEMENT; the
#: value is written by this module and read by the store, so it is declared in
#: both rather than importing a backend-specific module into the route.
_DEFERRED_HOME_SETTLEMENT = "deferred_home"


def _deferred_settlement_applies(settings: Settings, api_key: Any) -> bool:
    """Whether this request may spend on credit at the home plane's ledger.

    Two conditions, both required. The plane must have deferred settlement
    switched on, and the key must be FEDERATED — a locally issued key whose
    balance lives here is simply out of money, and letting it run up debt at
    a home plane that has no account for it would be nonsense.
    """
    if not getattr(settings, "federation_deferred_settlement_enabled", False):
        return False
    return bool(getattr(api_key, "federated_home", ""))


def _deferred_expires_at(settings: Settings) -> str:
    """When the reaper may reclaim this authorization's admitted estimate.

    Without an expiry there is no reclaim, and an enclave that dies between
    authorize and settle — which every deploy causes — leaks its estimate into
    the outstanding counter permanently.
    """
    from trusted_router.spend_windows import utcnow

    ttl = int(getattr(settings, "federation_deferred_authorization_ttl_seconds", 7200))
    expires = utcnow() + dt.timedelta(seconds=max(60, ttl))
    return expires.isoformat().replace("+00:00", "Z")


def _insufficient_credits_error(workspace: Any) -> Exception:
    """402, but say WHICH plane the money is on when that is the real answer.

    A federated workspace is a SHADOW of one on the home plane, and credits
    never federate — they seed at zero here and only an explicit transfer moves
    them (trusted_router.credit_transfer). So a bare "Insufficient credits"
    would be actively misleading: it tells a customer who has a healthy balance
    on the home plane to go top up, and it tells an operator nothing about the
    actual fix, which is to run a transfer.

    Deliberately NOT auto-transferring on demand. Three reasons, any one of
    which is disqualifying:

      * A leaked key would become a drain on the whole workspace balance, not
        just this plane's. Auto-transfer turns "spend up to the key's limit
        here" into "pull the home balance across a jurisdiction boundary and
        then spend it", and it re-arms itself on every subsequent request.
      * It reintroduces the coupling federation exists to remove: the first
        request on a cold key would block on a synchronous home-plane call
        that MOVES MONEY. If the home plane goes away mid-transfer, an
        inference request is holding an escrow it has no durable place to
        resolve.
      * Moving funds across planes is an audited action, not a cache fill.

    The message stays true whether the customer never transferred or
    transferred and then spent it, because both have the same fix.
    """
    if getattr(workspace, "federated_home", ""):
        return api_error(
            402,
            "No spendable credits on this plane. This workspace is federated: "
            "credits do not federate with identity and must be transferred to "
            "this plane explicitly. A balance on the home plane is not "
            "spendable here.",
            ErrorType.CREDITS_NOT_ON_THIS_PLANE,
        )
    return api_error(402, "Insufficient credits", ErrorType.INSUFFICIENT_CREDITS)


def _api_key_for_gateway_lookup(
    *,
    api_key_hash: str | None,
    api_key_lookup_hash: str | None,
) -> Any | None:
    """Resolve the caller's key, federating from the home plane on a miss.

    Hooked HERE and not inside the store on purpose. This function is the
    exclusive resolver for the four enclave endpoints (validate, key,
    resolve-custom-model, authorize). Hooking STORE.get_key_by_lookup_hash
    instead would also silently federate the direct-to-control-plane
    raw-bearer path, which verifies secret_hash — material a federated
    record deliberately does not carry. Same lookup, different trust
    decision; they must not share a code path.

    A LOCALLY ISSUED key is authoritative and returned as-is. A FEDERATED
    one is a cached copy of somebody else's record and is age-checked
    first — see `_federated_key_still_valid`.
    """
    api_key = None
    if api_key_hash:
        api_key = STORE.get_key_by_hash(api_key_hash)
    if api_key is None and api_key_lookup_hash:
        api_key = STORE.get_key_by_lookup_hash(api_key_lookup_hash)
    if api_key is not None and not getattr(api_key, "federated_home", ""):
        return api_key
    if not api_key_lookup_hash:
        # Nothing to re-resolve against. A federated record reached only by
        # key hash cannot be refreshed, so it is served as-is; the lookup-hash
        # path (which every enclave request uses) does the age check.
        return api_key
    return _federated_key_still_valid(api_key, api_key_lookup_hash)


def _assert_gateway_key_scope(api_key: Any) -> None:
    """Require `inference` for every kind of model-execution spend.

    This includes text, media, batch, and hosted-tool route types; the scope
    bounds the kind of authority rather than the model modality. See
    docs/design/oauth-scopes-and-app-economy.md, "Scopes: small, real,
    enforced".
    """
    scopes = frozenset(api_key.scopes)
    if scopes and SCOPE_INFERENCE not in scopes:
        raise api_error(
            403,
            f"API key is missing required scope: {SCOPE_INFERENCE}",
            ErrorType.INSUFFICIENT_SCOPE,
        )


def _oauth_app_terms_for_key(api_key: Any) -> tuple[int, str]:
    if not api_key.app_id:
        return 0, ""

    suspended, markup_basis_points, owner_user_id = _oauth_app_provenance(api_key)
    if suspended:
        # Do not report INVALID_API_KEY: enclaves negative-cache that result,
        # so un-suspending the app would not promptly restore its valid key.
        raise api_error(
            403,
            "OAuth app is missing or suspended; new authorizations are forbidden",
            ErrorType.FORBIDDEN,
        )
    return markup_basis_points, owner_user_id


def _oauth_app_provenance(api_key: Any) -> tuple[bool, int, str]:
    if getattr(api_key, "federated_home", ""):
        # Case 1: all federated app terms come from home. A peer-local app with
        # the same slug is unrelated for both restrictions and billing.
        suspended = bool(getattr(api_key, "federated_app_suspended", False))
        markup_basis_points = int(getattr(api_key, "federated_app_markup_basis_points", 0))
        owner_user_id = str(getattr(api_key, "federated_app_owner_user_id", ""))
    else:
        app = STORE.get_oauth_app(api_key.app_id)
        if app is None:
            # Case 3: an orphaned home-plane app key fails closed.
            suspended = True
            markup_basis_points = 0
            owner_user_id = ""
        else:
            # Case 2: a home-plane key uses its authoritative local app row.
            suspended = bool(app.suspended)
            markup_basis_points = int(app.markup_basis_points)
            owner_user_id = str(app.owner_user_id)

    return suspended, markup_basis_points, owner_user_id


def _oauth_app_is_suspended_for_key(api_key: Any) -> bool:
    """Resolve app suspension from the key's authoritative provenance."""
    return _oauth_app_provenance(api_key)[0]


def _federated_key_still_valid(cached: Any | None, lookup_hash: str) -> Any | None:
    """Serve a federated key only while it is young enough to trust.

    This is the ENTIRE revocation mechanism for a peer plane. Nothing pushes a
    revocation across: `upsert_federated_api_key` runs on a cache MISS, so
    without an age check a key the customer deleted at home keeps authorizing
    here forever, spending whatever credits were transferred to this plane, and
    the only remedy is a manual per-region row delete. The same applies to the
    `workspace_billing_paused` bit the shadow workspace carries — a pause that
    never arrives cannot quiesce anything.

    Three bands, and the middle one is the whole reason this is not just "call
    home every time":

      * Younger than the SOFT TTL: served from the local database. No
        cross-plane call, which is the availability property being bought.
      * Between the soft and hard TTLs: re-resolved, but a home plane that
        cannot answer does NOT fail the request — the cached record still
        serves. An outage at home must not become an outage here.
      * Past the HARD TTL: refused unless home answers. At some age
        "probably still valid" stops being good enough for a credential.

    A home plane that answers "no such key" REVOKES immediately in every band:
    that is a verdict, not an outage. The negative cache in
    services/federation.py bounds how often a revoked key can ask.
    """
    settings = get_settings()
    home = getattr(settings, "federation_home_base_url", "") or ""
    token = getattr(settings, "federation_home_token", "") or ""
    if not home or not token:
        # Federation is off. There is nothing to refresh against, so expiring
        # the record would take the plane down over a config change without
        # making any revoked key one bit less usable.
        return cached

    age = _federated_record_age_seconds(cached)
    if cached is not None and age < federation.SOFT_TTL_SECONDS:
        return cached

    client = _federation_client(home, token)
    try:
        record = client.resolve(lookup_hash)
    except FederationUnavailable as exc:
        if cached is not None and age < federation.HARD_TTL_SECONDS:
            logger.warning(
                "serving a stale federated key (age %ds): home plane unavailable",
                int(age),
            )
            return cached
        raise api_error(
            503,
            "Key directory is temporarily unavailable; retry shortly",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "5"},
        ) from exc
    if record is None:
        # Home genuinely does not know it: never issued, or REVOKED. Either
        # way this plane must stop serving it, cached copy or not.
        return None
    return STORE.upsert_federated_api_key(record)


def _federated_record_age_seconds(api_key: Any | None) -> float:
    """Seconds since this shadow was written, or "infinitely old".

    `created_at` is stamped by `federated_api_key_from_record` on every write,
    so for a federated record it means "when this plane last heard from home",
    and a successful refresh resets it.

    An unparseable or missing stamp counts as hard-expired rather than fresh.
    A record whose age cannot be established is exactly the one that must not
    be trusted indefinitely, and the failure is loud (a refresh, or a 503)
    instead of silent.
    """
    if api_key is None:
        return float("inf")
    raw = str(getattr(api_key, "created_at", "") or "")
    if not raw:
        return float("inf")
    try:
        stamped = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=dt.UTC)
    return max(0.0, (dt.datetime.now(dt.UTC) - stamped).total_seconds())


@lru_cache(maxsize=4)
def _federation_client(home: str, token: str) -> FederationClient:
    """One client per (home, token) so the breaker and single-flight state
    are shared across requests rather than reset on every call."""
    return FederationClient(home_base_url=home, peer_token=token)


def _require_monitor_model_key(
    body: dict[str, Any],
    api_key_lookup_hash: str,
    settings: Settings,
) -> None:
    if not _requests_monitor_model(body):
        return
    expected = settings.synthetic_monitor_api_key
    if expected and api_key_lookup_hash == lookup_hash_api_key(expected):
        return
    raise api_error(
        403,
        "trustedrouter/monitor is restricted to the synthetic monitor key",
        ErrorType.FORBIDDEN,
    )


def _requests_monitor_model(body: dict[str, Any]) -> bool:
    if str(body.get("model") or "").strip() == MONITOR_MODEL_ID:
        return True
    models = body.get("models")
    if isinstance(models, list):
        return any(str(model).strip() == MONITOR_MODEL_ID for model in models)
    return False


def _force_custom_model_credit_routes(
    body: dict[str, Any],
    *,
    error_message: str = "Custom models do not support BYOK routes",
) -> None:
    _force_credit_routes(body, error_message=error_message)


def _force_partner_credit_routes(body: dict[str, Any]) -> None:
    _force_credit_routes(body, error_message="Parasail Liberty does not support BYOK routes")


def _force_credit_routes(body: dict[str, Any], *, error_message: str) -> None:
    prefs = provider_route_preferences(body)
    if prefs.usage_type == UsageType.BYOK:
        raise api_error(
            400,
            error_message,
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    provider = body.get("provider")
    if isinstance(provider, dict):
        body["provider"] = {**provider, "usage": "credits"}
    else:
        body["provider"] = {"usage": "credits"}


def _is_web_search_restricted_model(model_id: str) -> bool:
    model = model_id.strip().lower()
    return any(
        model == prefix or model.startswith(f"{prefix}-") or model.startswith(f"{prefix}/")
        for prefix in (
            "trustedrouter/zdr",
            "trustedrouter/e2e",
            "trustedrouter/confidential",
            "trustedrouter/eu",
        )
    )


def _is_web_search_restricted_provider(provider: Any) -> bool:
    if not isinstance(provider, dict):
        return False
    return str(provider.get("data_collection") or "").strip().lower() == "deny" or (
        str(provider.get("jurisdiction") or "").strip().lower() == "eu"
    )


def _report_route_fallbacks(body: GatewaySettleRequest) -> None:
    """Accumulate successful provider failovers in Sentry as warnings.

    Each event means one provider route failed pre-output and the attested
    gateway routed around it — harmless individually, invisible in aggregate
    until now. A model+provider pair that accumulates these is a dead default
    route (GMI/nemotron 2026-07-15, GMI/kimi-k3 2026-07-16): alert on volume,
    then demote the route (see _PROVIDER_UNSERVED_CREDITS_MODELS).
    Grouping: message embeds model + failed providers, so each pair gets its
    own Sentry issue whose event count IS the accumulation signal.
    """
    try:
        import sentry_sdk

        failures = [f for f in (getattr(body, "route_failures", None) or []) if isinstance(f, str)][
            :8
        ]
        providers = sorted({f.split("|", 1)[0] for f in failures if f})
        model = body.selected_model or body.model or "unknown"
        with sentry_sdk.push_scope() as scope:
            scope.set_level("warning")
            scope.set_tag("failover.model", model[:100])
            if providers:
                scope.set_tag("failover.failed_providers", ",".join(providers)[:200])
            scope.set_context(
                "route_failover",
                {
                    "fallbacks": getattr(body, "route_fallbacks", None),
                    "failures": failures,
                    "served_by": body.selected_endpoint or body.endpoint,
                },
            )
            sentry_sdk.capture_message(
                f"provider failover: {model} routed around {','.join(providers) or 'unknown'}"
            )
    except Exception:  # noqa: BLE001 - observability must never break settlement
        logger.warning("route fallback sentry report failed", exc_info=True)


@spanner_rpc_budget(_BILLING_PATH_SPANNER_BUDGET_SECONDS)
def _settle_gateway_with_admission_sync(
    body: GatewaySettleRequest,
    *,
    settings: Settings,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    authorization = STORE.get_gateway_authorization(body.authorization_id)
    if authorization is None:
        raise api_error(404, "Gateway authorization not found", ErrorType.NOT_FOUND)
    subject = authorization.key_hash
    if not _SETTLE_ADMISSION.try_acquire(
        subject,
        limit=settings.settle_per_key_inflight_limit,
    ):
        logger.warning(
            "billing.settle_admission_limited",
            extra={
                "key_subject_fingerprint": hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16],
                "limit": settings.settle_per_key_inflight_limit,
            },
        )
        raise api_error(
            503,
            "Settlement is temporarily unavailable; retry shortly.",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
        )
    try:
        return _settle_gateway_authorization(
            body,
            success=True,
            settings=settings,
            background_tasks=background_tasks,
            _authorization=authorization,
        )
    finally:
        _SETTLE_ADMISSION.release(subject)


@spanner_rpc_budget(_BILLING_PATH_SPANNER_BUDGET_SECONDS)
def _settle_gateway_authorization(
    body: GatewaySettleRequest,
    *,
    success: bool,
    settings: Settings,
    background_tasks: BackgroundTasks | None = None,
    _authorization: GatewayAuthorization | None = None,
) -> dict[str, Any]:
    timing_start = perf_counter()
    # getattr: some callers/tests build the settle body via model_construct,
    # which skips defaults for unset fields.
    if success and getattr(body, "route_fallbacks", None):
        _report_route_fallbacks(body)
    authorization = _authorization or STORE.get_gateway_authorization(body.authorization_id)
    if authorization is None:
        raise api_error(404, "Gateway authorization not found", ErrorType.NOT_FOUND)
    if authorization.settled:
        _release_user_model_slot_safely(authorization)
        # No timing line for replays: they are ~one point-read and would dominate
        # the latency dataset with noise.
        return {"data": _already_settled_gateway_data(authorization)}

    if body.tags is not None:
        try:
            if not tags_match(body.tags, authorization.tags):
                logger.warning(
                    "gateway settlement tags ignored authorization_id=%s "
                    "authorized_tag_count=%d supplied_tag_count=%d",
                    authorization.id,
                    len(authorization.tags),
                    len(body.tags),
                )
        except InvalidTags as exc:
            logger.warning(
                "invalid gateway settlement tags ignored authorization_id=%s "
                "authorized_tag_count=%d error=%s",
                authorization.id,
                len(authorization.tags),
                str(exc),
            )

    settle_body = _settle_body_with_safe_attribution(body, authorization.id)
    settle_body = _settle_body_with_safe_client_context(settle_body, authorization.id)
    if not success:
        client_context = settle_body.get("client")
        client_fields = client_context if isinstance(client_context, dict) else {}
        logger.info(
            "gateway.refund_client_context authorization_id=%s gateway_request_id=%s "
            "sdk=%s sdk_version=%s attempt=%s prev_outcome=%s prev_error_class=%s "
            "prev_host=%s failover_used=%s",
            authorization.id,
            settle_body.get("gateway_request_id"),
            client_fields.get("sdk"),
            client_fields.get("sdk_version"),
            client_fields.get("attempt"),
            client_fields.get("prev_outcome"),
            client_fields.get("prev_error_class"),
            client_fields.get("prev_host"),
            client_fields.get("failover_used"),
        )
    # This field is control-plane-owned. Never trust a caller-supplied value;
    # only the selected endpoint and frozen price history may set operator COGS.
    settle_body.pop(PARTNER_OPERATOR_COST_SETTLE_FIELD, None)
    settle_body.pop(USER_MODEL_PAYOUT_SETTLE_FIELD, None)
    settle_body.pop(USER_MODEL_OWNER_SETTLE_FIELD, None)
    settle_body.pop(USER_MODEL_ID_SETTLE_FIELD, None)
    settle_body.pop(APP_MARKUP_PAYOUT_SETTLE_FIELD, None)
    settle_body.pop(APP_MARKUP_OWNER_SETTLE_FIELD, None)
    settle_body.pop(APP_MARKUP_APP_ID_SETTLE_FIELD, None)

    user_model_pair = _authorized_user_model_pair(authorization)
    selected_endpoint = _select_authorized_endpoint(authorization, body)
    if selected_endpoint is None:
        raise api_error(
            400,
            "selected endpoint was not authorized for this gateway request",
            ErrorType.BAD_REQUEST,
        )
    model = (
        user_model_pair[0]
        if user_model_pair is not None
        else MODELS.get(selected_endpoint.model_id)
    )
    if model is None:
        raise api_error(500, "Authorized model is no longer configured", ErrorType.INTERNAL_ERROR)
    auth_ms = (perf_counter() - timing_start) * 1000

    output_tokens = body.output_count
    service_tier = (
        None if user_model_pair is not None else _actual_service_tier_or_error(body.service_tier)
    )
    # Owner endpoints speak the OpenAI chat dialect, whose prompt_tokens
    # already INCLUDES any cached subset — the "trustedrouter" branch of
    # normalized_prompt_accounting (also what the outbox repair path uses).
    # Adding cache counts on top would double-bill the prompt and let an owner
    # who reports cached_tokens == prompt_tokens double their revenue. Owner
    # prices have no cache tier, so the whole prompt bills at the prompt price.
    uncached_input, total_input, cache_read, cache_creation = normalized_prompt_accounting(
        selected_endpoint.provider, body
    )
    if user_model_pair is not None:
        uncached_input = total_input
    partner_mode = _partner_billing_mode_or_error(
        requested_model_id=authorization.requested_model_id,
        route_type=body.route_type,
        idempotency_key=authorization.idempotency_key,
    )
    if user_model_pair is not None and partner_mode is not None:
        raise api_error(
            400,
            "User-provided models do not support partner billing",
            ErrorType.BAD_REQUEST,
        )
    if partner_mode is not None and UsageType.for_endpoint(selected_endpoint) != UsageType.CREDITS:
        raise api_error(
            400,
            "Parasail Liberty does not support BYOK routes",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    # Only Fugu defines this provider-private tier basis. Ignoring the field
    # for every other model prevents a future provider extension from silently
    # selecting a cheaper context tier.
    price_tier_input_tokens = _provider_price_tier_input_tokens(
        selected_endpoint,
        body.price_tier_input_tokens,
    )
    actual_cost = (
        custom_model_cost_microdollars(
            input_tokens=total_input,
            output_tokens=output_tokens,
            prompt_price=int(authorization.user_model_prompt_price_microdollars_per_m or 0),
            completion_price=int(authorization.user_model_completion_price_microdollars_per_m or 0),
        )
        if user_model_pair is not None
        else partner_cost_microdollars(
            partner_mode,
            input_tokens=total_input,
            output_tokens=output_tokens,
        )
        if partner_mode is not None
        else _endpoint_cost_microdollars(
            selected_endpoint,
            uncached_input,
            output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            price_tier_input_tokens=price_tier_input_tokens,
            effective_at=authorization.created_at,
            service_tier=service_tier,
        )
    )
    app_markup_micro = 0
    if not success:
        # A refund books zero and releases the frozen hold. Never require a
        # route marker from a generic abort path; the authorization id is the
        # durable authority and refusing a refund can strand funds for 26h.
        actual_cost = 0
    elif user_model_pair is not None:
        # The token counts come from the PAYEE's own meter (the owner endpoint
        # reports usage; the enclave forwards it). Catalog providers are
        # trusted to overrun a hold by a little; an owner who reports
        # 10^7 tokens for a 10-token answer must not be able to drain the
        # caller and pocket 70%. The hold the caller authorized — estimated
        # prompt at frozen prices plus max_output — is the ceiling.
        if actual_cost > authorization.estimated_microdollars:
            logger.warning(
                "billing.user_model_settle_capped_to_hold",
                extra={
                    "authorization_id": authorization.id,
                    "user_provided_model_id": authorization.user_provided_model_id,
                    "reported_microdollars": actual_cost,
                    "hold_microdollars": authorization.estimated_microdollars,
                    "input_tokens": total_input,
                    "output_tokens": output_tokens,
                },
            )
            actual_cost = authorization.estimated_microdollars
    else:
        actual_cost = _native_batch_cost_or_error(
            actual_cost,
            route_type=body.route_type,
            provider=selected_endpoint.provider,
            idempotency_key=authorization.idempotency_key,
            native_batch_eligible=authorization.native_batch_eligible,
            selected_usage_type=UsageType.for_endpoint(selected_endpoint),
        )
    if (
        success
        and authorization.receipt_fee_basis_points > 0
        and UsageType.for_endpoint(selected_endpoint) == UsageType.CREDITS
        and user_model_pair is None
        and partner_mode is None
    ):
        actual_cost = signed_receipt_price_microdollars(
            actual_cost, authorization.receipt_fee_basis_points
        )
    additional_cost = body.additional_cost_microdollars
    if user_model_pair is not None and additional_cost:
        raise api_error(
            400,
            "User-provided models do not support additional settlement cost",
            ErrorType.BAD_REQUEST,
        )
    if additional_cost:
        if body.route_type not in _ADDITIONAL_COST_ROUTE_TYPES:
            raise api_error(
                400,
                "additional cost settlement is only available for hosted search, image, or video",
                ErrorType.BAD_REQUEST,
            )
        if additional_cost > authorization.additional_cost_reservation_microdollars:
            raise api_error(
                400,
                "additional cost exceeds the authorized reservation",
                ErrorType.BAD_REQUEST,
            )
        if UsageType.for_endpoint(selected_endpoint) != UsageType.CREDITS:
            raise api_error(
                400,
                "additional cost settlement requires a Credits route",
                ErrorType.BAD_REQUEST,
            )
        actual_cost += additional_cost
    if success and authorization.app_markup_basis_points > 0:
        # THE HOLD AND THE CHARGE MUST MARK UP THE SAME BASE: authorize marks
        # model estimate plus hosted-tool/media reservation, so settle marks up
        # model cost plus the corresponding additional cost.
        proposed_app_markup_micro = app_markup_microdollars(
            actual_cost, authorization.app_markup_basis_points
        )
        actual_cost += proposed_app_markup_micro
    input_tokens = total_input
    selected_usage_type = UsageType.for_endpoint(selected_endpoint)
    if (
        success
        and authorization.settlement == "regional_lease"
        and actual_cost > authorization.estimated_microdollars
    ):
        logger.warning(
            "billing.regional_settle_capped_to_escrow",
            extra={
                "authorization_id": authorization.id,
                "workspace_id": authorization.workspace_id,
                "estimated_microdollars": authorization.estimated_microdollars,
                "actual_microdollars": actual_cost,
                "overrun_microdollars": (actual_cost - authorization.estimated_microdollars),
            },
        )
        actual_cost = authorization.estimated_microdollars
    if success and authorization.settlement == "spend_lease":
        actual_cost = clamp_spend_lease_charge(authorization, actual_cost)
    if success and authorization.app_markup_basis_points > 0:
        # THE PAYOUT IS A FUNCTION OF THE FINAL CHARGE: authorization freezes
        # the rate, while every clamp/cap/adjustment above decides the base.
        app_markup_micro = app_markup_microdollars_from_charge(
            actual_cost, authorization.app_markup_basis_points
        )
    operator_cost = (
        owner_share_microdollars(max(0, actual_cost - app_markup_micro))
        if user_model_pair is not None
        else _endpoint_cost_microdollars(
            selected_endpoint,
            uncached_input,
            output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            price_tier_input_tokens=price_tier_input_tokens,
            effective_at=authorization.created_at,
            service_tier=service_tier,
        )
        if partner_mode == PartnerBillingMode.INTERNAL
        else None
    )
    if (
        success
        and selected_usage_type == UsageType.CREDITS
        and actual_cost > authorization.estimated_microdollars
    ):
        logger.warning(
            "billing.settlement_exceeded_reservation",
            extra={
                "authorization_id": authorization.id,
                "workspace_id": authorization.workspace_id,
                "model": model.id,
                "provider": selected_endpoint.provider,
                "estimated_microdollars": authorization.estimated_microdollars,
                "actual_microdollars": actual_cost,
                "overrun_microdollars": actual_cost - authorization.estimated_microdollars,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
    generation_model_id = (
        PARASAIL_LIBERTY_2_0_MODEL_ID if partner_mode == PartnerBillingMode.TOP_LEVEL else model.id
    )
    generation_provider = (
        "parasail" if partner_mode == PartnerBillingMode.TOP_LEVEL else selected_endpoint.provider
    )
    generation_provider_name = PROVIDERS[generation_provider].name

    generation_id: str | None = None
    generation: Generation | None = None
    if success:
        generation = Generation.from_settle_body(
            authorization=authorization,
            provider_name=generation_provider_name,
            model_id=generation_model_id,
            usage_type=selected_usage_type,
            provider=generation_provider,
            body=settle_body,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_microdollars=actual_cost,
            app_markup_microdollars=app_markup_micro,
            operator_cost_microdollars=operator_cost,
        )
        generation_id = generation.id

    user_model_payout: UserModelPayout | None = None
    if user_model_pair is not None:
        owner_user_id = str(authorization.user_model_owner_user_id or "")
        user_model_id = str(authorization.user_provided_model_id or "")
        user_model_payout = UserModelPayout(
            owner_user_id=owner_user_id,
            model_id=user_model_id,
            amount_microdollars=(
                owner_share_microdollars(max(0, actual_cost - app_markup_micro)) if success else 0
            ),
            payer_workspace_id=authorization.workspace_id,
        )

    app_markup_payout: AppMarkupPayout | None = None
    if authorization.app_markup_basis_points > 0:
        app_markup_payout_micro = (
            app_markup_owner_share_microdollars(app_markup_micro) if success else 0
        )
        if app_markup_payout_micro > app_markup_micro:
            logger.error(
                "billing.app_markup_payout_exceeded_collected_markup",
                extra={
                    "authorization_id": authorization.id,
                    "payout_microdollars": app_markup_payout_micro,
                    "markup_microdollars": app_markup_micro,
                },
            )
            app_markup_payout_micro = app_markup_micro
        app_markup_payout = AppMarkupPayout(
            owner_user_id=authorization.app_owner_user_id,
            app_id=authorization.app_id,
            amount_microdollars=app_markup_payout_micro,
            payer_workspace_id=authorization.workspace_id,
        )

    # C1 removed the GCP legacy finalize branch; Spanner finalizes through typed
    # billing unconditionally. The memory store still uses the single-book path.
    _typed_store = typed_billing_store(STORE)
    is_typed = _typed_store is not None
    intent_kind = "settle" if success else "refund"
    is_customer_billing_event = partner_mode != PartnerBillingMode.INTERNAL
    enqueue_ms = 0.0
    outbox_enqueued = False
    refill_required = (
        settings.service_surface == "internal"
        and success
        and is_customer_billing_event
        and selected_usage_type == UsageType.CREDITS
    )
    refill_attached = not refill_required
    if settings.settle_outbox_enabled:
        enqueue_start = perf_counter()
        try:
            frozen_settle_body = _settle_repair_metadata(settle_body)
            if operator_cost is not None:
                frozen_settle_body[PARTNER_OPERATOR_COST_SETTLE_FIELD] = operator_cost
            if user_model_payout is not None:
                frozen_settle_body[USER_MODEL_PAYOUT_SETTLE_FIELD] = (
                    user_model_payout.amount_microdollars
                )
                frozen_settle_body[USER_MODEL_OWNER_SETTLE_FIELD] = user_model_payout.owner_user_id
                frozen_settle_body[USER_MODEL_ID_SETTLE_FIELD] = user_model_payout.model_id
            if app_markup_payout is not None:
                frozen_settle_body[APP_MARKUP_PAYOUT_SETTLE_FIELD] = (
                    app_markup_payout.amount_microdollars
                )
                frozen_settle_body[APP_MARKUP_OWNER_SETTLE_FIELD] = app_markup_payout.owner_user_id
                frozen_settle_body[APP_MARKUP_APP_ID_SETTLE_FIELD] = app_markup_payout.app_id
            # §5.4 honest scope: durability starts only when this INSERT commits;
            # crashes before it still rely on enclave redelivery. MF4/MF5 freeze
            # the finalize path and exact resolved cost used by the inline attempt.
            settle_outbox = spanner_settle_outbox()
            settle_outbox.enqueue(
                SettleOutboxRow(
                    authorization_id=authorization.id,
                    intent_kind=intent_kind,
                    settle_origin="typed" if is_typed else "legacy",
                    actual_cost_micro=actual_cost,
                    reservation_id=authorization.credit_reservation_id,
                    selected_endpoint_id=selected_endpoint.id,
                    model_id=generation_model_id,
                    selected_usage_type=str(selected_usage_type),
                    settle_body=json.dumps(frozen_settle_body, separators=(",", ":")),
                    auto_refill_workspace_id=(
                        authorization.workspace_id if refill_required else None
                    ),
                ),
                # Grace so inline finalize wins the benign race; the drain only
                # sees rows whose inline attempt is dead >=60s, avoiding replays.
                initial_delay_seconds=60,
            )
            if refill_required:
                # Pre-cutover combined rows have no refill columns. Attaching is
                # an independent NULL -> pending transition, so it is safe even
                # when the settlement row is actively leased or terminal.
                refill_attached = settle_outbox.attach_auto_refill(
                    authorization.id,
                    authorization.workspace_id,
                    initial_delay_seconds=60,
                )
                if not refill_attached:
                    raise RuntimeError("settlement auto-refill attachment was not confirmed")
            outbox_enqueued = True
        except Exception:
            logger.error(
                "settle outbox enqueue failed authorization_id=%s",
                authorization.id,
                exc_info=True,
            )
        enqueue_ms = (perf_counter() - enqueue_start) * 1000

    if refill_required and not refill_attached:
        raise api_error(
            503,
            "Settlement refill durability is temporarily unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        )

    if (
        is_typed
        and getattr(_typed_store, "request_record_write_mode", "legacy") == "typed"
        and settings.settle_outbox_enabled
        and not outbox_enqueued
    ):
        raise api_error(
            503,
            "Settlement durability is temporarily unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        )

    finalize_start = perf_counter()
    finalize_result = TypedFinalizeResult(
        finalized=False,
        activity_indexed=False,
    )
    if is_typed:
        assert _typed_store is not None
        # Typed finalize atomically commits billing, the bounded generation
        # record, and the ClickHouse delivery intent. Benchmark delivery and
        # the temporary Bigtable migration mirror happen after that commit.
        result_method = getattr(
            _typed_store,
            "typed_finalize_gateway_authorization_result",
            None,
        )
        if callable(result_method):
            try:
                finalize_result = cast(
                    TypedFinalizeResult,
                    result_method(
                        authorization.id,
                        success=success,
                        actual_microdollars=actual_cost,
                        selected_usage_type=selected_usage_type,
                        generation=generation,
                        user_model_payout=user_model_payout,
                        app_markup_payout=app_markup_payout,
                        # Resolve the durable intent in the finalize commit
                        # itself (docs/design/durable-settle-outbox.md §7).
                        settle_outbox_done=(
                            (authorization.id, intent_kind)
                            if settings.settle_outbox_enabled and outbox_enqueued
                            else None
                        ),
                    ),
                )
            except (RegionalLeaseLedgerError, LeaseSettlementError):
                # The frozen outbox intent is already durable. A local ledger
                # conflict must remain retryable so the enclave can replay and
                # the drain can finish once the ledger is healthy; surfacing an
                # unclassified 500 only turns a recoverable billing delay into
                # a client-visible router failure.
                logger.error(
                    "regional quota settlement temporarily unavailable "
                    "authorization_id=%s lease_id=%s",
                    authorization.id,
                    authorization.regional_lease_id,
                    exc_info=True,
                )
                raise api_error(
                    503,
                    "Settlement is temporarily unavailable",
                    ErrorType.SERVICE_UNAVAILABLE,
                    headers={"Retry-After": "1"},
                ) from None
        else:
            finalized_legacy_contract = _typed_store.typed_finalize_gateway_authorization(
                authorization.id,
                success=success,
                actual_microdollars=actual_cost,
                selected_usage_type=selected_usage_type,
                generation=generation,
                user_model_payout=user_model_payout,
                app_markup_payout=app_markup_payout,
            )
            finalize_result = TypedFinalizeResult(
                finalized=finalized_legacy_contract,
                activity_indexed=finalized_legacy_contract,
            )
        finalized = finalize_result.finalized
    else:
        finalized = STORE.finalize_gateway_authorization(
            authorization.id,
            success=success,
            actual_microdollars=actual_cost,
            selected_usage_type=selected_usage_type,
            generation=generation,
        )
        if finalized:
            _credit_user_model_payout_safely(
                authorization,
                success=success,
                payout=user_model_payout,
            )
            _credit_app_markup_payout_safely(
                authorization,
                success=success,
                payout=app_markup_payout,
            )
        finalize_result = TypedFinalizeResult(
            finalized=finalized,
            activity_indexed=finalized,
        )
    finalize_ms = (perf_counter() - finalize_start) * 1000
    if finalized and is_typed and authorization.settlement == "spend_lease":
        assert _typed_store is not None
        committed = cast(Any, _typed_store).get_gateway_authorization(authorization.id)
        if committed is None:
            logger.error(
                "spend_lease.eager_mirror_read_missing",
                extra={"authorization_id": authorization.id},
            )
        else:
            mirror_finalized_spend_lease_best_effort(_typed_store, committed)
    _release_user_model_slot_safely(authorization)
    if not finalized:
        # §3/§6/§7: leave the row pending on purpose. Inline's False only says
        # "claim lost"; it cannot distinguish a charged replay from reaper-free
        # lost charge. The drain's apply_frozen_settle outcome disambiguates, and
        # marking done here would silently swallow a lost charge.
        # No timing line for replays: they would dominate the latency dataset
        # with noise instead of measuring full settle/refund work.
        return {"data": _already_settled_gateway_data(authorization)}
    _record_user_model_gateway_outcome_safely(
        authorization,
        success=success,
        error_status=body.error_status,
        error_type=body.error_type,
    )
    mark_ms = 0.0
    if settings.settle_outbox_enabled and outbox_enqueued and finalize_result.activity_indexed:
        if finalize_result.outbox_marked is not None:
            # Resolved inside the finalize commit; mark_ms stays 0.0 so the
            # timing line keeps its shape (finalize_ms now includes the mark).
            if not finalize_result.outbox_marked:
                logger.info(
                    "settle outbox done mark skipped authorization_id=%s intent_kind=%s; "
                    "row leased or already resolved; drain will re-derive done",
                    authorization.id,
                    intent_kind,
                )
        else:
            # Legacy finalize contract: the intent was not resolved in the
            # finalize commit, so resolve it here as before.
            mark_start = perf_counter()
            try:
                marked = spanner_settle_outbox().mark(authorization.id, intent_kind, done=True)
                if marked is None:
                    logger.info(
                        "settle outbox done mark skipped authorization_id=%s "
                        "intent_kind=%s; row leased or already resolved; drain will "
                        "re-derive done",
                        authorization.id,
                        intent_kind,
                    )
            except Exception:
                # Safe to swallow: §7 says a crash/failure after inline finalize
                # leaves a pending replay, and the drain will re-derive done via
                # ALREADY_SETTLED_WITH_CHARGE / ALREADY_SETTLED_LEGACY.
                logger.error(
                    "settle outbox done mark failed authorization_id=%s",
                    authorization.id,
                    exc_info=True,
                )
            mark_ms = (perf_counter() - mark_start) * 1000
    elif finalized and settings.settle_outbox_enabled and outbox_enqueued:
        logger.warning(
            "settle activity index pending repair authorization_id=%s",
            authorization.id,
        )

    if success and is_customer_billing_event and selected_usage_type == UsageType.CREDITS:
        _schedule_auto_refill(
            authorization.workspace_id,
            authorization.id,
            settings,
            background_tasks,
        )
    if success and is_customer_billing_event:
        if background_tasks is not None:
            background_tasks.add_task(
                record_successful_api_call_safely,
                authorization.workspace_id,
                model=generation_model_id,
                provider=generation_provider,
            )
        else:
            record_successful_api_call_safely(
                authorization.workspace_id,
                model=generation_model_id,
                provider=generation_provider,
            )
        # Alert-mode budgets: email the owner when a window is crossed (never
        # blocks — the block happens at authorize for limit-mode keys). Off the
        # hot path; best-effort.
        from trusted_router.services.budget_alerts import maybe_send_budget_alerts

        if background_tasks is not None:
            background_tasks.add_task(
                maybe_send_budget_alerts,
                api_key_hash=authorization.key_hash,
                workspace_id=authorization.workspace_id,
                settings=settings,
            )
        else:
            maybe_send_budget_alerts(
                api_key_hash=authorization.key_hash,
                workspace_id=authorization.workspace_id,
                settings=settings,
            )
    if success and generation is not None:
        # Customers' webhooks must not gain a new object silently: the
        # correlation id is useful to them, the client telemetry object is not.
        broadcast_settle_body = dict(settle_body)
        broadcast_settle_body.pop("client", None)
        # App attribution is frozen from the key at authorize. Never let a
        # caller-supplied root field become forgeable broadcast metadata.
        broadcast_settle_body.pop("app_id", None)
        broadcast_settle_body.pop("price_tier_input_tokens", None)
        try:
            enqueue_metadata_broadcast(generation, settle_body=broadcast_settle_body)
            broadcast_enqueued = True
        except Exception:
            # Billing is already committed. Metadata observability is
            # best-effort and must never rewrite a successful settlement as a
            # retryable 5xx, which could make the enclave replay billed work.
            logger.error(
                "broadcast_metadata_enqueue_failed generation_id=%s workspace_id=%s",
                generation.id,
                generation.workspace_id,
                exc_info=True,
            )
            broadcast_enqueued = False
        if broadcast_enqueued and should_drain_inline(settings) and background_tasks is not None:
            background_tasks.add_task(
                drain_broadcast_queue,
                settings=settings,
            )
        elif broadcast_enqueued and should_drain_inline(settings):
            drain_broadcast_queue(settings=settings)
    if not success and not _is_synthetic_settlement(body):
        STORE.record_provider_benchmark(
            ProviderBenchmarkSample.from_provider_error(
                model=model,
                provider_name=PROVIDERS[selected_endpoint.provider].name,
                input_tokens=input_tokens,
                elapsed_seconds=float(body.elapsed_seconds or 0.001),
                streamed=body.streamed,
                usage_type=selected_usage_type,
                error_status=body.error_status or 502,
                error_type=body.error_type or "provider_error",
                region=authorization.region,
                provider=selected_endpoint.provider,
                workspace_id=authorization.workspace_id,
            )
        )

    total_ms = (perf_counter() - timing_start) * 1000
    # Request-log latency minus total_ms ~= Cloud Run queue + transport time;
    # that subtraction is the point of this line (2026-07-05 latency investigation).
    logger.info(
        "settle timing authorization_id=%s success=%s origin=%s total_ms=%.1f "
        "auth_ms=%.1f enqueue_ms=%.1f finalize_ms=%.1f mark_ms=%.1f",
        authorization.id,
        success,
        "typed" if is_typed else "legacy",
        total_ms,
        auth_ms,
        enqueue_ms,
        finalize_ms,
        mark_ms,
    )
    return {
        "data": {
            "authorization_id": authorization.id,
            "settled": True,
            "finalization_outcome": "settled" if success else "refunded",
            "generation_id": generation_id,
            **money_pair("cost", actual_cost),
            "usage_type": selected_usage_type.value,
            "limit_usage_type": authorization.usage_type.value,
            "model": generation_model_id,
            "endpoint_id": selected_endpoint.id,
            "provider": generation_provider,
            "region": authorization.region,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": int(body.reasoning_tokens or 0),
            "cache_read_input_tokens": int(body.cache_read_input_tokens or 0),
        }
    }


def _credit_user_model_payout_safely(
    authorization: Any,
    *,
    success: bool,
    payout: UserModelPayout | None,
) -> None:
    if not success or payout is None or payout.amount_microdollars <= 0:
        return
    try:
        STORE.credit_user_earnings(
            payout.owner_user_id,
            payout.amount_microdollars,
            user_model_payout_event_id(authorization.id),
            custom_model_id=payout.model_id,
            payer_workspace_id=payout.payer_workspace_id,
        )
    except Exception:
        logger.error(
            "user_model_payout_failed authorization_id=%s owner=%s",
            authorization.id,
            payout.owner_user_id,
            exc_info=True,
        )


def _credit_app_markup_payout_safely(
    authorization: Any,
    *,
    success: bool,
    payout: AppMarkupPayout | None,
) -> None:
    if not success or payout is None or payout.amount_microdollars <= 0:
        return
    try:
        STORE.credit_user_earnings(
            payout.owner_user_id,
            payout.amount_microdollars,
            app_markup_payout_event_id(authorization.id),
            custom_model_id=payout.app_id,
            payer_workspace_id=payout.payer_workspace_id,
        )
    except Exception:
        logger.error(
            "app_markup_payout_failed authorization_id=%s owner=%s",
            authorization.id,
            payout.owner_user_id,
            exc_info=True,
        )


def _release_user_model_slot_safely(authorization: Any) -> None:
    model_id = authorization.user_provided_model_id
    if not model_id:
        return
    try:
        release_user_model_slot(model_id, authorization.id)
    except Exception:
        logger.warning(
            "user_model_slot_release_failed authorization_id=%s model_id=%s",
            authorization.id,
            model_id,
            exc_info=True,
        )


def _record_user_model_gateway_outcome_safely(
    authorization: Any,
    *,
    success: bool,
    error_status: int | None,
    error_type: str | None,
) -> None:
    model_id = authorization.user_provided_model_id
    if not model_id:
        return
    dispatch_success: bool | None = True if success else None
    if not success and is_owner_fault(error_status, error_type):
        dispatch_success = False
    if dispatch_success is None:
        return
    try:
        record_user_model_gateway_result(
            model_id,
            success=dispatch_success,
        )
    except Exception:
        # Deleting or renaming the live model never invalidates frozen billing.
        logger.warning(
            "user_model_dispatch_result_failed authorization_id=%s model_id=%s",
            authorization.id,
            model_id,
            exc_info=True,
        )


def _already_settled_gateway_data(authorization: Any) -> dict[str, Any]:
    """Return the stable winner of a settle/refund race.

    The billing authorization payload is authoritative. Generation/activity
    mirrors may lag or fail after the Spanner money transaction commits, so
    their presence must never be used to distinguish a charge from a refund.
    """
    authorization = STORE.get_gateway_authorization(authorization.id) or authorization
    outcome = str(authorization.finalization_outcome or "").strip().lower()
    data: dict[str, Any] = {
        "authorization_id": authorization.id,
        "settled": outcome == "settled",
        "already_settled": True,
        "finalization_outcome": outcome or "pending",
    }
    if outcome == "refunded":
        data.update(money_pair("cost", 0))
        return data
    if outcome != "settled":
        # Rolling records without the explicit outcome fail closed. A missing
        # Bigtable generation cannot be interpreted as a refund.
        return data
    if authorization.finalized_generation_id:
        data["generation_id"] = authorization.finalized_generation_id
    data.update(money_pair("cost", int(authorization.finalized_cost_microdollars or 0)))
    if authorization.finalized_usage_type:
        data["usage_type"] = authorization.finalized_usage_type
    if authorization.finalized_model_id:
        data["model"] = authorization.finalized_model_id
    if authorization.finalized_provider:
        data["provider"] = authorization.finalized_provider
    if authorization.finalized_region:
        data["region"] = authorization.finalized_region
    data["input_tokens"] = int(authorization.finalized_input_tokens or 0)
    data["output_tokens"] = int(authorization.finalized_output_tokens or 0)
    data["reasoning_tokens"] = int(authorization.finalized_reasoning_tokens or 0)
    data["cache_read_input_tokens"] = int(authorization.finalized_cached_input_tokens or 0)
    return data


def _settle_body_with_safe_attribution(
    body: GatewaySettleRequest, authorization_id: str
) -> dict[str, Any]:
    settle_body = body.model_dump(exclude_none=True)
    settle_body.pop("tags", None)
    attribution_keys = (
        "user",
        "session_id",
        "trace",
        "app",
        "http_referer",
        "app_categories",
    )
    try:
        attribution = validate_request_attribution(
            user=body.user,
            session_id=body.session_id,
            trace=body.trace,
            app=body.app,
            http_referer=body.http_referer,
            app_categories=body.app_categories,
        )
    except InvalidAttribution as exc:
        for key in attribution_keys:
            settle_body.pop(key, None)
        logger.warning(
            "invalid gateway settlement attribution dropped authorization_id=%s error_class=%s",
            authorization_id,
            type(exc).__name__,
        )
        return settle_body
    for key in attribution_keys:
        settle_body.pop(key, None)
    settle_body.update(attribution.body_fields())
    return settle_body


def _settle_body_with_safe_client_context(
    settle_body: dict[str, Any], authorization_id: str
) -> dict[str, Any]:
    """Soft-validate the enclave's client telemetry; drop it rather than 4xx.

    Telemetry may never fail settlement. Anything that does not match the
    closed vocabulary in ``client_context`` is removed and logged; the money
    path continues unchanged.
    """
    if "client" in settle_body:
        client_context = parse_client_context(settle_body.get("client"))
        if client_context is None:
            settle_body.pop("client", None)
            logger.warning(
                "invalid gateway settlement client context dropped "
                "authorization_id=%s error_class=%s",
                authorization_id,
                "ValidationError",
            )
        else:
            settle_body["client"] = client_context.model_dump(exclude_none=True)

    if "gateway_request_id" in settle_body:
        gateway_request_id = parse_gateway_request_id(settle_body.get("gateway_request_id"))
        if gateway_request_id is None:
            settle_body.pop("gateway_request_id", None)
        else:
            settle_body["gateway_request_id"] = gateway_request_id
    return settle_body


def _settle_repair_metadata(settle_body: dict[str, Any]) -> dict[str, Any]:
    """Freeze only fields needed to reconstruct activity metadata.

    The durable outbox is an operational repair log, not a content store.
    Lenient request extras, trace dictionaries, and arbitrary metadata are
    deliberately excluded. The one metadata bit retained marks synthetic
    traffic so repaired rows stay out of public provider benchmarks.
    """
    frozen = {key: value for key, value in settle_body.items() if key in _SETTLE_REPAIR_FIELDS}
    metadata = settle_body.get("metadata")
    if isinstance(metadata, dict) and _synthetic_metadata_enabled(metadata):
        frozen["metadata"] = {"trustedrouter_synthetic": "true"}
    return frozen


def _synthetic_metadata_enabled(metadata: dict[str, Any]) -> bool:
    return str(metadata.get("trustedrouter_synthetic")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_synthetic_settlement(body: GatewaySettleRequest) -> bool:
    if body.app == "TrustedRouter Synthetic":
        return True
    metadata = body.metadata
    if not isinstance(metadata, dict):
        return False
    return _synthetic_metadata_enabled(metadata)


def _gateway_candidate_payload(
    model: Model,
    endpoint: ModelEndpoint,
    workspace_id: str,
    region: str,
) -> dict[str, Any]:
    usage_type = UsageType.for_endpoint(endpoint)
    byok_config = (
        _get_byok_provider(workspace_id, endpoint.provider) if usage_type.is_byok() else None
    )
    return {
        "endpoint_id": endpoint.id,
        "model": model.id,
        "upstream_model": endpoint.upstream_id or model.id,
        "provider": endpoint.provider,
        "provider_name": PROVIDERS[endpoint.provider].name,
        **_gateway_provider_route_payload(endpoint),
        "usage_type": usage_type.value,
        **_gateway_byok_payload(byok_config, workspace_id),
        "region": region,
    }


def _gateway_provider_route_payload(endpoint: ModelEndpoint) -> dict[str, Any]:
    """Return provider-specific, typed enforcement metadata for the enclave."""

    if endpoint.provider == "wafer" and endpoint_zero_data_retention(endpoint) is True:
        return {"wafer_zdr_required": True}
    return {}


def _gateway_byok_payload(byok_config: Any | None, workspace_id: str) -> dict[str, Any]:
    if byok_config is None:
        return {
            "byok_secret_ref": None,
            "byok_encrypted_secret": None,
            "byok_cache_key": None,
            "byok_key_hint": None,
            "byok_provider": None,
        }
    envelope_provider = str(byok_config.provider)
    return {
        "byok_secret_ref": byok_config.secret_ref,
        "byok_encrypted_secret": encrypted_secret_payload(byok_config.encrypted_secret),
        "byok_cache_key": byok_cache_key(
            byok_config.encrypted_secret,
            workspace_id=workspace_id,
            provider=envelope_provider,
        ),
        "byok_key_hint": byok_config.key_hint,
        "byok_provider": envelope_provider,
    }


def _user_model_gateway_candidate(
    user_model: UserProvidedModel,
) -> tuple[Model, ModelEndpoint]:
    """Build the Credits-only authorization sentinel for owner dispatch.

    User-provided models deliberately do not enter the frozen catalog.  The
    gateway still needs a stable model/endpoint pair for its reservation
    record, so this pair exists only inside the authorization response; the
    enclave resolves the actual owner dispatch block separately.
    """
    return user_model_gateway_pair(
        model_id=user_model.id,
        name=user_model.name,
        revision=user_model.revision,
        prompt_price_microdollars_per_m=(user_model.prompt_price_microdollars_per_million_tokens),
        completion_price_microdollars_per_m=(
            user_model.completion_price_microdollars_per_million_tokens
        ),
        owner_user_id=user_model.owner_user_id,
        upstream_model_id=user_model.upstream_model_id,
    )


def _authorized_user_model_pair(
    authorization: Any,
) -> tuple[Model, ModelEndpoint] | None:
    """Rebuild a user-model sentinel only from authorization-frozen money facts.

    The live model is consulted solely for a display name. Its deletion,
    revision, prices, owner, endpoint, and secret material have no authority
    over settlement.
    """
    model_id = authorization.user_provided_model_id
    if not model_id:
        return None
    name = model_id
    try:
        live_model = STORE.get_user_model(model_id)
    except Exception:
        live_model = None
    if live_model is not None:
        name = live_model.name
    revision = authorization.user_provided_model_revision
    prompt_price = authorization.user_model_prompt_price_microdollars_per_m
    completion_price = authorization.user_model_completion_price_microdollars_per_m
    owner_user_id = authorization.user_model_owner_user_id
    if revision is None or prompt_price is None or completion_price is None or not owner_user_id:
        return None
    try:
        return user_model_gateway_pair(
            model_id=model_id,
            name=name,
            revision=int(revision),
            prompt_price_microdollars_per_m=int(prompt_price),
            completion_price_microdollars_per_m=int(completion_price),
            owner_user_id=owner_user_id,
            upstream_model_id=model_id,
        )
    except (TypeError, ValueError):
        return None


def _eligible_gateway_endpoint_candidates(
    candidates: list[tuple[Model, ModelEndpoint]],
    workspace_id: str,
) -> list[tuple[Model, ModelEndpoint]]:
    out: list[tuple[Model, ModelEndpoint]] = []
    for model, endpoint in candidates:
        usage_type = UsageType.for_endpoint(endpoint)
        if usage_type.is_byok() and _get_byok_provider(workspace_id, endpoint.provider) is None:
            continue
        out.append((model, endpoint))
    return out


def _get_byok_provider(workspace_id: str, provider: str) -> Any | None:
    for storage_slug in byok_storage_provider_candidates(provider):
        config = STORE.get_byok_provider(workspace_id, storage_slug)
        if config is not None:
            return config
    return None


def _select_authorized_endpoint(
    authorization: Any, body: GatewaySettleRequest
) -> ModelEndpoint | None:
    user_model_pair = _authorized_user_model_pair(authorization)
    if user_model_pair is not None:
        frozen_model, frozen_endpoint = user_model_pair
        if body.selected_endpoint_id not in {None, frozen_endpoint.id}:
            return None
        # The enclave may echo the caller's raw spelling of the id
        # ("TrustedRouter/User-Foo", the short "user-foo" alias); authorize
        # normalized it before freezing, so compare normalized. A refused
        # settle here strands the hold, so be no stricter than the id grammar.
        selected_model_id = body.selected_model_id
        if (
            selected_model_id is not None
            and normalize_custom_model_id(selected_model_id) != frozen_model.id
        ):
            return None
        return frozen_endpoint
    authorized_endpoint_ids = authorization.candidate_endpoint_ids or []
    if not authorized_endpoint_ids and authorization.endpoint_id:
        authorized_endpoint_ids = [authorization.endpoint_id]
    selected_endpoint_id = body.selected_endpoint_id
    if selected_endpoint_id is not None:
        if selected_endpoint_id not in authorized_endpoint_ids:
            return None
        return _endpoint_for_id_compat(selected_endpoint_id)

    selected_model_id = body.selected_model_id or authorization.model_id
    if selected_model_id == authorization.model_id and authorization.endpoint_id:
        return _endpoint_for_id_compat(authorization.endpoint_id)

    for endpoint_id in authorized_endpoint_ids:
        endpoint = _endpoint_for_id_compat(endpoint_id)
        if endpoint is not None and endpoint.model_id == selected_model_id:
            return endpoint

    authorized_model_ids = authorization.candidate_model_ids or [authorization.model_id]
    if selected_model_id not in authorized_model_ids:
        return None
    model = MODELS.get(selected_model_id)
    return default_endpoint_for_model(model) if model is not None else None


def _endpoint_for_id_compat(endpoint_id: str) -> ModelEndpoint | None:
    endpoint = endpoint_for_id(endpoint_id)
    if endpoint is not None:
        return endpoint

    model_id, separator, usage_suffix = endpoint_id.partition("@gemini/")
    if not separator or usage_suffix not in {"prepaid", "byok"}:
        return None
    model = MODELS.get(model_id)
    if model is None:
        return None

    # Before the split, prepaid chat used Vertex while Gemini embeddings and
    # BYOK used AI Studio. Preserve that exact route when an authorization
    # created by the old control plane settles or replays after deployment.
    provider = (
        "google-ai-studio"
        if usage_suffix == "byok" or model.supports_embeddings
        else "google-vertex"
    )
    return endpoint_for_id(f"{model_id}@{provider}/{usage_suffix}")


def _requested_service_tier_or_error(service_tier: str | None) -> str | None:
    if service_tier is None:
        return None
    normalized = service_tier.strip().lower()
    if normalized not in OPENAI_SERVICE_TIERS:
        raise api_error(
            400,
            "service_tier must be default, auto, or priority",
            ErrorType.BAD_REQUEST,
        )
    return normalized


# Providers name the ordinary, non-expedited tier differently. Anthropic
# reports usage.service_tier="standard"; OpenAI reports "default". They mean
# the same thing and price the same, so both settle as "default".
#
# Deliberately NOT aliased: Anthropic "batch" and OpenAI "flex"/"scale" are
# cheaper tiers, and quietly settling one of those as "default" would overcharge
# the customer. An unrecognized tier still fails loudly rather than mis-pricing.
_SERVICE_TIER_SYNONYMS = {"standard": "default"}


def _actual_service_tier_or_error(service_tier: str | None) -> str | None:
    if service_tier is None:
        return None
    normalized = service_tier.strip().lower()
    normalized = _SERVICE_TIER_SYNONYMS.get(normalized, normalized)
    if normalized not in {"default", "priority"}:
        raise api_error(
            400,
            "settlement service_tier must be the actual default or priority tier",
            ErrorType.BAD_REQUEST,
        )
    return normalized


def _service_tier_endpoint_candidates_or_error(
    candidates: list[tuple[Model, ModelEndpoint]],
    *,
    service_tier: str | None,
    estimated_input_tokens: int,
) -> list[tuple[Model, ModelEndpoint]]:
    if service_tier is None:
        return candidates
    openai_candidates = [
        (model, endpoint) for model, endpoint in candidates if endpoint.provider == "openai"
    ]
    if service_tier in {"auto", "priority"}:
        openai_candidates = [
            (model, endpoint)
            for model, endpoint in openai_candidates
            if openai_priority_pricing(endpoint.model_id) is not None
        ]
    if service_tier == "priority" and estimated_input_tokens > OPENAI_PRIORITY_MAX_PROMPT_TOKENS:
        raise api_error(
            400,
            "OpenAI Priority processing does not support prompts over 272000 tokens",
            ErrorType.BAD_REQUEST,
        )
    if not openai_candidates:
        raise api_error(
            400,
            f"OpenAI {service_tier} processing is unavailable for the requested model",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    return openai_candidates


def _is_native_batch_route(route_type: str | None) -> bool:
    return (route_type or "").strip().lower().startswith(_NATIVE_BATCH_ROUTE_PREFIX)


def _native_batch_eligibility(
    *,
    route_type: str | None,
    retention_allowed: bool,
    model_usage_type: UsageType,
    custom_model: Any | None,
    broadcast_destinations: list[dict[str, Any]],
    requested_model_id: str,
    endpoint: ModelEndpoint,
) -> bool:
    """Freeze the provider-retention and discount decision at authorization."""
    return (
        _is_native_batch_route(route_type)
        and retention_allowed
        and model_usage_type == UsageType.CREDITS
        and custom_model is None
        and not broadcast_destinations
        and not requested_model_id.strip().lower().startswith("trustedrouter/")
        and endpoint.provider.strip().lower() in _NATIVE_BATCH_BILLED_FRACTION_BPS
    )


def _native_batch_request_allows_retention(body: dict[str, Any], settings: Settings) -> bool:
    """Fail closed on privacy/routing metadata visible to the control plane.

    Provider-native Batch APIs temporarily retain request and result content.
    The measured enclave separately validates the complete request body with a
    strict field allowlist before exporting content. This second gate covers
    the non-content routing state the control plane can independently verify.
    """
    if body.get("models"):
        return False
    if ":" in str(body.get("model") or ""):
        # Native Batch retention is an opt-in contract. Keep every shorthand
        # model variant on the managed path until that suffix is explicitly
        # audited, even when today's suffix changes only price or throughput.
        return False
    if body.get("service_tier"):
        return False
    if any(
        key in body
        for key in {
            "zdr",
            "e2e",
            "confidential",
            "data_collection",
            "min_privacy",
            "jurisdiction",
            "store",
        }
    ):
        return False
    provider = body.get("provider")
    if provider is not None and (
        not isinstance(provider, dict)
        or any(key not in _NATIVE_BATCH_PROVIDER_FIELDS for key in provider)
    ):
        return False
    provider = provider or {}
    if (
        str(provider.get("data_collection") or "").strip().lower() == "deny"
        or bool(str(provider.get("min_privacy") or "").strip())
        or bool(str(provider.get("jurisdiction") or "").strip())
        or str(provider.get("usage") or "").strip().lower() == "byok"
    ):
        return False
    preferences = resolved_route_preferences(body, settings)
    return not (
        preferences.data_collection == "deny"
        or preferences.min_privacy_rank > 0
        or preferences.provider_jurisdiction is not None
        or preferences.usage_type == "BYOK"
    )


def _is_native_batch_idempotency_key(idempotency_key: str | None) -> bool:
    return (idempotency_key or "").startswith("tr-native-batch:")


def _require_native_batch_route_binding(
    route_type: str | None,
    idempotency_key: str | None,
) -> None:
    if _is_native_batch_route(route_type) != _is_native_batch_idempotency_key(idempotency_key):
        raise api_error(
            400,
            "native Batch route must use its enclave-generated idempotency key",
            ErrorType.BAD_REQUEST,
        )


def _authorization_ttl_seconds(route_type: str | None) -> int:
    return 26 * 60 * 60 if _is_native_batch_route(route_type) else GATEWAY_RESERVATION_TTL_SECONDS


def _native_batch_cost_or_error(
    cost_microdollars: int,
    *,
    route_type: str | None,
    provider: str,
    idempotency_key: str | None = None,
    native_batch_eligible: bool = False,
    selected_usage_type: UsageType = UsageType.CREDITS,
) -> int:
    if not _is_native_batch_route(route_type):
        if _is_native_batch_idempotency_key(idempotency_key):
            raise api_error(
                400,
                "native Batch authorization requires native Batch settlement",
                ErrorType.BAD_REQUEST,
            )
        return cost_microdollars
    _require_native_batch_route_binding(route_type, idempotency_key)
    if not native_batch_eligible:
        raise api_error(
            400,
            "authorization is not eligible for native Batch settlement",
            ErrorType.BAD_REQUEST,
        )
    if selected_usage_type != UsageType.CREDITS:
        raise api_error(
            400,
            "native Batch settlement requires a prepaid provider route",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    billed_fraction_bps = _NATIVE_BATCH_BILLED_FRACTION_BPS.get(provider.strip().lower())
    if billed_fraction_bps is None:
        raise api_error(
            400,
            "selected provider does not support TrustedRouter native Batch settlement",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    if cost_microdollars <= 0:
        return 0
    # Round upward so a positive billable request never disappears below the
    # integer microdollar ledger's minimum unit.
    return max(1, (cost_microdollars * billed_fraction_bps + 9_999) // 10_000)


def _provider_price_tier_input_tokens(
    endpoint: ModelEndpoint,
    reported_input_tokens: int | None,
) -> int | None:
    """Admit a provider-private tier basis only for its pinned model contract."""

    if endpoint.provider == "sakana" and endpoint.model_id == SAKANA_FUGU_MODEL_ID:
        return reported_input_tokens
    return None


def _endpoint_cost_microdollars(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
    effective_at: datetime | str | None = None,
    service_tier: str | None = None,
    reserve_auto: bool = False,
) -> int:
    """input_tokens must be the UNCACHED prompt tokens when cache counts
    are passed — cached reads/writes bill at the provider-specific
    multiple of the prompt price (see catalog.cache_token_prices_microdollars)."""
    endpoint = effective_endpoint(endpoint, at=effective_at)
    if service_tier == "priority" or (service_tier == "auto" and reserve_auto):
        if endpoint.provider != "openai":
            raise ValueError("OpenAI service tiers require an OpenAI endpoint")
        return openai_priority_cost_microdollars(
            endpoint.model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
    total_prompt = input_tokens + cache_read_tokens + cache_creation_tokens
    # Some providers expose separately billable internal orchestration tokens
    # while selecting their long-context tier from the initial request context.
    # The attested gateway supplies that exact provider-metered count. Invalid
    # values fall back to the larger aggregate, which is conservative for COGS.
    tier_prompt = total_prompt
    if (
        price_tier_input_tokens is not None
        and price_tier_input_tokens > 0
        and price_tier_input_tokens <= total_prompt
    ):
        tier_prompt = price_tier_input_tokens
    rates = resolve_request_rates(
        getattr(endpoint, "price_tiers", ()) or (),
        headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=tier_prompt,
    )
    prompt_price = rates.prompt_price_microdollars_per_million_tokens

    cost = (
        endpoint.request_price_microdollars
        + token_cost_microdollars(input_tokens, prompt_price)
        + token_cost_microdollars(
            output_tokens,
            rates.completion_price_microdollars_per_million_tokens,
        )
    )
    has_positive_charge = (
        endpoint.request_price_microdollars > 0
        or (input_tokens > 0 and prompt_price > 0)
        or (output_tokens > 0 and rates.completion_price_microdollars_per_million_tokens > 0)
    )
    if cache_read_tokens or cache_creation_tokens:
        default_read_price, write_price = cache_token_prices_microdollars(
            endpoint.provider, prompt_price
        )
        read_price = (
            rates.prompt_cached_price_microdollars_per_million_tokens
            if rates.prompt_cached_price_microdollars_per_million_tokens is not None
            else default_read_price
        )
        cost += token_cost_microdollars(cache_read_tokens, read_price)
        cost += token_cost_microdollars(cache_creation_tokens, write_price)
        has_positive_charge = (
            has_positive_charge
            or (cache_read_tokens > 0 and read_price > 0)
            or (cache_creation_tokens > 0 and write_price > 0)
        )
    # Microdollars are the ledger's smallest unit. A positive-priced request
    # must still reserve and settle one unit when its exact fractional cost
    # rounds below one microdollar; otherwise tiny calls can bypass key limits.
    return max(cost, 1) if has_positive_charge else 0


def _partner_billing_mode_or_error(
    *,
    requested_model_id: str | None,
    route_type: str | None,
    idempotency_key: str | None,
) -> PartnerBillingMode | None:
    try:
        return partner_billing_mode(
            requested_model_id=requested_model_id,
            route_type=route_type,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc


def _schedule_auto_refill(
    workspace_id: str,
    authorization_id: str,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> None:
    from trusted_router.services.auto_refill import (
        maybe_charge_after_settle,
        settlement_auto_refill_idempotency_key,
    )

    # The internal surface has no Stripe credential. Its successful credit
    # settlement attached durable refill work to the settlement outbox row;
    # the control-owned drain performs the charge. Combined preserves the
    # legacy in-process behavior below.
    if settings.service_surface == "internal":
        return
    idempotency_key = settlement_auto_refill_idempotency_key(authorization_id)
    if background_tasks is not None:
        background_tasks.add_task(
            maybe_charge_after_settle,
            workspace_id,
            settings=settings,
            idempotency_key=idempotency_key,
        )
        return
    maybe_charge_after_settle(
        workspace_id,
        settings=settings,
        idempotency_key=idempotency_key,
    )
