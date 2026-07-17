"""/internal/gateway/{authorize,settle,refund} — the cross-request
reservation handle the attested gateway uses.

Authorize reserves credits + per-key spend cap and returns an
authorization id. Settle and refund land on a separate request from
the authorize call (the enclave settles after streaming finishes).
The reservation is one-shot: a second settle on the same authorization
returns already_settled=True without double-charging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from starlette.concurrency import run_in_threadpool

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
)
from trusted_router.config import Settings
from trusted_router.errors import api_error, assert_workspace_billing_active
from trusted_router.money import money_pair, token_cost_microdollars
from trusted_router.pricing import resolve_request_rates
from trusted_router.provider_types import estimate_tokens_from_text
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
    provider_route_preferences,
)
from trusted_router.schemas import (
    GatewayAuthorizeRequest,
    GatewayResolveCustomModelRequest,
    GatewaySettleRequest,
    GatewayValidateRequest,
)
from trusted_router.security import lookup_hash_api_key
from trusted_router.services.broadcast import (
    drain_broadcast_queue,
    enqueue_metadata_broadcast,
    gateway_destination_payload,
    should_drain_inline,
)
from trusted_router.services.settle_outbox_apply import normalized_prompt_accounting
from trusted_router.services.settle_outbox_drain import (
    drain_settle_outbox,
    spanner_settle_outbox,
)
from trusted_router.storage import (
    STORE,
    Generation,
    ProviderBenchmarkSample,
    typed_billing_store,
)
from trusted_router.storage_custom_models import is_custom_model_id, normalize_custom_model_id
from trusted_router.storage_models import SettleOutboxRow
from trusted_router.types import ErrorType, UsageType

logger = logging.getLogger(__name__)
REQUEST_METADATA_VERSION = 1


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
    return await run_in_threadpool(_authorize_gateway_sync, request, body, settings)


def _authorize_gateway_sync(
    request: Request,
    body: GatewayAuthorizeRequest,
    settings: Settings,
) -> dict[str, Any]:
    """Core gateway-authorize logic, extracted from the route closure so it is a
    named, directly unit-testable function (#40). The registered route handler is
    a thin wrapper; behavior is byte-identical to the prior inline handler."""
    require_internal_gateway(request, settings)
    api_key = _api_key_for_gateway_authorization(body)
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
    assert_workspace_billing_active(workspace)
    body_dict = body.model_dump(exclude_none=True)
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
        raise api_error(
            400, str(exc), ErrorType.INVALID_REQUEST_METADATA
        ) from exc
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
    if is_custom_model_id(requested_model_id):
        custom_model = STORE.get_custom_model(normalize_custom_model_id(requested_model_id))
        if custom_model is None or not custom_model.enabled:
            raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
        body_dict["model"] = custom_model.base_model_id
        body_dict.pop("models", None)
        body_dict["custom_model_id"] = custom_model.id
        body_dict["custom_model_revision"] = custom_model.revision
        _force_custom_model_credit_routes(body_dict)
    # Embedding-only models can't go through the chat resolver (it
    # rejects supports_chat=False). Route them to the embeddings
    # resolver so the attested enclave can authorize + bill an
    # embeddings call exactly like a chat one.
    route_model_id = str(body_dict.get("model") or body.model)
    requested_model = MODELS.get(route_model_id) if route_model_id else None
    is_embeddings_request = (
        requested_model is not None
        and requested_model.supports_embeddings
        and not requested_model.supports_chat
    )
    if is_embeddings_request:
        endpoint_candidates = embeddings_route_endpoint_candidates(body_dict, settings)
        if not endpoint_candidates:
            raise api_error(
                400, "Model does not support embeddings", ErrorType.MODEL_NOT_SUPPORTED
            )
    else:
        endpoint_candidates = chat_route_endpoint_candidates(body_dict, settings)
        if not endpoint_candidates:
            raise api_error(
                400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED
            )
    endpoint_candidates = _eligible_gateway_endpoint_candidates(
        endpoint_candidates, workspace.id
    )
    if not endpoint_candidates:
        raise api_error(
            400,
            "No authorized route candidates are available for this workspace",
            ErrorType.PROVIDER_NOT_SUPPORTED,
        )
    model, endpoint = endpoint_candidates[0]
    region = choose_region(settings, body.region or None)

    input_tokens = body.estimated_input_tokens
    if custom_model is not None and custom_model.hidden_prompt.strip():
        input_tokens += estimate_tokens_from_text(custom_model.hidden_prompt)
    output_tokens = body.output_estimate
    estimate = max(
        _endpoint_cost_microdollars(candidate_endpoint, input_tokens, output_tokens)
        for _candidate_model, candidate_endpoint in endpoint_candidates
    )
    model_usage_type = UsageType.for_endpoint(endpoint)
    has_credit_candidate = any(
        UsageType.for_endpoint(candidate_endpoint) == UsageType.CREDITS
        for _candidate_model, candidate_endpoint in endpoint_candidates
    )
    reservation_usage_type = UsageType.CREDITS if has_credit_candidate else UsageType.BYOK
    request_idempotency_key = _gateway_idempotency_key(request, body) or str(
        uuid.uuid4()
    )
    fingerprint_body = dict(body_dict)
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
            STORE.get_byok_provider(workspace.id, existing_endpoint.provider)
            if existing_usage_type.is_byok()
            else None
        )
        broadcast_destinations = [
            payload
            for destination in STORE.list_broadcast_destinations(workspace.id)
            if (payload := gateway_destination_payload(destination)) is not None
        ]
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

    existing_authorization = STORE.get_gateway_authorization_by_idempotency_key(
        workspace.id, api_key.hash, request_idempotency_key
    )
    if existing_authorization is None:
        # Typed authorizations have no JSON idempotency index; whenever the
        # active store has typed billing, retries must replay from the typed
        # table because the legacy cohort brake no longer exists after C1.
        _typed_store = typed_billing_store(STORE)
        if _typed_store is not None:
            existing_authorization = _typed_store.get_typed_authorization_by_idempotency(
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
    credit_reservation_id: str | None = None
    idempotent_replay = False
    # C1 removed the workspace cohort/denylist brake: GCP now always uses typed
    # billing when the store exposes that capability. Emergency rollback is the
    # previous deploy revision; the memory store below remains the test twin.
    _typed_store = typed_billing_store(STORE)
    if _typed_store is not None:
        import datetime as _dt

        from trusted_router.spend_windows import (
            enforced_window_limits,
            utcnow,
            window_resets_at,
        )
        from trusted_router.storage_gcp_authorize import AuthorizeOutcome
        from trusted_router.storage_gcp_counters import key_usage_shard_count

        # expires_at = generous execution deadline (> max stream + settle
        # retry window) so the reaper only reclaims genuinely-abandoned holds.
        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=7200)
        # Per-window key caps (approximate). Omitted entirely for a BYOK
        # request on a key that excludes BYOK from its caps — same rule the
        # lifetime cap applies (authorize_atomic's window_limits contract).
        is_byok_request = not has_credit_candidate
        window_limits = (
            {}
            if is_byok_request and not api_key.include_byok_in_limit
            else enforced_window_limits(api_key)  # {} in alert mode → never blocks
        )
        outcome, authorization = _typed_store.authorize_gateway_typed(
            workspace_id=workspace.id,
            key_hash=api_key.hash,
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
            key_usage_shards=key_usage_shard_count(api_key),
            custom_model_id=custom_model.id if custom_model else None,
            custom_model_revision=custom_model.revision if custom_model else None,
            expires_at=expires_at,
            window_limits=window_limits or None,
        )
        if outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS:
            raise api_error(402, "Insufficient credits", ErrorType.INSUFFICIENT_CREDITS)
        if outcome.startswith(AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED):
            _, _, window = outcome.partition(":")
            window = window or "daily"
            resets_at = window_resets_at(window, utcnow())
            retry_after = max(1, int((resets_at - utcnow()).total_seconds()))
            raise api_error(
                429,
                f"API key {window} spend limit exceeded; resets at "
                f"{resets_at.isoformat().replace('+00:00', 'Z')}",
                ErrorType.KEY_WINDOW_LIMIT_EXCEEDED,
                headers={"Retry-After": str(retry_after)},
            )
        if outcome in (AuthorizeOutcome.KEY_LIMIT_EXCEEDED, AuthorizeOutcome.KEY_MISSING):
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            )
        if outcome == AuthorizeOutcome.IDEMPOTENCY_MISMATCH:
            raise api_error(
                409,
                "Idempotency key was already used for a different gateway request",
                ErrorType.CONFLICT,
            )
        if authorization is None:
            raise api_error(500, "gateway authorize failed", ErrorType.INTERNAL_ERROR)
        if outcome == AuthorizeOutcome.REPLAY:
            # concurrent-race replay: respond from the STORED authorization
            return _replay_response(authorization)
        credit_reservation_id = authorization.credit_reservation_id
    else:
        from trusted_router.spend_windows import (
            KeyWindowLimitExceeded,
            utcnow,
            window_resets_at,
        )

        try:
            STORE.reserve_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
        except KeyWindowLimitExceeded as exc:
            # InMemory twin of the typed window rejection (same 429 shape).
            resets_at = window_resets_at(exc.window, utcnow())
            retry_after = max(1, int((resets_at - utcnow()).total_seconds()))
            raise api_error(
                429,
                f"API key {exc.window} spend limit exceeded; resets at "
                f"{resets_at.isoformat().replace('+00:00', 'Z')}",
                ErrorType.KEY_WINDOW_LIMIT_EXCEEDED,
                headers={"Retry-After": str(retry_after)},
            ) from exc
        except ValueError as exc:
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc

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
                STORE.refund_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
                raise api_error(
                    402, "Insufficient credits", ErrorType.INSUFFICIENT_CREDITS
                ) from exc

        authorization = STORE.create_gateway_authorization(
            workspace_id=workspace.id,
            key_hash=api_key.hash,
            model_id=model.id,
            provider=endpoint.provider,
            usage_type=reservation_usage_type,
            estimated_microdollars=estimate,
            credit_reservation_id=credit_reservation_id,
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
            custom_model_id=custom_model.id if custom_model else None,
            custom_model_revision=custom_model.revision if custom_model else None,
        )
    byok_config = (
        STORE.get_byok_provider(workspace.id, endpoint.provider)
        if model_usage_type.is_byok()
        else None
    )
    broadcast_destinations = [
        payload
        for destination in STORE.list_broadcast_destinations(workspace.id)
        if (payload := gateway_destination_payload(destination)) is not None
    ]
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
        raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
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
        raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
    from trusted_router.routes.keys import _enriched_key_shape

    return {"data": _enriched_key_shape(api_key)}


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
        raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
    assert_workspace_billing_active(workspace)
    if not is_custom_model_id(body.model):
        raise api_error(400, "Model is not a custom model", ErrorType.BAD_REQUEST)
    custom_model = STORE.get_custom_model(normalize_custom_model_id(body.model))
    if custom_model is None or not custom_model.enabled:
        raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
    return {
        "data": {
            "workspace_id": workspace.id,
            "api_key_hash": api_key.hash,
            "route_type": body.route_type,
            "custom_model": {
                "id": custom_model.id,
                "name": custom_model.name,
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
        return await run_in_threadpool(
            _gateway_resolve_custom_model_sync, request, body, settings
        )

    @router.post("/internal/gateway/authorize")
    async def gateway_authorize(
        request: Request,
        body: GatewayAuthorizeRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await authorize_gateway(request, body, settings)

    @router.post("/internal/gateway/settle")
    async def gateway_settle(
        request: Request,
        body: GatewaySettleRequest,
        settings: SettingsDep,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        # background_tasks.add_task is a plain list append inside the sync core;
        # the tasks themselves still run on the loop after the response.
        return await run_in_threadpool(
            _settle_gateway_authorization,
            body,
            success=True,
            settings=settings,
            background_tasks=background_tasks,
        )

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
        return await run_in_threadpool(drain_settle_outbox, limit)


def _api_key_for_gateway_authorization(body: GatewayAuthorizeRequest) -> Any | None:
    return _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )


def _gateway_idempotency_key(
    request: Request, body: GatewayAuthorizeRequest
) -> str | None:
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
    *, workspace_id: str, key_hash: str, body: dict[str, Any]
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
    material["workspace_id"] = workspace_id
    material["key_hash"] = key_hash
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _authorization_endpoint_candidates(
    authorization: Any,
    fallback: list[tuple[Model, ModelEndpoint]],
) -> list[tuple[Model, ModelEndpoint]]:
    candidates: list[tuple[Model, ModelEndpoint]] = []
    endpoint_ids = authorization.candidate_endpoint_ids or []
    if not endpoint_ids and authorization.endpoint_id:
        endpoint_ids = [authorization.endpoint_id]
    for endpoint_id in endpoint_ids:
        endpoint = endpoint_for_id(endpoint_id)
        if endpoint is None:
            continue
        model = MODELS.get(endpoint.model_id)
        if model is None:
            continue
        candidates.append((model, endpoint))
    return candidates or fallback


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
            "requested_model": requested_model_id,
            "usage_type": model_usage_type.value,
            "limit_usage_type": limit_usage_type.value,
            **money_pair("estimated_cost", estimate),
            "credit_reservation_id": credit_reservation_id,
            **_gateway_byok_payload(byok_config, workspace_id, endpoint.provider),
            "content_storage_enabled": False,
            "region": region,
            "regions": region_payload(settings),
            "broadcast_destinations": broadcast_destinations,
            "idempotent_replay": idempotent_replay,
            "request_metadata_version": REQUEST_METADATA_VERSION,
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


def _api_key_for_gateway_lookup(
    *,
    api_key_hash: str | None,
    api_key_lookup_hash: str | None,
) -> Any | None:
    if api_key_hash:
        api_key = STORE.get_key_by_hash(api_key_hash)
        if api_key is not None:
            return api_key
    if api_key_lookup_hash:
        return STORE.get_key_by_lookup_hash(api_key_lookup_hash)
    return None


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


def _force_custom_model_credit_routes(body: dict[str, Any]) -> None:
    prefs = provider_route_preferences(body)
    if prefs.usage_type == UsageType.BYOK:
        raise api_error(
            400,
            "Custom models do not support BYOK routes",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    provider = body.get("provider")
    if isinstance(provider, dict):
        body["provider"] = {**provider, "usage": "credits"}
    else:
        body["provider"] = {"usage": "credits"}


def _settle_gateway_authorization(
    body: GatewaySettleRequest,
    *,
    success: bool,
    settings: Settings,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    timing_start = perf_counter()
    authorization = STORE.get_gateway_authorization(body.authorization_id)
    if authorization is None:
        raise api_error(404, "Gateway authorization not found", ErrorType.NOT_FOUND)
    if authorization.settled:
        # No timing line for replays: they are ~one point-read and would dominate
        # the latency dataset with noise.
        return {
            "data": {
                "authorization_id": authorization.id,
                "settled": False,
                "already_settled": True,
            }
        }

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

    selected_endpoint = _select_authorized_endpoint(authorization, body)
    if selected_endpoint is None:
        raise api_error(
            400,
            "selected endpoint was not authorized for this gateway request",
            ErrorType.BAD_REQUEST,
        )
    model = MODELS.get(selected_endpoint.model_id)
    if model is None:
        raise api_error(500, "Authorized model is no longer configured", ErrorType.INTERNAL_ERROR)
    auth_ms = (perf_counter() - timing_start) * 1000

    output_tokens = body.output_count
    uncached_input, total_input, cache_read, cache_creation = normalized_prompt_accounting(
        selected_endpoint.provider, body
    )
    actual_cost = _endpoint_cost_microdollars(
        selected_endpoint,
        uncached_input,
        output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        effective_at=authorization.created_at,
    )
    input_tokens = total_input
    selected_usage_type = UsageType.for_endpoint(selected_endpoint)

    generation_id: str | None = None
    generation: Generation | None = None
    if success:
        generation = Generation.from_settle_body(
            authorization=authorization,
            provider_name=PROVIDERS[selected_endpoint.provider].name,
            model_id=model.id,
            usage_type=selected_usage_type,
            provider=selected_endpoint.provider,
            body=settle_body,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_microdollars=actual_cost,
        )
        generation_id = generation.id

    # C1 removed the GCP legacy finalize branch; Spanner finalizes through typed
    # billing unconditionally. The memory store still uses the single-book path.
    _typed_store = typed_billing_store(STORE)
    is_typed = _typed_store is not None
    intent_kind = "settle" if success else "refund"
    enqueue_ms = 0.0
    if settings.settle_outbox_enabled:
        enqueue_start = perf_counter()
        try:
            # §5.4 honest scope: durability starts only when this INSERT commits;
            # crashes before it still rely on enclave redelivery. MF4/MF5 freeze
            # the finalize path and exact resolved cost used by the inline attempt.
            spanner_settle_outbox().enqueue(
                SettleOutboxRow(
                    authorization_id=authorization.id,
                    intent_kind=intent_kind,
                    settle_origin="typed" if is_typed else "legacy",
                    actual_cost_micro=actual_cost,
                    reservation_id=authorization.credit_reservation_id,
                    selected_endpoint_id=selected_endpoint.id,
                    model_id=model.id,
                    selected_usage_type=str(selected_usage_type),
                    settle_body=json.dumps(settle_body, separators=(",", ":")),
                ),
                # Grace so inline finalize wins the benign race; the drain only
                # sees rows whose inline attempt is dead >=60s, avoiding replays.
                initial_delay_seconds=60,
            )
        except Exception:
            logger.error(
                "settle outbox enqueue failed authorization_id=%s",
                authorization.id,
                exc_info=True,
            )
        enqueue_ms = (perf_counter() - enqueue_start) * 1000

    finalize_start = perf_counter()
    if is_typed:
        assert _typed_store is not None
        # Typed finalize includes the Bigtable activity-index and benchmark
        # write inside the wrapper's index_after_commit.
        finalized = _typed_store.typed_finalize_gateway_authorization(
            authorization.id,
            success=success,
            actual_microdollars=actual_cost,
            selected_usage_type=selected_usage_type,
            generation=generation,
        )
    else:
        finalized = STORE.finalize_gateway_authorization(
            authorization.id,
            success=success,
            actual_microdollars=actual_cost,
            selected_usage_type=selected_usage_type,
            generation=generation,
        )
    finalize_ms = (perf_counter() - finalize_start) * 1000
    if not finalized:
        # §3/§6/§7: leave the row pending on purpose. Inline's False only says
        # "claim lost"; it cannot distinguish a charged replay from reaper-free
        # lost charge. The drain's apply_frozen_settle outcome disambiguates, and
        # marking done here would silently swallow a lost charge.
        # No timing line for replays: they would dominate the latency dataset
        # with noise instead of measuring full settle/refund work.
        return {
            "data": {
                "authorization_id": authorization.id,
                "settled": False,
                "already_settled": True,
            }
        }
    mark_ms = 0.0
    if settings.settle_outbox_enabled:
        mark_start = perf_counter()
        try:
            marked = spanner_settle_outbox().mark(authorization.id, intent_kind, done=True)
            if marked is None:
                logger.info(
                    "settle outbox done mark skipped authorization_id=%s intent_kind=%s; "
                    "row leased or already resolved; drain will re-derive done",
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

    if success and selected_usage_type == UsageType.CREDITS:
        _schedule_auto_refill(authorization.workspace_id, settings, background_tasks)
    if success:
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
        enqueue_metadata_broadcast(generation, settle_body=settle_body)
        if should_drain_inline(settings) and background_tasks is not None:
            background_tasks.add_task(
                drain_broadcast_queue,
                settings=settings,
            )
        elif should_drain_inline(settings):
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
            "generation_id": generation_id,
            **money_pair("cost", actual_cost),
            "usage_type": selected_usage_type.value,
            "limit_usage_type": authorization.usage_type.value,
            "model": model.id,
            "endpoint_id": selected_endpoint.id,
            "provider": selected_endpoint.provider,
            "region": authorization.region,
        }
    }


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
            "invalid gateway settlement attribution dropped authorization_id=%s "
            "error_class=%s",
            authorization_id,
            type(exc).__name__,
        )
        return settle_body
    for key in attribution_keys:
        settle_body.pop(key, None)
    settle_body.update(attribution.body_fields())
    return settle_body


def _is_synthetic_settlement(body: GatewaySettleRequest) -> bool:
    if body.app == "TrustedRouter Synthetic":
        return True
    metadata = body.metadata
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("trustedrouter_synthetic")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _gateway_candidate_payload(
    model: Model,
    endpoint: ModelEndpoint,
    workspace_id: str,
    region: str,
) -> dict[str, Any]:
    usage_type = UsageType.for_endpoint(endpoint)
    byok_config = (
        STORE.get_byok_provider(workspace_id, endpoint.provider) if usage_type.is_byok() else None
    )
    return {
        "endpoint_id": endpoint.id,
        "model": model.id,
        "upstream_model": endpoint.upstream_id or model.id,
        "provider": endpoint.provider,
        "provider_name": PROVIDERS[endpoint.provider].name,
        "usage_type": usage_type.value,
        **_gateway_byok_payload(byok_config, workspace_id, endpoint.provider),
        "region": region,
    }


def _gateway_byok_payload(
    byok_config: Any | None, workspace_id: str, provider: str
) -> dict[str, Any]:
    if byok_config is None:
        return {
            "byok_secret_ref": None,
            "byok_encrypted_secret": None,
            "byok_cache_key": None,
            "byok_key_hint": None,
        }
    return {
        "byok_secret_ref": byok_config.secret_ref,
        "byok_encrypted_secret": encrypted_secret_payload(byok_config.encrypted_secret),
        "byok_cache_key": byok_cache_key(
            byok_config.encrypted_secret,
            workspace_id=workspace_id,
            provider=provider,
        ),
        "byok_key_hint": byok_config.key_hint,
    }


def _eligible_gateway_endpoint_candidates(
    candidates: list[tuple[Model, ModelEndpoint]],
    workspace_id: str,
) -> list[tuple[Model, ModelEndpoint]]:
    out: list[tuple[Model, ModelEndpoint]] = []
    for model, endpoint in candidates:
        usage_type = UsageType.for_endpoint(endpoint)
        if (
            usage_type.is_byok()
            and STORE.get_byok_provider(workspace_id, endpoint.provider) is None
        ):
            continue
        out.append((model, endpoint))
    return out


def _select_authorized_endpoint(
    authorization: Any, body: GatewaySettleRequest
) -> ModelEndpoint | None:
    authorized_endpoint_ids = authorization.candidate_endpoint_ids or []
    if not authorized_endpoint_ids and authorization.endpoint_id:
        authorized_endpoint_ids = [authorization.endpoint_id]
    selected_endpoint_id = body.selected_endpoint_id
    if selected_endpoint_id is not None:
        if selected_endpoint_id not in authorized_endpoint_ids:
            return None
        return endpoint_for_id(selected_endpoint_id)

    selected_model_id = body.selected_model_id or authorization.model_id
    if selected_model_id == authorization.model_id and authorization.endpoint_id:
        return endpoint_for_id(authorization.endpoint_id)

    for endpoint_id in authorized_endpoint_ids:
        endpoint = endpoint_for_id(endpoint_id)
        if endpoint is not None and endpoint.model_id == selected_model_id:
            return endpoint

    authorized_model_ids = authorization.candidate_model_ids or [authorization.model_id]
    if selected_model_id not in authorized_model_ids:
        return None
    model = MODELS.get(selected_model_id)
    return default_endpoint_for_model(model) if model is not None else None


def _endpoint_cost_microdollars(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    effective_at: datetime | str | None = None,
) -> int:
    """input_tokens must be the UNCACHED prompt tokens when cache counts
    are passed — cached reads/writes bill at the provider-specific
    multiple of the prompt price (see catalog.cache_token_prices_microdollars)."""
    endpoint = effective_endpoint(endpoint, at=effective_at)
    total_prompt = input_tokens + cache_read_tokens + cache_creation_tokens
    rates = resolve_request_rates(
        getattr(endpoint, "price_tiers", ()) or (),
        headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=total_prompt,
    )
    prompt_price = rates.prompt_price_microdollars_per_million_tokens

    cost = token_cost_microdollars(input_tokens, prompt_price) + token_cost_microdollars(
        output_tokens,
        rates.completion_price_microdollars_per_million_tokens,
    )
    has_positive_charge = (input_tokens > 0 and prompt_price > 0) or (
        output_tokens > 0
        and rates.completion_price_microdollars_per_million_tokens > 0
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


def _schedule_auto_refill(
    workspace_id: str,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> None:
    from trusted_router.services.auto_refill import maybe_charge_after_settle

    if background_tasks is not None:
        background_tasks.add_task(maybe_charge_after_settle, workspace_id, settings=settings)
        return
    maybe_charge_after_settle(workspace_id, settings=settings)
