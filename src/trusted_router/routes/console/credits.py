"""/console/credits — credit balance, Stripe checkout, payment-method
setup + management, and auto-refill toggle.

The five POST handlers cover the full Stripe integration surface from
the console UI; each delegates to services/stripe_billing for the
actual API calls so this module stays focused on form parsing + redirects."""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from trusted_router.auth import SettingsDep
from trusted_router.billing_policy import (
    is_stablecoin_checkout_method,
    is_wallet_only_user,
)
from trusted_router.domains import request_control_origin
from trusted_router.money import (
    MIN_PAYPAL_CHECKOUT_DOLLARS,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
    money_pair,
)
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.adyen_billing import (
    ADYEN_WEB_CSS_SRI,
    ADYEN_WEB_JS_SRI,
    adyen_web_asset_urls,
    create_adyen_checkout_session,
)
from trusted_router.services.paypal_billing import (
    capture_paypal_order_for_workspace,
    create_paypal_checkout_session,
)
from trusted_router.services.stripe_billing import (
    create_billing_portal_session,
    create_checkout_session,
    create_payment_method_session,
    describe_saved_payment_method,
    list_workspace_payments,
    remove_saved_payment_method,
)
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary


def register(app: FastAPI) -> None:
    @app.get("/console/credits")
    async def console_credits(
        ctx: ConsoleDep,
        settings: SettingsDep,
        purpose: str = "",
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        summary = live_credit_summary(ctx.workspace.id)
        wallet_only_billing = is_wallet_only_user(ctx.user)
        # Pull the last 20 Stripe checkout sessions tagged with this
        # workspace_id from Stripe's Search API. Returns [] if Stripe is
        # unreachable / not configured / there are no payments yet — all
        # three collapse to the same "no payment history yet" copy on
        # the template, so the rest of the page renders fine without
        # being blocked on Stripe's API. See list_workspace_payments
        # docstring for why we pull live instead of reading from a TR
        # ledger (tr_entities doesn't store per-payment metadata today).
        payments = list_workspace_payments(
            workspace_id=ctx.workspace.id,
            settings=settings,
            limit=20,
        )
        saved_payment_method = describe_saved_payment_method(
            payment_method_id=credit.stripe_payment_method_id if credit else None,
            settings=settings,
        )
        verification_remaining = max(
            0,
            VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
            - STORE.get_lifetime_topup_microdollars(ctx.user.id),
        )
        verification_nudge = (
            {
                **money_pair("verification_topup_remaining", verification_remaining),
                "verification_topup_remaining_display": money(verification_remaining),
            }
            if purpose == "identity_verification"
            else {}
        )
        return HTMLResponse(render(
            "console/credits.html",
            settings=settings,
            ctx=ctx,
            active="credits",
            page_title="Credits",
            page_subtitle="Top up to keep prepaid routes flowing.",
            credits_available=money(summary["available"] if summary else 0),
            credits_usage=money(summary["total_usage"] if summary else 0),
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
            paypal_enabled=settings.paypal_enabled or settings.environment.lower() in {"local", "test"},
            paypal_minimum_dollars=MIN_PAYPAL_CHECKOUT_DOLLARS,
            adyen_enabled=settings.adyen_checkout_ready,
            wallet_only_billing=wallet_only_billing,
            payments=payments,
            saved_payment_method=saved_payment_method,
            api_base_url=ctx.api_base_url,
            identity_verification_checkout=purpose == "identity_verification",
            **verification_nudge,
        ))

    @app.get("/console/credits/checkout")
    async def console_credit_checkout_get(_ctx: ConsoleDep) -> Response:
        return RedirectResponse(url="/console/credits", status_code=302)

    @app.post("/console/credits/checkout")
    async def console_credit_checkout(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
        amount: str = Form(...),
        payment_method: str = Form("auto"),
        purpose: str = Form(""),
    ) -> Response:
        if is_wallet_only_user(ctx.user) and not is_stablecoin_checkout_method(
            payment_method
        ):
            return _wallet_only_redirect()
        origin = request_control_origin(request, settings)
        if payment_method == "paypal":
            success_url = f"{origin}/console/credits/paypal/capture"
        elif payment_method == "adyen":
            success_url = f"{origin}/console/credits?checkout=processing"
        else:
            success_url = f"{origin}/console/credits?checkout=success"
        try:
            # CheckoutRequest validates payment_method against the Literal
            # set; the cast just tells mypy that the form value will be
            # checked at construction time.
            body = CheckoutRequest(
                amount=amount,
                workspace_id=ctx.workspace.id,
                payment_method=cast(Any, payment_method),
                purpose=cast(Any, purpose or None),
                success_url=success_url,
                cancel_url=f"{origin}/console/credits?checkout=cancel",
            )
        except ValidationError:
            return RedirectResponse(url="/console/credits?error=invalid_checkout", status_code=303)
        credit = STORE.get_credit_account(ctx.workspace.id)
        try:
            data = (
                create_adyen_checkout_session(
                    body=body,
                    workspace_id=ctx.workspace.id,
                    customer_email=(
                        ctx.user.email if ctx.user.email and "@" in ctx.user.email else None
                    ),
                    settings=settings,
                )
                if body.payment_method == "adyen"
                else create_paypal_checkout_session(
                    body=body,
                    workspace_id=ctx.workspace.id,
                    initiating_user_id=ctx.user.id,
                    customer_email=ctx.user.email if ctx.user.email and "@" in ctx.user.email else None,
                    settings=settings,
                )
                if body.payment_method == "paypal"
                else create_checkout_session(
                    body=body,
                    workspace_id=ctx.workspace.id,
                    initiating_user_id=ctx.user.id,
                    customer_email=ctx.user.email if ctx.user.email and "@" in ctx.user.email else None,
                    customer_id=credit.stripe_customer_id if credit else None,
                    settings=settings,
                )
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=checkout_unavailable", status_code=303)
        if body.purpose == "identity_verification":
            data.update(
                money_pair(
                    "verification_topup_remaining",
                    max(
                        0,
                        VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
                        - STORE.get_lifetime_topup_microdollars(ctx.user.id),
                    ),
                )
            )
        if str(data.get("mode", "")).startswith("mock"):
            suffix = "&purpose=identity_verification" if body.purpose else ""
            return RedirectResponse(url=f"/console/credits?checkout=mock{suffix}", status_code=303)
        if data.get("mode") == "adyen":
            adyen_js_url, adyen_css_url = adyen_web_asset_urls(settings)
            checkout_config = {
                "clientKey": data["client_key"],
                "environment": data["environment"],
                "session": {"id": data["id"], "sessionData": data["session_data"]},
                "successUrl": success_url,
                "cancelUrl": f"{origin}/console/credits?checkout=cancel",
            }
            return HTMLResponse(
                render(
                    "console/adyen_checkout.html",
                    settings=settings,
                    ctx=ctx,
                    active="credits",
                    page_title="Adyen checkout",
                    page_subtitle="Add prepaid credits with Adyen.",
                    checkout_config=checkout_config,
                    adyen_js_url=adyen_js_url,
                    adyen_css_url=adyen_css_url,
                    adyen_js_sri=ADYEN_WEB_JS_SRI,
                    adyen_css_sri=ADYEN_WEB_CSS_SRI,
                    amount_display=money(int(data["amount_microdollars"])),
                    processing_fee_display=money(
                        int(data["processing_fee_microdollars"])
                    ),
                    total_display=money(int(data["total_microdollars"])),
                    api_base_url=ctx.api_base_url,
                )
            )
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @app.get("/console/credits/paypal/capture")
    async def console_paypal_capture(
        ctx: ConsoleDep,
        settings: SettingsDep,
        token: str = "",
    ) -> Response:
        if is_wallet_only_user(ctx.user):
            return _wallet_only_redirect()
        if not token:
            return RedirectResponse(url="/console/credits?error=paypal_missing_order", status_code=303)
        try:
            result = capture_paypal_order_for_workspace(
                order_id=token,
                workspace_id=ctx.workspace.id,
                settings=settings,
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=paypal_capture_failed", status_code=303)
        suffix = "paypal=credited" if result.credited else "paypal=duplicate"
        return RedirectResponse(url=f"/console/credits?checkout=success&{suffix}", status_code=303)

    @app.post("/console/credits/payment-methods/add")
    async def console_add_payment_method(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        if is_wallet_only_user(ctx.user):
            return _wallet_only_redirect()
        credit = STORE.get_credit_account(ctx.workspace.id)
        origin = request_control_origin(request, settings)
        try:
            data = create_payment_method_session(
                workspace_id=ctx.workspace.id,
                customer_email=ctx.user.email if ctx.user.email and "@" in ctx.user.email else None,
                customer_id=credit.stripe_customer_id if credit else None,
                success_url=f"{origin}/console/credits?payment_method=success",
                cancel_url=f"{origin}/console/credits?payment_method=cancel",
                settings=settings,
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=payment_method_unavailable", status_code=303)
        if str(data.get("mode", "")).startswith("mock"):
            return RedirectResponse(url="/console/credits?payment_method=mock", status_code=303)
        return RedirectResponse(url=str(data["url"]), status_code=303)

    @app.post("/console/credits/payment-methods/manage")
    async def console_manage_payment_methods(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        if is_wallet_only_user(ctx.user):
            return _wallet_only_redirect()
        credit = STORE.get_credit_account(ctx.workspace.id)
        if not (credit and credit.stripe_customer_id):
            return RedirectResponse(url="/console/credits?error=no_payment_method", status_code=303)
        data = create_billing_portal_session(
            customer_id=credit.stripe_customer_id,
            return_url=f"{request_control_origin(request, settings)}/console/credits",
            settings=settings,
        )
        if data["mode"] == "mock":
            return RedirectResponse(url="/console/credits?payment_method=mock-portal", status_code=303)
        return RedirectResponse(url=data["url"], status_code=303)

    @app.post("/console/credits/payment-methods/remove")
    async def console_remove_payment_method(
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        try:
            result = remove_saved_payment_method(
                workspace_id=ctx.workspace.id,
                settings=settings,
            )
        except HTTPException:
            return RedirectResponse(url="/console/credits?error=payment_method_remove_failed", status_code=303)
        suffix = "removed" if result.get("removed") else "none"
        return RedirectResponse(url=f"/console/credits?payment_method={suffix}", status_code=303)

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
        if truly_enable and is_wallet_only_user(ctx.user):
            return _wallet_only_redirect()
        if truly_enable and not (credit and credit.stripe_customer_id and credit.stripe_payment_method_id):
            return RedirectResponse(url="/console/credits?error=no_payment_method", status_code=303)
        STORE.update_auto_refill_settings(
            ctx.workspace.id,
            enabled=truly_enable,
            threshold_microdollars=threshold * 1_000_000,
            amount_microdollars=amount * 1_000_000,
        )
        return RedirectResponse(url="/console/credits?saved=1", status_code=303)


def _wallet_only_redirect() -> RedirectResponse:
    return RedirectResponse(
        url="/console/credits?error=stablecoin_only",
        status_code=303,
    )
