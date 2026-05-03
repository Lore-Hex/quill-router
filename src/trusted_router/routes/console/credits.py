"""/console/credits — credit balance, Stripe checkout, payment-method
setup + management, and auto-refill toggle.

The five POST handlers cover the full Stripe integration surface from
the console UI; each delegates to services/stripe_billing for the
actual API calls so this module stays focused on form parsing + redirects."""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_billing import (
    create_billing_portal_session,
    create_checkout_session,
    create_payment_method_session,
)
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/credits")
    async def console_credits(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        return HTMLResponse(render(
            "console/credits.html",
            settings=settings,
            user=ctx.user,
            active="credits",
            page_title="Credits",
            page_subtitle="Top up to keep prepaid routes flowing.",
            credits_available=money(
                (credit.total_credits_microdollars - credit.total_usage_microdollars - credit.reserved_microdollars)
                if credit else 0
            ),
            credits_usage=money(credit.total_usage_microdollars if credit else 0),
            auto_refill_enabled=credit.auto_refill_enabled if credit else False,
            auto_refill_threshold_dollars=(
                credit.auto_refill_threshold_microdollars // 1_000_000 if credit and credit.auto_refill_threshold_microdollars else 10
            ),
            auto_refill_amount_dollars=(
                credit.auto_refill_amount_microdollars // 1_000_000 if credit and credit.auto_refill_amount_microdollars else 25
            ),
            has_payment_method=bool(
                credit and credit.stripe_customer_id and credit.stripe_payment_method_id
            ),
            has_stripe_customer=bool(credit and credit.stripe_customer_id),
            payment_method_pending=bool(
                credit and credit.stripe_customer_id and not credit.stripe_payment_method_id
            ),
            last_auto_refill_at=credit.last_auto_refill_at if credit else None,
            last_auto_refill_status=credit.last_auto_refill_status if credit else None,
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/credits/checkout")
    async def console_credit_checkout_get(_ctx: ConsoleDep) -> Response:
        return RedirectResponse(url="/console/credits", status_code=302)

    @app.post("/console/credits/checkout")
    async def console_credit_checkout(
        ctx: ConsoleDep,
        settings: SettingsDep,
        amount: str = Form(...),
        payment_method: str = Form("auto"),
    ) -> Response:
        try:
            # CheckoutRequest validates payment_method against the Literal
            # set; the cast just tells mypy that the form value will be
            # checked at construction time.
            body = CheckoutRequest(
                amount=amount,
                workspace_id=ctx.workspace.id,
                payment_method=cast(Any, payment_method),
                success_url=f"https://{settings.trusted_domain}/console/credits?checkout=success",
                cancel_url=f"https://{settings.trusted_domain}/console/credits?checkout=cancel",
            )
        except ValidationError:
            return RedirectResponse(url="/console/credits?error=invalid_checkout", status_code=303)
        try:
            data = create_checkout_session(
                body=body,
                workspace_id=ctx.workspace.id,
                customer_email=ctx.user.email if ctx.user.email and "@" in ctx.user.email else None,
                settings=settings,
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=checkout_unavailable", status_code=303)
        if str(data.get("mode", "")).startswith("mock"):
            return RedirectResponse(url="/console/credits?checkout=mock", status_code=303)
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @app.post("/console/credits/payment-methods/add")
    async def console_add_payment_method(
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        try:
            data = create_payment_method_session(
                workspace_id=ctx.workspace.id,
                customer_email=ctx.user.email if ctx.user.email and "@" in ctx.user.email else None,
                customer_id=credit.stripe_customer_id if credit else None,
                success_url=f"https://{settings.trusted_domain}/console/credits?payment_method=success",
                cancel_url=f"https://{settings.trusted_domain}/console/credits?payment_method=cancel",
                settings=settings,
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=payment_method_unavailable", status_code=303)
        if str(data.get("mode", "")).startswith("mock"):
            return RedirectResponse(url="/console/credits?payment_method=mock", status_code=303)
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @app.post("/console/credits/payment-methods/manage")
    async def console_manage_payment_methods(
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        if not (credit and credit.stripe_customer_id):
            return RedirectResponse(url="/console/credits?error=no_payment_method", status_code=303)
        data = create_billing_portal_session(
            customer_id=credit.stripe_customer_id,
            return_url=f"https://{settings.trusted_domain}/console/credits",
            settings=settings,
        )
        if data["mode"] == "mock":
            return RedirectResponse(url="/console/credits?payment_method=mock-portal", status_code=303)
        return RedirectResponse(url=data["url"], status_code=303)

    @app.post("/console/credits/auto-refill")
    async def console_save_auto_refill(
        ctx: ConsoleDep,
        settings: SettingsDep,
        enabled: str = Form(""),
        threshold: int = Form(..., ge=10, le=500),
        amount: int = Form(..., ge=5, le=2000),
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        # Reject the enable toggle if there's no saved payment method —
        # otherwise the trigger fires every settle and silently fails.
        truly_enable = enabled == "1"
        if truly_enable and not (credit and credit.stripe_customer_id and credit.stripe_payment_method_id):
            return RedirectResponse(url="/console/credits?error=no_payment_method", status_code=303)
        STORE.update_auto_refill_settings(
            ctx.workspace.id,
            enabled=truly_enable,
            threshold_microdollars=threshold * 1_000_000,
            amount_microdollars=amount * 1_000_000,
        )
        return RedirectResponse(url="/console/credits?saved=1", status_code=303)
