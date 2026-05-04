"""/console/byok — list configured BYOK providers and add new ones.

POST mirrors PUT /v1/byok/providers/{provider} — accepts either a raw
api_key (stored as an envelope-encrypted BYOK row) or an explicit
secret_ref."""

from __future__ import annotations

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.byok_crypto import encrypt_byok_secret
from trusted_router.catalog import PROVIDERS
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/byok")
    async def console_byok(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        providers = [
            {
                "provider": p.provider,
                "provider_name": (
                    PROVIDERS[p.provider].name if p.provider in PROVIDERS else p.provider
                ),
                "key_hint": p.key_hint,
            }
            for p in STORE.list_byok_providers(ctx.workspace.id)
        ]
        return HTMLResponse(render(
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
        provider_slug = provider.strip().lower()
        secret_ref = secret_ref.strip()
        explicit_hint = key_hint.strip() or None
        stored_hint: str | None
        encrypted_secret = None
        if api_key:
            if secret_ref and not secret_ref.startswith("byok://"):
                return RedirectResponse(url="/console/byok?error=secret_ref", status_code=303)
            secret_ref = _default_secret_ref(ctx.workspace.id, provider_slug)
            stored_hint = explicit_hint or _key_hint(api_key)
            encrypted_secret = encrypt_byok_secret(
                api_key,
                settings,
                workspace_id=ctx.workspace.id,
                provider=provider_slug,
            )
        elif secret_ref:
            if secret_ref.startswith("byok://"):
                return RedirectResponse(url="/console/byok?error=secret_ref", status_code=303)
            stored_hint = explicit_hint
        else:
            return RedirectResponse(url="/console/byok?error=missing_key", status_code=303)
        STORE.upsert_byok_provider(
            workspace_id=ctx.workspace.id,
            provider=provider_slug,
            secret_ref=secret_ref,
            key_hint=stored_hint,
            encrypted_secret=encrypted_secret,
        )
        return RedirectResponse(url="/console/byok", status_code=303)


def _default_secret_ref(workspace_id: str, provider: str) -> str:
    """Mirror routes/byok.py:_default_byok_secret_ref so the console form
    produces the same encrypted BYOK reference shape as the API path."""
    return f"byok://workspaces/{workspace_id}/providers/{provider}"


def _key_hint(api_key: str) -> str:
    """First-6 + last-4 characters — same hint shape as routes/byok.py."""
    stripped = api_key.strip()
    if len(stripped) <= 10:
        return stripped
    return f"{stripped[:6]}...{stripped[-4:]}"
