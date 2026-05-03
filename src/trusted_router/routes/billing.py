from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.errors import api_error, deprecated
from trusted_router.money import money_pair
from trusted_router.routes.helpers import json_body
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_billing import (
    create_billing_portal_session,
    create_checkout_session,
    create_payment_method_session,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


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
                "data": create_checkout_session(
                    body=body,
                    workspace_id=workspace_id,
                    customer_email=_checkout_customer_email(principal),
                    settings=settings,
                )
            },
            status_code=201,
        )

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
