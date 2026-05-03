"""Server-rendered console pages.

Each page is a separate URL with its own GET (and optional POST) handler.
The pages share `_layout.html` for the topbar + sidebar; the body comes
from each per-page template.

Auth: every console route requires an active session cookie. Without
one, we 302 to `/?reason=signin` so the marketing page can auto-open the
sign-in modal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    SettingsDep,
)
from trusted_router.catalog import PROVIDERS
from trusted_router.config import Settings
from trusted_router.money import format_money_display, format_money_precise
from trusted_router.regions import configured_regions, region_payload
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_billing import (
    create_billing_portal_session,
    create_checkout_session,
    create_payment_method_session,
)
from trusted_router.storage import (
    STORE,
    ApiKey,
    AuthSession,
    User,
    Workspace,
)
from trusted_router.views import render_template


@dataclass(frozen=True)
class ConsoleContext:
    """Resolved per-request identity for console pages. The session must be
    `state="active"`; pending wallet sessions don't see the console."""

    user: User
    session: AuthSession
    workspace: Workspace
    workspaces: list[Workspace]


def require_console_context(request: Request) -> ConsoleContext:
    """FastAPI dependency. Resolves the active console session or raises a
    302 redirect to the marketing page so it can pop the sign-in modal."""
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    session = STORE.get_auth_session_by_raw(cookie_token) if cookie_token else None
    if session is None or session.state != "active":
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    user = STORE.get_user(session.user_id)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspaces = STORE.list_workspaces_for_user(user.id)
    if not workspaces:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspace = _selected_console_workspace(session, workspaces)
    return ConsoleContext(user=user, session=session, workspace=workspace, workspaces=workspaces)


ConsoleDep = Annotated[ConsoleContext, Depends(require_console_context)]


