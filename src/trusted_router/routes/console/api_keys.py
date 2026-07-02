"""/console/api-keys — list, create, and manage API keys for the current
workspace: one-shot reveal on create, budgets (lifetime + daily/weekly/monthly
windows), disable/enable, and delete (disabled keys only — disabling first
stops new authorizes so in-flight typed holds drain before the row goes away).
All mutations are form POSTs -> 303 redirects with ?saved=/?error= flash params
(the console's no-JS pattern)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.errors import assert_workspace_billing_active
from trusted_router.money import dollars_to_microdollars, microdollars_to_decimal
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.spend_windows import WINDOWS
from trusted_router.storage import STORE, ApiKey

_FLASH = {
    "saved:limit": ("success", "Budgets saved."),
    "saved:disabled": ("success", "Key disabled — it can no longer authorize requests."),
    "saved:enabled": ("success", "Key enabled."),
    "saved:deleted": ("success", "Key deleted."),
    "error:limit": ("error", "Budgets must be non-negative dollar amounts."),
    "error:delete-active": ("error", "Disable the key first, then delete it."),
}


def register(app: FastAPI) -> None:
    def _render_page(
        ctx: Any,
        settings: Any,
        *,
        created_key: str | None = None,
        saved: str | None = None,
        error: str | None = None,
    ) -> Response:
        keys = [_key_view(k) for k in STORE.list_keys(ctx.workspace.id)]
        flash = None
        if saved:
            flash = _FLASH.get(f"saved:{saved}")
        elif error:
            flash = _FLASH.get(f"error:{error}")
        return HTMLResponse(render(
            "console/api_keys.html",
            settings=settings,
            user=ctx.user,
            active="api-keys",
            page_title="API Keys",
            page_subtitle="Long-lived keys for your applications.",
            keys=keys,
            created_key=created_key,
            flash=flash,
            api_base_url=settings.api_base_url,
        ))

    @app.get("/console/api-keys")
    async def console_api_keys(
        ctx: ConsoleDep,
        settings: SettingsDep,
        saved: str | None = None,
        error: str | None = None,
    ) -> Response:
        return _render_page(ctx, settings, saved=saved, error=error)

    @app.post("/console/api-keys")
    async def console_create_api_key(
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form("API key", min_length=1, max_length=120),
        limit: str = Form(""),
        limit_daily: str = Form(""),
        limit_weekly: str = Form(""),
        limit_monthly: str = Form(""),
    ) -> Response:
        try:
            limit_microdollars = _parse_limit(limit)
            daily_micro = _parse_limit(limit_daily)
            weekly_micro = _parse_limit(limit_weekly)
            monthly_micro = _parse_limit(limit_monthly)
        except ValueError:
            return RedirectResponse(url="/console/api-keys?error=limit", status_code=303)
        assert_workspace_billing_active(ctx.workspace)  # quiesce: no new keys while paused
        raw, _ = STORE.create_api_key(
            workspace_id=ctx.workspace.id,
            name=name,
            creator_user_id=ctx.user.id,
            management=False,
            limit_microdollars=limit_microdollars,
            limit_daily_microdollars=daily_micro,
            limit_weekly_microdollars=weekly_micro,
            limit_monthly_microdollars=monthly_micro,
        )
        return _render_page(ctx, settings, created_key=raw)

    @app.post("/console/api-keys/{key_hash}/limit")
    async def console_update_api_key_limit(
        ctx: ConsoleDep,
        key_hash: str,
        limit: str = Form(""),
        limit_daily: str = Form(""),
        limit_weekly: str = Form(""),
        limit_monthly: str = Form(""),
    ) -> Response:
        _require_key(ctx, key_hash)
        try:
            patch: dict[str, Any] = {"limit_microdollars": _parse_limit(limit)}
            for window, value in (
                ("daily", limit_daily), ("weekly", limit_weekly), ("monthly", limit_monthly),
            ):
                # Empty input = clear the window limit (explicit None).
                patch[f"limit_{window}_microdollars"] = _parse_limit(value)
        except ValueError:
            return RedirectResponse(url="/console/api-keys?error=limit", status_code=303)
        STORE.update_key(key_hash, patch)
        return RedirectResponse(url="/console/api-keys?saved=limit", status_code=303)

    @app.post("/console/api-keys/{key_hash}/disable")
    async def console_disable_api_key(ctx: ConsoleDep, key_hash: str) -> Response:
        _require_key(ctx, key_hash, manage=True)
        STORE.update_key(key_hash, {"disabled": True})
        return RedirectResponse(url="/console/api-keys?saved=disabled", status_code=303)

    @app.post("/console/api-keys/{key_hash}/enable")
    async def console_enable_api_key(ctx: ConsoleDep, key_hash: str) -> Response:
        _require_key(ctx, key_hash, manage=True)
        STORE.update_key(key_hash, {"disabled": False})
        return RedirectResponse(url="/console/api-keys?saved=enabled", status_code=303)

    @app.post("/console/api-keys/{key_hash}/delete")
    async def console_delete_api_key(ctx: ConsoleDep, key_hash: str) -> Response:
        key = _require_key(ctx, key_hash, manage=True)
        # Disable-first, then delete: an ACTIVE key may have in-flight typed
        # holds; deleting it mid-flight strands them (issue #29). Disabling
        # stops new authorizes and the holds settle/drain, making the delete
        # safe in practice.
        if not key.disabled:
            return RedirectResponse(url="/console/api-keys?error=delete-active", status_code=303)
        STORE.delete_key(key_hash)
        return RedirectResponse(url="/console/api-keys?saved=deleted", status_code=303)


def _require_key(ctx: Any, key_hash: str, *, manage: bool = False) -> ApiKey:
    """Ownership check for every mutating route; `manage=True` additionally
    requires owner/manager role (codex #94: disable/enable/delete are
    destructive — a plain workspace member must not kill another member's
    keys; budget edits keep the console's pre-existing member-level access)."""
    key = STORE.get_key_by_hash(key_hash)
    if key is None or key.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=404, detail="API key not found")
    if manage and not STORE.user_can_manage(ctx.user.id, ctx.workspace.id):
        raise HTTPException(status_code=403, detail="Requires workspace manager role")
    return key


