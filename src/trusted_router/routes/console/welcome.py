"""/console/welcome — one-time key reveal landing for first sign-in."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.storage import STORE, Workspace


def register(app: FastAPI) -> None:
    @app.get("/console/welcome")
    async def console_welcome(
        ctx: ConsoleDep,
        settings: SettingsDep,
        first: int | None = None,
    ) -> Response:
        credit = STORE.get_credit_account(ctx.workspace.id)
        trial_microdollars = credit.total_credits_microdollars if credit else 0
        return HTMLResponse(render(
            "console/welcome.html",
            settings=settings,
            user=ctx.user,
            active="api-keys",
            page_title="Welcome",
            page_subtitle="Save your API key — it won't be shown again.",
            revealed_key=None if first is None else _reveal_first_key(ctx.workspace),
            workspace_name=ctx.workspace.name,
            # `trial_credit` is the formatted display value; the matching
            # raw amount is exposed too so the template can show the
            # "add a card to unlock the trial" CTA when the workspace
            # is still at $0 (the new default — see storage.py
            # create_workspace + routes/internal/webhook.py for the
            # card-attach grant flow).
            trial_credit=money(trial_microdollars),
            trial_credit_microdollars=trial_microdollars,
            api_base_url=settings.api_base_url,
        ))


def _reveal_first_key(workspace: Workspace) -> str | None:
    """Best-effort one-shot key reveal for the welcome page. We can't
    re-derive a raw key from its hash, so this only succeeds if a fresh
    raw key has been stashed elsewhere — for now we return None and the
    welcome page falls back to a static "go to API Keys" message."""
    _ = workspace
    return None