def register_console_routes(app: FastAPI) -> None:
    @app.get("/console")
    async def console_root() -> Response:
        return RedirectResponse(url="/console/api-keys", status_code=302)

    @app.post("/console/workspaces/select")
    async def console_select_workspace(
        request: Request,
        ctx: ConsoleDep,
        workspace_id: str = Form(..., min_length=1, max_length=128),
        next_path: str = Form("/console/api-keys", alias="next"),
    ) -> Response:
        if not any(workspace.id == workspace_id for workspace in ctx.workspaces):
            return RedirectResponse(url="/console/settings?error=workspace", status_code=303)
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_token:
            STORE.set_auth_session_workspace(cookie_token, workspace_id)
        return RedirectResponse(url=_safe_console_next(next_path), status_code=303)

    @app.get("/console/welcome")
    async def console_welcome(
        ctx: ConsoleDep,
        settings: SettingsDep,
        first: int | None = None,
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        return HTMLResponse(_render(
            "console/welcome.html",
            settings=settings,
            user=ctx.user,
            active="api-keys",
            page_title="Welcome",
            page_subtitle="Save your API key — it won't be shown again.",
            revealed_key=None if first is None else _reveal_first_key(ctx.workspace),
            workspace_name=ctx.workspace.name,
            trial_credit=_money(credit.total_credits_microdollars if credit else 0),
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/api-keys")
    async def console_api_keys(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        keys = [_key_view(k) for k in STORE.list_keys(ctx.workspace.id)]
        return HTMLResponse(_render(
            "console/api_keys.html",
            settings=settings,
            user=ctx.user,
            active="api-keys",
            page_title="API Keys",
            page_subtitle="Long-lived keys for your applications.",
            keys=keys,
            created_key=None,
            api_base_url=settings.api_base_url,
        ))

    @app.post("/console/api-keys")
    async def console_create_api_key(
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form("API key", min_length=1, max_length=120),
        limit: str = Form(""),
    ) -> Response:
        limit_microdollars = None
        if limit:
            try:
                limit_microdollars = int(float(limit) * 1_000_000)
            except ValueError:
                limit_microdollars = None
        raw, _ = STORE.create_api_key(
            workspace_id=ctx.workspace.id,
            name=name,
            creator_user_id=ctx.user.id,
            management=False,
            limit_microdollars=limit_microdollars,
        )
        keys = [_key_view(k) for k in STORE.list_keys(ctx.workspace.id)]
        return HTMLResponse(_render(
            "console/api_keys.html",
            settings=settings,
            user=ctx.user,
            active="api-keys",
            page_title="API Keys",
            page_subtitle="Long-lived keys for your applications.",
            keys=keys,
            created_key=raw,
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/credits")
    async def console_credits(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        return HTMLResponse(_render(
            "console/credits.html",
            settings=settings,
            user=ctx.user,
            active="credits",
            page_title="Credits",
            page_subtitle="Top up to keep prepaid routes flowing.",
            credits_available=_money(
                (credit.total_credits_microdollars - credit.total_usage_microdollars - credit.reserved_microdollars)
                if credit else 0
            ),
            credits_usage=_money(credit.total_usage_microdollars if credit else 0),
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

    @app.get("/console/activity")
    async def console_activity(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        events = STORE.activity_events(ctx.workspace.id, limit=50)
        for event in events:
            event["cost_display"] = format_money_precise(int(event.get("cost_microdollars") or 0))
        return HTMLResponse(_render(
            "console/activity.html",
            settings=settings,
            user=ctx.user,
            active="activity",
            page_title="Observability",
            page_subtitle="Per-request metadata, no prompt content.",
            activity=events,
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/byok")
    async def console_byok(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        providers = [
            {
                "provider": p.provider,
                "provider_name": (PROVIDERS[p.provider].name if p.provider in PROVIDERS else p.provider),
                "key_hint": p.key_hint,
            }
            for p in STORE.list_byok_providers(ctx.workspace.id)
        ]
        return HTMLResponse(_render(
            "console/byok.html",
            settings=settings,
            user=ctx.user,
            active="byok",
            page_title="BYOK",
            page_subtitle="Bring your own provider keys.",
            providers=providers,
            api_base_url=settings.api_base_url,
        ))

    @app.post("/console/byok")
    async def console_save_byok(
        ctx: ConsoleDep,
        settings: SettingsDep,
        provider: str = Form(..., min_length=1, max_length=64),
        api_key: str = Form("", max_length=512),
        secret_ref: str = Form("", max_length=512),
        key_hint: str = Form("", max_length=80),
    ) -> Response:
        # The new console UI sends `api_key` (raw); legacy callers + API tests
        # may send `secret_ref` + `key_hint`. Mirror PUT /v1/byok/providers/...
        # so both paths land on the same storage shape.
        api_key = api_key.strip()
        secret_ref = secret_ref.strip()
        explicit_hint = key_hint.strip() or None
        stored_hint: str | None
        if api_key:
            secret_ref = secret_ref or _default_byok_secret_ref(ctx.workspace.id, provider)
            stored_hint = explicit_hint or _byok_key_hint(api_key)
        elif secret_ref:
            stored_hint = explicit_hint
        else:
            return RedirectResponse(url="/console/byok?error=missing_key", status_code=303)
        STORE.upsert_byok_provider(
            workspace_id=ctx.workspace.id,
            provider=provider,
            secret_ref=secret_ref,
            key_hint=stored_hint,
        )
        return RedirectResponse(url="/console/byok", status_code=303)

    @app.get("/console/routing")
    async def console_routing(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        regions = []
        for region in region_payload(settings):
            regions.append({
                "id": region["id"],
                "primary": region["primary"],
                "hostname": settings.regional_api_hostname_template.format(region=region["id"]),
            })
        auto_order = [item.strip() for item in settings.auto_model_order.split(",") if item.strip()]
        return HTMLResponse(_render(
            "console/routing.html",
            settings=settings,
            user=ctx.user,
            active="routing",
            page_title="Routing",
            page_subtitle="Auto-rollover order and regional endpoints.",
            auto_order=auto_order,
            regions=regions,
            configured_regions=configured_regions(settings),
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/settings")
    async def console_settings(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        return HTMLResponse(_render(
            "console/settings.html",
            settings=settings,
            user=ctx.user,
            active="settings",
            page_title="Workspace settings",
            page_subtitle="Names, content storage, integrations.",
            workspace=ctx.workspace,
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/account/preferences")
    async def console_preferences(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        return HTMLResponse(_render(
            "console/account/preferences.html",
            settings=settings,
            user=ctx.user,
            active="preferences",
            page_title="Preferences",
            page_subtitle="Account and sign-in.",
            provider=ctx.session.provider,
            environment=settings.environment,
            api_base_url=settings.api_base_url,
        ))


def _render(template: str, **context: Any) -> str:
    settings: Settings = context.pop("settings")
    user: User = context.pop("user")
    active = str(context.get("active") or "")
    workspaces = STORE.list_workspaces_for_user(user.id)
    current_workspace = context.get("workspace")
    if not isinstance(current_workspace, Workspace):
        current_workspace = workspaces[0] if workspaces else None
    return render_template(
        template,
        api_base_url=context.pop("api_base_url", settings.api_base_url),
        user=user,
        user_email=user.email,
        workspaces=workspaces,
        current_workspace=current_workspace,
        current_workspace_id=current_workspace.id if current_workspace else "",
        console_next_path=_console_path_for_active(active),
        **context,
    )


_money = format_money_display


def _default_byok_secret_ref(workspace_id: str, provider: str) -> str:
    """Mirror of routes/byok.py:_default_byok_secret_ref so the console form
    produces the same secret_ref shape the API path uses."""
    return f"secretmanager://trustedrouter/workspaces/{workspace_id}/providers/{provider}"


def _byok_key_hint(api_key: str) -> str:
    """First-6 + last-4 characters — same hint shape as routes/byok.py."""
    stripped = api_key.strip()
    if len(stripped) <= 10:
        return stripped
    return f"{stripped[:6]}...{stripped[-4:]}"


def _key_view(key: ApiKey) -> dict[str, Any]:
    limit_display = "none" if key.limit_microdollars is None else _money(key.limit_microdollars)
    return {
        "name": key.name,
        "label": key.label,
        "limit_display": limit_display,
        "disabled": key.disabled,
    }


def _reveal_first_key(workspace: Workspace) -> str | None:
    """Best-effort one-shot key reveal for the welcome page. We can't
    re-derive a raw key from its hash, so this only succeeds if a fresh
    raw key has been stashed elsewhere — for now we return None and the
    welcome page falls back to a static "go to API Keys" message."""
    _ = workspace
    return None


def _selected_console_workspace(session: AuthSession, workspaces: list[Workspace]) -> Workspace:
    if session.workspace_id:
        for workspace in workspaces:
            if workspace.id == session.workspace_id:
                return workspace
    return workspaces[0]


def _console_path_for_active(active: str) -> str:
    return {
        "api-keys": "/console/api-keys",
        "byok": "/console/byok",
        "routing": "/console/routing",
        "activity": "/console/activity",
        "settings": "/console/settings",
        "credits": "/console/credits",
        "preferences": "/console/account/preferences",
    }.get(active, "/console/api-keys")


def _safe_console_next(next_path: str) -> str:
    if not next_path.startswith("/console/") or next_path.startswith("//"):
        return "/console/api-keys"
    return next_path


def register_console_route_module(app: FastAPI, _router: APIRouter) -> None:
    """Adapter for the main module's registration pattern."""
    register_console_routes(app)
