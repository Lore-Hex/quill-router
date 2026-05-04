"""/console/api-keys — list, create, and reveal API keys for the current
workspace. POST creates a new key and renders the same page with the
raw key one-shot revealed in the response."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.money import dollars_to_microdollars
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.storage import STORE, ApiKey


def register(app: FastAPI) -> None:
    @app.get("/console/api-keys")
    async def console_api_keys(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        keys = [_key_view(k) for k in STORE.list_keys(ctx.workspace.id)]
        return HTMLResponse(render(
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
                limit_microdollars = dollars_to_microdollars(limit)
            except ValueError:
                return RedirectResponse(url="/console/api-keys?error=limit", status_code=303)
            if limit_microdollars < 0:
                return RedirectResponse(url="/console/api-keys?error=limit", status_code=303)
        raw, _ = STORE.create_api_key(
            workspace_id=ctx.workspace.id,
            name=name,
            creator_user_id=ctx.user.id,
            management=False,
            limit_microdollars=limit_microdollars,
        )
        keys = [_key_view(k) for k in STORE.list_keys(ctx.workspace.id)]
        return HTMLResponse(render(
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


def _key_view(key: ApiKey) -> dict[str, Any]:
    limit_display = "none" if key.limit_microdollars is None else money(key.limit_microdollars)
    return {
        "name": key.name,
        "label": key.label,
        "limit_display": limit_display,
        "disabled": key.disabled,
    }
