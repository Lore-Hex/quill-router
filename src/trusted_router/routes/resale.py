"""Accountless NYTE holder actions, mounted only on the durable control plane."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from starlette.concurrency import run_in_threadpool

from trusted_router.config import Settings
from trusted_router.services import resale
from trusted_router.storage_resale import reference

PATH = "/token-exchange/reservations"
HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


class HolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["email", "redeem", "positions", "backfill", "list", "withdraw", "request_refund"]
    email: str = Field(default="", max_length=254)
    token: str = Field(default="", max_length=256)
    session: str = Field(default="", max_length=128)
    position: str = Field(default="", max_length=256)
    listing: str = Field(default="", max_length=128)
    request_id: str = Field(default="", max_length=128)
    lots: StrictInt = 0
    cursor: str | None = Field(default=None, max_length=256)


def handle(body: HolderRequest, settings: Settings) -> dict:
    if body.action == "redeem":
        return resale.redeem_link(body.token, settings)
    holder = resale.command("session", key=reference(body.session))["holder"]
    if body.action == "backfill":
        resale.backfill_email(body.email, holder, settings)
        return {"status": "reconciled"}
    if body.action == "positions":
        result = resale.command("positions", holder=holder, cursor=body.cursor)
        # No Stripe payment reference, contact identity, fill data, or other
        # holder identifiers in the browser representation.
        return {"resale_enabled": settings.resale_enabled, "cursor": result["cursor"], "positions": [
            {**{key: p[key] for key in ("id", "session_id", "grade", "lots", "per_lot_cents", "purchase_completed_at", "listed_lots", "sold_lots", "status")}, "listing_rows": [{key: row[key] for key in ("id", "lots_listed", "lots_sold", "listed_at", "status")} for row in p["listing_rows"]]}
            for p in result["positions"]
        ]}
    if body.action == "list" and not settings.resale_enabled:
        raise ValueError("Resale listing is not enabled yet")
    if not body.request_id:
        raise ValueError("An action request ID is required")
    result = resale.command(body.action, holder=holder, position=body.position,
                            listing=body.listing, lots=body.lots, request_id=body.request_id)
    if body.action == "request_refund":
        resale.alert_ops(settings, "NYTE full refund requested", f"Reservation {body.position} has a durable full-refund request. Refund its original Stripe payment in full; no balance credits. Run python -m scripts.retry_nyte_resale_refunds --full-refunds to list requests.")
    return result


def register(app: FastAPI, settings: Settings) -> None:
    @app.options(PATH, include_in_schema=False)
    async def preflight(request: Request) -> JSONResponse:
        headers = _headers(request)
        if headers is None:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=HEADERS)
        return JSONResponse({}, headers={**headers, "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})

    @app.post(PATH, include_in_schema=False)
    async def action(request: Request, background: BackgroundTasks) -> JSONResponse:
        from trusted_router.routes.public import _inquiry_client_identity, _inquiry_rate_ok

        headers = _headers(request)
        if headers is None:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=HEADERS)
        def response(data: dict, status: int = 200) -> JSONResponse:
            return JSONResponse(data, status_code=status, headers=headers)
        try:
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return response({"error": "Use JSON"}, 415)
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    return response({"error": "Request too large"}, 413)
                raw.extend(chunk)
            body = HolderRequest.model_validate_json(raw)
            identity = _inquiry_client_identity(request, settings)
            if body.action == "email":
                if _inquiry_rate_ok("nyte-holder-email:" + identity):
                    background.add_task(resale.send_holder_link, body.email, settings)
                return response({"message": resale.GENERIC_RESPONSE})
            from trusted_router.exchange_analytics import accept_event
            if not accept_event("nyte-holder:" + identity):
                return response({"error": "Try again shortly"}, 429)
            return response(await run_in_threadpool(handle, body, settings))
        except (ValidationError, json.JSONDecodeError):
            return response({"error": "Invalid request"}, 422)
        except ValueError as exc:
            return response({"error": str(exc)}, 400)
        except Exception:  # noqa: BLE001 - never log submitted email/capabilities or SDK bodies
            return response({"error": "Reservations temporarily unavailable; try again"}, 503)


def _headers(request: Request) -> dict[str, str] | None:
    origin = request.headers.get("origin")
    if origin != "https://nytokenexchange.com":
        return None
    return {**HEADERS, "Access-Control-Allow-Origin": origin, "Vary": "Origin"}
