from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from trusted_router.auth import Principal, SettingsDep
from trusted_router.money import format_money_display
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.routes.credit_transfers import execute_credit_transfer, transfer_shapes
from trusted_router.schemas import CreditTransferRequest
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/credit-transfers")
    def console_credit_transfers(
        ctx: ConsoleDep,
        settings: SettingsDep,
        sent: str = "",
        error: str = "",
        idempotency_key: str = "",
    ) -> Response:
        movements = STORE.list_credit_movements(
            ctx.workspace.id,
            kinds=["user_transfer_out", "user_transfer_in"],
            limit=100,
        )
        return HTMLResponse(
            render(
                "console/credit_transfers.html",
                settings=settings,
                ctx=ctx,
                active="credits",
                page_title="Credit transfers",
                page_subtitle="Send prepaid credits to another verified user.",
                format_money_display=format_money_display,
                transfers=transfer_shapes(movements),
                idempotency_key=idempotency_key or uuid.uuid4().hex,
                sent=sent == "1",
                error=error,
            )
        )

    @app.post("/console/credit-transfers")
    def console_send_credit_transfer(
        ctx: ConsoleDep,
        settings: SettingsDep,
        recipient_username: str = Form(...),
        amount: str = Form(...),
        idempotency_key: str = Form(...),
    ) -> Response:
        def redirect(**query: str) -> RedirectResponse:
            query["idempotency_key"] = idempotency_key
            return RedirectResponse("/console/credit-transfers?" + urlencode(query), status_code=303)

        if not ctx.can_manage:
            return redirect(error="forbidden")
        try:
            body = CreditTransferRequest(
                recipient_username=recipient_username,
                amount=amount,
                idempotency_key=idempotency_key,
            )
            execute_credit_transfer(
                Principal(
                    user=ctx.user,
                    workspace=ctx.workspace,
                    api_key=None,
                    is_management=True,
                    scopes=frozenset(),
                ),
                body,
                settings,
            )
        except ValidationError:
            return redirect(error="invalid")
        except HTTPException as exc:
            error_code = "transfer_failed"
            if exc.status_code == 402:
                error_code = "insufficient"
            elif exc.status_code == 429:
                error_code = "daily_limit"
            elif exc.status_code == 404:
                error_code = "not_found"
            elif exc.status_code == 403:
                error_code = "forbidden"
            return redirect(error=error_code)
        return redirect(sent="1")
