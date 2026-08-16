"""Owner earnings ledger and credit transfer console."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.money import dollars_to_microdollars, format_money_display
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/earnings")
    async def console_earnings(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        return HTMLResponse(_render_page(request, ctx, settings))

    @app.post("/console/earnings/transfer")
    async def console_transfer_earnings(
        ctx: ConsoleDep,
        workspace_id: str = Form(...),
        amount: str = Form(...),
        idempotency_key: str | None = Form(default=None, max_length=64),
    ) -> Response:
        workspace = next(
            (candidate for candidate in ctx.workspaces if candidate.id == workspace_id),
            None,
        )
        if workspace is None:
            return _redirect("error=workspace")
        if workspace.federated_home:
            return _redirect("error=federated")
        try:
            amount_microdollars = dollars_to_microdollars(amount)
        except ValueError:
            return _redirect("error=amount")
        if amount_microdollars <= 0:
            return _redirect("error=amount")
        event_suffix = idempotency_key or str(uuid.uuid4())
        outcome = STORE.transfer_earnings_to_workspace(
            ctx.user.id,
            workspace.id,
            amount_microdollars,
            f"earnings_transfer:{ctx.user.id}:{event_suffix}",
        )
        if outcome == "insufficient":
            return _redirect("error=insufficient")
        return _redirect("saved=duplicate" if outcome == "duplicate" else "saved=transferred")


def _render_page(request: Request, ctx: ConsoleDep, settings: SettingsDep) -> str:
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    summary = STORE.earnings_summary(ctx.user.id)
    model_totals = STORE.custom_model_earnings_by_model(ctx.user.id, since=since)
    by_model: list[dict[str, Any]] = []
    for model_id, amount in sorted(
        model_totals.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        model = STORE.get_user_model(model_id)
        by_model.append(
            {
                "model_id": model_id,
                "model_name": model.name if model is not None else None,
                "earned_microdollars": amount,
                "earned_display": format_money_display(amount),
            }
        )
    recent = [
        {
            "kind": movement.kind,
            "amount_display": format_money_display(movement.amount_microdollars),
            "custom_model_id": movement.custom_model_id,
            "counterparty_account_id": movement.counterparty_account_id,
            "created_at": movement.created_at,
        }
        for movement in STORE.list_credit_movements(f"user:{ctx.user.id}", limit=50)
    ]
    return render(
        "console/earnings.html",
        settings=settings,
        ctx=ctx,
        active="earnings",
        page_title="Earnings",
        page_subtitle="Credits earned by your user-provided models.",
        summary={
            **summary,
            "total_earned_display": format_money_display(summary["total_earned"]),
            "total_transferred_display": format_money_display(
                summary["total_transferred"]
            ),
            "available_display": format_money_display(summary["available"]),
        },
        by_model_30d=by_model,
        recent=recent,
        transfer_workspaces=[
            workspace for workspace in ctx.workspaces if not workspace.federated_home
        ],
        transfer_idempotency_key=str(uuid.uuid4()),
        flash=_flash_message(
            request.query_params.get("saved"),
            request.query_params.get("error"),
        ),
    )


def _redirect(query: str) -> RedirectResponse:
    return RedirectResponse(url=f"/console/earnings?{query}", status_code=303)


def _flash_message(saved: str | None, error: str | None) -> dict[str, str] | None:
    if saved == "transferred":
        return {
            "type": "success",
            "text": "Earnings transferred into the workspace.",
        }
    if saved == "duplicate":
        return {
            "type": "success",
            "text": "That transfer was already completed; no credits moved twice.",
        }
    if error == "insufficient":
        return {"type": "error", "text": "Available earnings are insufficient."}
    if error == "federated":
        return {
            "type": "error",
            "text": "Earnings cannot be transferred to a federated workspace.",
        }
    if error == "workspace":
        return {"type": "error", "text": "Workspace not found."}
    if error == "amount":
        return {"type": "error", "text": "Enter a transfer amount greater than $0."}
    return None
