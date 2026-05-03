"""Console root + workspace switcher.

`/console` redirects to `/console/api-keys` (the default landing).
`/console/workspaces/select` POST is the sidebar workspace switcher —
it stamps the chosen workspace_id onto the auth session cookie so
subsequent page loads pick up the same workspace."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response

from trusted_router.auth import SESSION_COOKIE_NAME
from trusted_router.routes.console._shared import ConsoleDep, safe_console_next
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
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
        return RedirectResponse(url=safe_console_next(next_path), status_code=303)
