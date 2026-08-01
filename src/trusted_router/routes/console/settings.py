"""/console/settings — workspace name, content storage status."""

from __future__ import annotations

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE

MAX_WORKSPACE_NAME = 120


def register(app: FastAPI) -> None:
    @app.get("/console/settings")
    async def console_settings(
        ctx: ConsoleDep,
        settings: SettingsDep,
        saved: str = "",
        error: str = "",
    ) -> Response:
        return HTMLResponse(render(
            "console/settings.html",
            settings=settings,
            user=ctx.user,
            active="settings",
            page_title="Workspace settings",
            page_subtitle="Names, content storage, integrations.",
            workspace=ctx.workspace,
            api_base_url=ctx.api_base_url,
            can_manage=STORE.user_can_manage(ctx.user.id, ctx.workspace.id),
            saved=bool(saved),
            error=error,
        ))

    @app.post("/console/settings")
    async def console_update_settings(
        ctx: ConsoleDep,
        name: str = Form(""),
    ) -> Response:
        """Rename the workspace.

        This handler did not exist. The template has posted here since the page
        was written, so every Save returned 405 "Method Not Allowed" and the
        rename silently never worked.
        """
        # Renaming is a management action: the name appears on invoices and in
        # every member's console, so a plain member must not be able to change
        # it. Same gate the API-key management routes use.
        if not STORE.user_can_manage(ctx.user.id, ctx.workspace.id):
            return RedirectResponse(url="/console/settings?error=forbidden", status_code=303)

        cleaned = name.strip()
        if not cleaned:
            # Reject rather than store a blank. An empty name renders as an
            # unidentifiable entry in the workspace switcher, and the only way
            # back is this same form.
            return RedirectResponse(url="/console/settings?error=name", status_code=303)
        if len(cleaned) > MAX_WORKSPACE_NAME:
            # The input carries maxlength, but that is a client-side hint, not a
            # constraint on anything posting directly.
            return RedirectResponse(url="/console/settings?error=too_long", status_code=303)

        if STORE.update_workspace(ctx.workspace.id, name=cleaned) is None:
            return RedirectResponse(url="/console/settings?error=missing", status_code=303)

        # POST/redirect/GET: without the redirect, a refresh re-submits the form.
        return RedirectResponse(url="/console/settings?saved=1", status_code=303)
