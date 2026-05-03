from __future__ import annotations

import uuid
from typing import Any

import stripe
from fastapi import APIRouter, BackgroundTasks, Request

from trusted_router.auth import SettingsDep, get_authorization_bearer, is_api_key_expired
from trusted_router.catalog import (
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    default_endpoint_for_model,
    endpoint_for_id,
)
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import MICRODOLLARS_PER_CENT, money_pair, token_cost_microdollars
from trusted_router.regions import choose_region, region_payload
from trusted_router.routes.helpers import json_body
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.security import constant_time_equal
from trusted_router.storage import STORE, Generation, ProviderBenchmarkSample
from trusted_router.types import ErrorType, UsageType


def register_internal_routes(router: APIRouter) -> None:
    @router.post("/internal/stripe/webhook")
    async def stripe_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("stripe-signature")
        if settings.stripe_webhook_secret:
            try:
                event = stripe.Webhook.construct_event(raw, sig, settings.stripe_webhook_secret)
            except Exception as exc:
                raise api_error(400, "Invalid Stripe webhook", ErrorType.BAD_REQUEST) from exc
        else:
            event = await json_body(request)
        event_id = str(event.get("id") or uuid.uuid4())
        event_type = event.get("type")
        if event_type == "checkout.session.completed":
            obj = event.get("data", {}).get("object", {})
            workspace_id = obj.get("metadata", {}).get("workspace_id")
            amount_total = int(obj.get("amount_total") or 0)
            customer_id = obj.get("customer")
            if workspace_id and STORE.get_credit_account(workspace_id) is not None:
                if obj.get("mode") == "setup":
                    if isinstance(customer_id, str):
                        STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                    return {"data": {"setup_saved": True, "event_id": event_id}}
                credited = STORE.credit_workspace_once(
                    workspace_id, amount_total * MICRODOLLARS_PER_CENT, event_id
                )
                # Capture the Stripe customer the first time they pay so
                # auto-refill can use it later. The default payment method
                # arrives separately in `setup_intent.succeeded` (or via the
                # PaymentIntent's `payment_method` if Checkout was set up
                # with `setup_future_usage`).
                if isinstance(customer_id, str):
                    STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                return {"data": {"credited": credited, "event_id": event_id}}
        if event_type == "setup_intent.succeeded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            customer_id = obj.get("customer")
            payment_method = obj.get("payment_method")
            if (
                isinstance(workspace_id, str)
                and isinstance(customer_id, str)
                and isinstance(payment_method, str)
                and STORE.get_credit_account(workspace_id) is not None
            ):
                STORE.set_stripe_customer(
                    workspace_id,
                    customer_id=customer_id,
                    payment_method_id=payment_method,
                )
                return {"data": {"setup_saved": True, "event_id": event_id}}
        if event_type == "payment_intent.succeeded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            amount_microdollars_raw = metadata.get("amount_microdollars")
            if (
                metadata.get("auto_refill") == "true"
                and isinstance(workspace_id, str)
                and isinstance(amount_microdollars_raw, str)
            ):
                amount_microdollars = int(amount_microdollars_raw)
                credited = STORE.credit_workspace_once(
                    workspace_id, amount_microdollars, event_id
                )
                STORE.record_auto_refill_outcome(workspace_id, status="succeeded")
                # Also persist the payment-method if Stripe surfaced one —
                # first auto-refill after a Checkout that didn't include
                # setup_future_usage might be the first time we see the PM.
                payment_method = obj.get("payment_method")
                if isinstance(payment_method, str):
                    STORE.set_stripe_customer(
                        workspace_id,
                        customer_id=str(obj.get("customer") or ""),
                        payment_method_id=payment_method,
                    )
                return {"data": {"credited": credited, "event_id": event_id, "auto_refill": True}}
        if event_type == "payment_intent.payment_failed":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            if metadata.get("auto_refill") == "true" and isinstance(workspace_id, str):
                last_error = obj.get("last_payment_error") or {}
                code = last_error.get("code") or "unknown"
                STORE.record_auto_refill_outcome(workspace_id, status=f"failed:{code}")
                return {"data": {"event_id": event_id, "auto_refill_failed": True, "code": code}}
        return {"data": {"ignored": True, "event_id": event_id}}

    @router.post("/internal/gateway/authorize")
    async def gateway_authorize(
        request: Request,
        body: GatewayAuthorizeRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        _require_internal_gateway(request, settings)
        api_key = STORE.get_key_by_hash(body.api_key_hash)
        if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
        workspace = STORE.get_workspace(api_key.workspace_id)
        if workspace is None:
            raise api_error(403, "Workspace is unavailable", ErrorType.FORBIDDEN)
        body_dict = body.model_dump(exclude_none=True)
        requested_model_id = body.model
        endpoint_candidates = chat_route_endpoint_candidates(body_dict, settings)
        if not endpoint_candidates:
            raise api_error(400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED)
        endpoint_candidates = _eligible_gateway_endpoint_candidates(endpoint_candidates, workspace.id)
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
            raise api_error(402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED) from exc

        if has_credit_candidate:
            try:
                credit_reservation = STORE.reserve(workspace.id, api_key.hash, estimate)
                credit_reservation_id = credit_reservation.id
            except ValueError as exc:
                STORE.refund_key_limit(api_key.hash, estimate, usage_type=reservation_usage_type)
                raise api_error(402, "Insufficient credits", ErrorType.INSUFFICIENT_CREDITS) from exc

        authorization = STORE.create_gateway_authorization(
            workspace_id=workspace.id,
            key_hash=api_key.hash,
            model_id=model.id,
            provider=endpoint.provider,
            usage_type=reservation_usage_type,
            estimated_microdollars=estimate,
            credit_reservation_id=credit_reservation_id,
            requested_model_id=requested_model_id,
            candidate_model_ids=[candidate_model.id for candidate_model, _endpoint in endpoint_candidates],
            region=region,
            endpoint_id=endpoint.id,
            candidate_endpoint_ids=[
                candidate_endpoint.id for _candidate_model, candidate_endpoint in endpoint_candidates
            ],
        )
        byok_config = (
            STORE.get_byok_provider(workspace.id, endpoint.provider)
            if model_usage_type.is_byok()
            else None
        )
        return {
            "data": {
                "authorization_id": authorization.id,
                "workspace_id": workspace.id,
                "api_key_hash": api_key.hash,
                "model": model.id,
                "endpoint_id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "requested_model": requested_model_id,
                "usage_type": model_usage_type.value,
                "limit_usage_type": reservation_usage_type.value,
                **money_pair("estimated_cost", estimate),
                "credit_reservation_id": credit_reservation_id,
                "byok_secret_ref": byok_config.secret_ref if byok_config else None,
                "byok_key_hint": byok_config.key_hint if byok_config else None,
                "content_storage_enabled": False,
                "region": region,
                "regions": region_payload(settings),
                "route_candidates": [
                    _gateway_candidate_payload(candidate_model, candidate_endpoint, workspace.id, region)
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
        _require_internal_gateway(request, settings)
        return _settle_gateway_authorization(
            body, success=True, settings=settings, background_tasks=background_tasks,
        )

    @router.post("/internal/gateway/refund")
    async def gateway_refund(
        request: Request,
        body: GatewaySettleRequest,
        settings: SettingsDep,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        _require_internal_gateway(request, settings)
        return _settle_gateway_authorization(
            body, success=False, settings=settings, background_tasks=background_tasks,
        )

    @router.get("/internal/sentry-test")
    async def sentry_test(request: Request, settings: SettingsDep) -> None:
        if settings.environment.lower() not in {"local", "test"} and not settings.enable_sentry_test_route:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        _require_internal_gateway(request, settings)
        raise RuntimeError("synthetic sentry test")


def _require_internal_gateway(request: Request, settings: Settings) -> None:
    if settings.internal_gateway_token:
        supplied = get_authorization_bearer(request) or request.headers.get("x-trustedrouter-internal-token") or ""
        if not constant_time_equal(supplied, settings.internal_gateway_token):
            raise api_error(401, "Invalid internal gateway token", ErrorType.UNAUTHORIZED)
        return
    if settings.environment not in {"local", "test"}:
        raise api_error(403, "Internal gateway token is required", ErrorType.FORBIDDEN)


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
        return {"data": {"authorization_id": authorization.id, "settled": False, "already_settled": True}}

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
        return {"data": {"authorization_id": authorization.id, "settled": False, "already_settled": True}}

    if success and selected_usage_type == UsageType.CREDITS:
        _schedule_auto_refill(authorization.workspace_id, settings, background_tasks)
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
    byok_config = STORE.get_byok_provider(workspace_id, endpoint.provider) if usage_type.is_byok() else None
    return {
        "endpoint_id": endpoint.id,
        "model": model.id,
        "provider": endpoint.provider,
        "provider_name": PROVIDERS[endpoint.provider].name,
        "usage_type": usage_type.value,
        "byok_secret_ref": byok_config.secret_ref if byok_config else None,
        "byok_key_hint": byok_config.key_hint if byok_config else None,
        "region": region,
    }


def _eligible_gateway_endpoint_candidates(
    candidates: list[tuple[Model, ModelEndpoint]],
    workspace_id: str,
) -> list[tuple[Model, ModelEndpoint]]:
    out: list[tuple[Model, ModelEndpoint]] = []
    for model, endpoint in candidates:
        usage_type = UsageType.for_endpoint(endpoint)
        if usage_type.is_byok() and STORE.get_byok_provider(workspace_id, endpoint.provider) is None:
            continue
        out.append((model, endpoint))
    return out


def _select_authorized_endpoint(authorization: Any, body: GatewaySettleRequest) -> ModelEndpoint | None:
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
    return (
        token_cost_microdollars(input_tokens, endpoint.prompt_price_microdollars_per_million_tokens)
        + token_cost_microdollars(
            output_tokens,
            endpoint.completion_price_microdollars_per_million_tokens,
        )
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
