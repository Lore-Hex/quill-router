from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.routes.oauth_authorized_apps import (
    _authorized_app_shape,
    _live_delegated_snapshots,
    _monthly_budget,
    _owned_app_snapshots,
    _update_all_or_rollback,
)
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/authorized-apps")
    def console_authorized_apps(
        ctx: ConsoleDep,
        settings: SettingsDep,
        saved: str | None = None,
    ) -> Response:
        _require_manager(ctx)
        grouped: dict[str, list[Any]] = {}
        for snapshot in _live_delegated_snapshots(ctx.workspace.id):
            grouped.setdefault(snapshot.api_key.app_id, []).append(snapshot)
        apps = [
            _authorized_app_shape(oauth_app, grouped[app_id])
            for app_id in sorted(grouped)
            if (oauth_app := STORE.get_oauth_app(app_id)) is not None
        ]
        return HTMLResponse(
            render(
                "console/authorized_apps.html",
                settings=settings,
                ctx=ctx,
                active="authorized-apps",
                page_title="Authorized apps",
                page_subtitle="Review and control apps connected to this workspace.",
                apps=apps,
                saved=saved,
                money=money,
            )
        )

    @app.post("/console/authorized-apps/{app_id}/budget")
    def console_update_authorized_app_budget(
        app_id: str,
        ctx: ConsoleDep,
        monthly_budget: str = Form(""),
    ) -> Response:
        _require_manager(ctx)
        snapshots = _owned_app_snapshots(ctx.workspace.id, app_id)
        try:
            value = _monthly_budget(monthly_budget or None)
        except HTTPException:
            return RedirectResponse(
                "/console/authorized-apps?saved=invalid-budget", status_code=303
            )
        _update_all_or_rollback(
            [row.api_key for row in snapshots],
            field="limit_monthly_microdollars",
            value=value,
        )
        return RedirectResponse("/console/authorized-apps?saved=budget", status_code=303)

    @app.post("/console/authorized-apps/{app_id}/revoke")
    def console_revoke_authorized_app(app_id: str, ctx: ConsoleDep) -> Response:
        _require_manager(ctx)
        snapshots = _owned_app_snapshots(ctx.workspace.id, app_id)
        _update_all_or_rollback(
            [row.api_key for row in snapshots], field="disabled", value=True
        )
        return RedirectResponse("/console/authorized-apps?saved=revoked", status_code=303)


def _require_manager(ctx: Any) -> None:
    if not ctx.can_manage:
        raise HTTPException(status_code=403, detail="Requires workspace manager role")