def _parse_limit(value: str) -> int | None:
    """'' -> None (no limit); a dollar string -> microdollars; negative raises."""
    normalized = value.strip()
    if not normalized:
        return None
    micro = dollars_to_microdollars(normalized)
    if micro < 0:
        raise ValueError("limit must be non-negative")
    return micro


def _key_view(key: ApiKey) -> dict[str, Any]:
    limit_display = "none" if key.limit_microdollars is None else money(key.limit_microdollars)
    # One typed point-read for live usage + current window spend (falls back to
    # the JSON values when typed is off / row missing).
    typed = getattr(STORE, "typed_key_usage", None)
    usage = typed(key.hash) if typed is not None else None
    windows_used = (usage or {}).get("windows", {})
    lifetime_used = usage["usage"] if usage is not None else key.usage_microdollars
    window_views = []
    for window in WINDOWS:
        limit_value = getattr(key, f"limit_{window}_microdollars", None)
        window_views.append({
            "name": window,
            "input": "" if limit_value is None else microdollars_to_decimal(limit_value),
            "limit_display": None if limit_value is None else money(limit_value),
            "used_display": money(windows_used.get(window, 0)) if limit_value is not None else None,
        })
    return {
        "hash": key.hash,
        "name": key.name,
        "label": key.label,
        "limit_display": limit_display,
        "limit_input": (
            "" if key.limit_microdollars is None else microdollars_to_decimal(key.limit_microdollars)
        ),
        "usage_display": money(lifetime_used),
        "windows": window_views,
        "disabled": key.disabled,
    }
