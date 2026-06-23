from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from trusted_router.auth import ManagementPrincipal, SettingsDep, principal_from_request
from trusted_router.errors import api_error, deprecated
from trusted_router.money import money_pair
from trusted_router.routes.helpers import json_body
from trusted_router.schemas import CheckoutRequest, X402FundingRequest, X402SettleRequest
from trusted_router.services.paypal_billing import (
    capture_paypal_order_for_workspace,
    create_paypal_checkout_session,
)
from trusted_router.services.stripe_billing import (
    create_billing_portal_session,
    create_checkout_session,
    create_payment_method_session,
)
from trusted_router.services.x402_billing import (
    X402_HEADER,
    create_x402_funding_challenge,
    settle_x402_payment,
    validate_x402_funding_amount,
    x402_payment_required_response_body,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_billing_routes(router: APIRouter) -> None:
    @router.get("/credits")
    async def credits(principal: ManagementPrincipal) -> dict[str, dict[str, Any]]:
        account = STORE.get_credit_account(principal.workspace.id)
        if account is None:
            raise api_error(404, "Credit account not found", ErrorType.NOT_FOUND)
        available_microdollars = (
            account.total_credits_microdollars
            - account.total_usage_microdollars
            - account.reserved_microdollars
        )
        return {
            "data": {
                **money_pair("total_credits", account.total_credits_microdollars),
                **money_pair("total_usage", account.total_usage_microdollars),
                **money_pair("reserved", account.reserved_microdollars),
                **money_pair("available", available_microdollars),
            }
        }

    @router.post("/billing/checkout")
    async def billing_checkout(
        body: CheckoutRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        workspace_id = body.workspace_id or principal.workspace.id
        if workspace_id != principal.workspace.id:
            raise api_error(403, "Forbidden", ErrorType.FORBIDDEN)
        return JSONResponse(
            {
                "data": (
                    create_paypal_checkout_session(
                        body=body,
                        workspace_id=workspace_id,
                        customer_email=_checkout_customer_email(principal),
                        settings=settings,
                    )
                    if body.payment_method == "paypal"
                    else create_checkout_session(
                        body=body,
                        workspace_id=workspace_id,
                        customer_email=_checkout_customer_email(principal),
                        settings=settings,
                    )
                )
            },
            status_code=201,
        )

    @router.post("/billing/paypal/orders/{order_id}/capture")
    async def billing_paypal_capture(
        order_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, dict[str, Any]]:
        result = capture_paypal_order_for_workspace(
            order_id=order_id,
            workspace_id=principal.workspace.id,
            settings=settings,
        )
        return {
            "data": {
                "order_id": result.order_id or order_id,
                "capture_id": result.capture_id,
                "workspace_id": result.workspace_id,
                "credited": result.credited,
                "status": result.status,
                **money_pair("amount", result.amount_microdollars),
            }
        }

    @router.post("/billing/x402/fund")
    async def billing_x402_fund(
        request: Request,
        settings: SettingsDep,
    ) -> JSONResponse:
        if not settings.x402_enabled:
            raise api_error(404, "route not found", ErrorType.NOT_FOUND)
        principal = principal_from_request(request, settings)
        if principal.api_key is None:
            raise api_error(403, "An API key is required for x402 funding", ErrorType.FORBIDDEN)
        body = _validated_x402_body(X402FundingRequest, await json_body(request))
        validate_x402_funding_amount(body.amount, settings)
        _enforce_x402_rate_limit(
            namespace="x402_fund_key",
            subject=principal.api_key.hash,
            limit=settings.x402_rate_limit_key_per_window,
            settings=settings,
        )
        _enforce_x402_rate_limit(
            namespace="x402_fund_workspace",
            subject=principal.workspace.id,
            limit=settings.x402_rate_limit_workspace_per_window,
            settings=settings,
        )
        challenge = create_x402_funding_challenge(
            body=body,
            workspace_id=principal.workspace.id,
            settings=settings,
        )
        return JSONResponse(
            x402_payment_required_response_body(challenge),
            status_code=402,
            headers={X402_HEADER: str(challenge["payment_required_header"])},
        )

    @router.post("/billing/x402/settle")
    async def billing_x402_settle(
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, dict[str, Any]]:
        if not settings.x402_enabled:
            raise api_error(404, "route not found", ErrorType.NOT_FOUND)
        principal = principal_from_request(request, settings)
        if principal.api_key is None:
            raise api_error(403, "An API key is required for x402 settlement", ErrorType.FORBIDDEN)
        body = _validated_x402_body(X402SettleRequest, await json_body(request))
        _enforce_x402_rate_limit(
            namespace="x402_settle_workspace",
            subject=principal.workspace.id,
            limit=settings.x402_settle_workspace_per_window,
            settings=settings,
        )
        _enforce_x402_rate_limit(
            namespace="x402_settle",
            subject=f"{principal.workspace.id}:{body.payment_intent_id}",
            limit=settings.x402_settle_rate_limit_per_window,
            settings=settings,
        )
        result = settle_x402_payment(
            payment_intent_id=body.payment_intent_id,
            workspace_id=principal.workspace.id,
            settings=settings,
        )
        return {"data": result}

    @router.post("/billing/portal")
    async def billing_portal(
        request: Request,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, dict[str, str]]:
        body = await json_body(request)
        return_url = str(body.get("return_url") or f"https://{settings.trusted_domain}/billing")
        account = STORE.get_credit_account(principal.workspace.id)
        customer_id = account.stripe_customer_id if account else None
        return {"data": create_billing_portal_session(customer_id=customer_id, return_url=return_url, settings=settings)}

    @router.post("/billing/payment-methods/setup")
    async def billing_payment_method_setup(
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        account = STORE.get_credit_account(principal.workspace.id)
        return JSONResponse(
            {
                "data": create_payment_method_session(
                    workspace_id=principal.workspace.id,
                    customer_email=_checkout_customer_email(principal),
                    customer_id=account.stripe_customer_id if account else None,
                    success_url=f"https://{settings.trusted_domain}/billing/payment-methods/success",
                    cancel_url=f"https://{settings.trusted_domain}/billing/payment-methods",
                    settings=settings,
                )
            },
            status_code=201,
        )

    @router.post("/credits/coinbase")
    async def credits_coinbase() -> JSONResponse:
        return deprecated()


def _checkout_customer_email(principal: Any) -> str | None:
    if principal.user is not None and principal.user.email and "@" in principal.user.email:
        return principal.user.email
    if principal.api_key is not None and principal.api_key.creator_user_id:
        user = STORE.get_user(principal.api_key.creator_user_id)
        if user is not None and user.email and "@" in user.email:
            return user.email
    return None


def _validated_x402_body(model: Any, body: dict[str, Any]) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        first = cast(dict[str, Any], exc.errors()[0]) if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", []) if part != "body") or "body"
        message = first.get("msg") or "Invalid request body"
        raise api_error(400, f"{loc}: {message}", ErrorType.BAD_REQUEST) from exc


def _enforce_x402_rate_limit(
    *,
    namespace: str,
    subject: str,
    limit: int,
    settings: Any,
) -> None:
    if limit <= 0:
        return
    try:
        hit = STORE.hit_rate_limit(
            namespace=namespace,
            subject=subject,
            limit=limit,
            window_seconds=settings.x402_rate_limit_window_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - payment creation must fail closed.
        log.exception(
            "x402.rate_limit_unavailable",
            extra={"namespace": namespace, "subject": subject},
        )
        raise api_error(503, "x402 rate limiter is unavailable", ErrorType.SERVICE_UNAVAILABLE) from exc
    if not hit.allowed:
        raise api_error(429, "x402 funding rate limit exceeded", ErrorType.RATE_LIMITED)
