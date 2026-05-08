"""/internal/gateway/{authorize,settle,refund} — the cross-request
reservation handle the attested gateway uses.

Authorize reserves credits + per-key spend cap and returns an
authorization id. Settle and refund land on a separate request from
the authorize call (the enclave settles after streaming finishes).
The reservation is one-shot: a second settle on the same authorization
returns already_settled=True without double-charging.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from trusted_router.auth import SettingsDep, is_api_key_expired
from trusted_router.byok_crypto import byok_cache_key, encrypted_secret_payload
from trusted_router.catalog import (
    MODELS,
    MONITOR_MODEL_ID,
    PROVIDERS,
    Model,
    ModelEndpoint,
    default_endpoint_for_model,
    endpoint_for_id,
)
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import money_pair, token_cost_microdollars
from trusted_router.regions import choose_region, region_payload
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.schemas import (
    GatewayAuthorizeRequest,
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
from trusted_router.storage import STORE, Generation, ProviderBenchmarkSample
from trusted_router.types import ErrorType, UsageType


def register(router: APIRouter) -> None:
    @router.post("/internal/gateway/validate")
    async def gateway_validate(
        request: Request,
        body: GatewayValidateRequest,
        settings: SettingsDep,
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
        return {
            "data": {
                "workspace_id": workspace.id,
                "api_key_hash": api_key.hash,
                "route_type": body.route_type,
            }
        }

    @router.post("/internal/gateway/authorize")
    async def gateway_authorize(
        request: Request,
        body: GatewayAuthorizeRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        api_key = _api_key_for_gateway_authorization(body)
        if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
        workspace = STORE.get_workspace(api_key.workspace_id)
        if workspace is None:
            raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
        body_dict = body.model_dump(exclude_none=True)
        _require_monitor_model_key(body_dict, api_key.lookup_hash, settings)
        requested_model_id = body.model
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
        credit_reservation_id: str | None = None
        try:
            STORE.reserve_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
        except ValueError as exc:
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc

        if has_credit_candidate:
            try:
                credit_reservation = STORE.reserve(workspace.id, api_key.hash, estimate)
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
        return {
            "data": {
                "authorization_id": authorization.id,
                "workspace_id": workspace.id,
                "api_key_hash": api_key.hash,
                "model": model.id,
                "upstream_model": endpoint.upstream_id or model.id,
                "endpoint_id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "requested_model": requested_model_id,
                "usage_type": model_usage_type.value,
                "limit_usage_type": reservation_usage_type.value,
                **money_pair("estimated_cost", estimate),
                "credit_reservation_id": credit_reservation_id,
                **_gateway_byok_payload(byok_config, workspace.id, endpoint.provider),
                "content_storage_enabled": False,
                "region": region,
                "regions": region_payload(settings),
                "broadcast_destinations": broadcast_destinations,
                "route_candidates": [
                    _gateway_candidate_payload(
                        candidate_model, candidate_endpoint, workspace.id, region
                    )
                    for candidate_model, candidate_endpoint in endpoint_candidates
                ],
            }
        }

    @router.post("/internal/gateway/settle")
    async def gateway_settle(
        request: Request,
        body: GatewaySettleRequest,
        settings: SettingsDep,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        return _settle_gateway_authorization(
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
        return _settle_gateway_authorization(
            body,
            success=False,
            settings=settings,
            background_tasks=background_tasks,
        )


def _api_key_for_gateway_authorization(body: GatewayAuthorizeRequest) -> Any | None:
    return _api_key_for_gateway_lookup(
        api_key_hash=body.api_key_hash,
        api_key_lookup_hash=body.api_key_lookup_hash,
    )


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


def _settle_gateway_authorization(
    body: GatewaySettleRequest,
    *,
    success: bool,
    settings: Settings,
    background_tasks: BackgroundTasks | None = None,
) -> dict[str, Any]:
    authorization = STORE.get_gateway_authorization(body.authorization_id)
    if authorization is None:
        raise api_error(404, "Gateway authorization not found", ErrorType.NOT_FOUND)
    if authorization.settled:
        return {
            "data": {
                "authorization_id": authorization.id,
                "settled": False,
                "already_settled": True,
            }
        }

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

    input_tokens = body.input_count
    output_tokens = body.output_count
    actual_cost = _endpoint_cost_microdollars(selected_endpoint, input_tokens, output_tokens)
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
            body=body.model_dump(exclude_none=True),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_cost_microdollars=actual_cost,
        )
        generation_id = generation.id

    finalized = STORE.finalize_gateway_authorization(
        authorization.id,
        success=success,
        actual_microdollars=actual_cost,
        selected_usage_type=selected_usage_type,
        generation=generation,
    )
    if not finalized:
        return {
            "data": {
                "authorization_id": authorization.id,
                "settled": False,
                "already_settled": True,
            }
        }

    if success and selected_usage_type == UsageType.CREDITS:
        _schedule_auto_refill(authorization.workspace_id, settings, background_tasks)
    if success and generation is not None:
        settle_body = body.model_dump(exclude_none=True)
        enqueue_metadata_broadcast(generation, settle_body=settle_body)
        if should_drain_inline(settings) and background_tasks is not None:
            background_tasks.add_task(
                drain_broadcast_queue,
                settings=settings,
            )
        elif should_drain_inline(settings):
            drain_broadcast_queue(settings=settings)
    if not success:
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
) -> int:
    return token_cost_microdollars(
        input_tokens, endpoint.prompt_price_microdollars_per_million_tokens
    ) + token_cost_microdollars(
        output_tokens,
        endpoint.completion_price_microdollars_per_million_tokens,
    )


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
