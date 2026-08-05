from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from trusted_router.auth import Principal, principal_from_request
from trusted_router.bedrock_group_buy import (
    BEDROCK_GROUP_BUY_PUBLIC_MESSAGE_LIMIT,
    BedrockGroupBuyPublicSnapshot,
    pledge_form_values,
    pledge_from_mapping,
    public_snapshot,
)
from trusted_router.config import Settings
from trusted_router.dashboard import public_bedrock_group_buy_html
from trusted_router.errors import api_error
from trusted_router.storage import STORE
from trusted_router.storage_models import (
    BedrockGroupBuyAggregate,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
)

log = logging.getLogger(__name__)

_SNAPSHOT_TTL_SECONDS = 15.0
_snapshot_lock = threading.Lock()
_snapshot_value: BedrockGroupBuyPublicSnapshot | None = None
_snapshot_expires_at = 0.0


def register_bedrock_group_buy_routes(app: FastAPI, settings: Settings) -> None:
    _invalidate_public_snapshot()

    @app.api_route(
        "/bedrock-group-buy",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
    )
    @app.api_route(
        "/bedrock-group-buy/",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def group_buy_page(request: Request) -> HTMLResponse:
        principal = _optional_user_principal(request, settings)
        pledge = await _own_pledge(principal)
        notice = ""
        if request.query_params.get("saved") == "1":
            notice = "Your commitment is saved. You can change or remove it at any time."
        elif request.query_params.get("withdrawn") == "1":
            notice = "Your commitment and anonymous message were removed."
        return await _page_response(
            settings,
            principal=principal,
            pledge=pledge,
            notice=notice,
            share_after_commit=request.query_params.get("saved") == "1",
        )

    @app.post("/bedrock-group-buy/pledge", response_class=HTMLResponse)
    async def save_group_buy_pledge(request: Request) -> Response:
        _assert_same_origin(request, settings)
        principal = _optional_user_principal(request, settings)
        if principal is None:
            return RedirectResponse(
                "/bedrock-group-buy?reason=signin&next=%2Fbedrock-group-buy",
                status_code=303,
            )
        payload: dict[str, object] = {}
        try:
            payload = await _request_values(request)
            saved = await _save_or_withdraw(principal, payload)
        except ValueError as exc:
            return await _page_response(
                settings,
                principal=principal,
                pledge=await _own_pledge(principal),
                error=str(exc),
                submitted_values=payload,
                status_code=422,
            )
        _invalidate_public_snapshot()
        if saved is None:
            log.info("bedrock_group_buy.pledge_withdrawn")
            return RedirectResponse("/bedrock-group-buy?withdrawn=1", status_code=303)
        log.info(
            "bedrock_group_buy.pledge_saved public_message=%s",
            saved.publish_message,
        )
        return RedirectResponse("/bedrock-group-buy?saved=1#share", status_code=303)

    @app.post("/bedrock-group-buy/withdraw")
    async def withdraw_group_buy_pledge(request: Request) -> RedirectResponse:
        _assert_same_origin(request, settings)
        principal = _optional_user_principal(request, settings)
        if principal is None:
            return RedirectResponse(
                "/bedrock-group-buy?reason=signin&next=%2Fbedrock-group-buy",
                status_code=303,
            )
        assert principal.user is not None
        await run_in_threadpool(STORE.withdraw_bedrock_group_buy_pledge, principal.user.id)
        _invalidate_public_snapshot()
        log.info("bedrock_group_buy.pledge_withdrawn")
        return RedirectResponse("/bedrock-group-buy?withdrawn=1", status_code=303)

    @app.get("/v1/bedrock-group-buy")
    async def public_group_buy() -> JSONResponse:
        snapshot = await _public_snapshot(settings)
        return JSONResponse(
            snapshot.public_dict(),
            headers={
                "cache-control": "public, max-age=15, stale-while-revalidate=60",
            },
        )

    @app.get("/v1/bedrock-group-buy/me")
    async def own_group_buy_pledge(request: Request) -> JSONResponse:
        principal = _required_user_principal(request, settings)
        pledge = await _own_pledge(principal)
        return JSONResponse({"pledge": _private_pledge_dict(pledge) if pledge else None})

    @app.api_route("/v1/bedrock-group-buy/pledge", methods=["PUT", "POST"])
    async def upsert_group_buy_pledge(request: Request) -> JSONResponse:
        _assert_same_origin(request, settings)
        principal = _required_user_principal(request, settings)
        try:
            payload = await _request_values(request)
            saved = await _save_or_withdraw(principal, payload)
        except ValueError as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status_code=422,
            )
        _invalidate_public_snapshot()
        snapshot = await _public_snapshot(settings)
        return JSONResponse(
            {
                "pledge": _private_pledge_dict(saved) if saved is not None else None,
                "campaign": snapshot.public_dict(),
            }
        )

    @app.delete("/v1/bedrock-group-buy/pledge")
    async def delete_group_buy_pledge(request: Request) -> JSONResponse:
        _assert_same_origin(request, settings)
        principal = _required_user_principal(request, settings)
        assert principal.user is not None
        removed = await run_in_threadpool(
            STORE.withdraw_bedrock_group_buy_pledge,
            principal.user.id,
        )
        _invalidate_public_snapshot()
        return JSONResponse({"removed": removed})


async def _page_response(
    settings: Settings,
    *,
    principal: Principal | None,
    pledge: BedrockGroupBuyPledge | None,
    notice: str = "",
    error: str = "",
    submitted_values: Mapping[str, object] | None = None,
    share_after_commit: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    snapshot = await _public_snapshot(settings)
    signed_in = principal is not None and principal.user is not None
    body = public_bedrock_group_buy_html(
        settings,
        snapshot=snapshot,
        signed_in=signed_in,
        pledge=pledge,
        form_values=pledge_form_values(pledge, submitted_values),
        notice=notice,
        error=error,
        share_after_commit=share_after_commit,
    )
    cache_control = "private, no-store" if signed_in else "public, max-age=15"
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={"cache-control": cache_control},
    )


async def _public_snapshot(settings: Settings) -> BedrockGroupBuyPublicSnapshot:
    del settings  # Kept in the signature for future campaign configuration.
    global _snapshot_expires_at, _snapshot_value
    now = time.monotonic()
    with _snapshot_lock:
        if _snapshot_value is not None and now < _snapshot_expires_at:
            return _snapshot_value
    aggregate, messages = await run_in_threadpool(_read_public_snapshot_parts)
    snapshot = public_snapshot(aggregate, messages)
    with _snapshot_lock:
        _snapshot_value = snapshot
        _snapshot_expires_at = time.monotonic() + _SNAPSHOT_TTL_SECONDS
    return snapshot


def _read_public_snapshot_parts() -> tuple[
    BedrockGroupBuyAggregate,
    list[BedrockGroupBuyPublicMessage],
]:
    return (
        STORE.bedrock_group_buy_aggregate(),
        STORE.list_bedrock_group_buy_public_messages(limit=BEDROCK_GROUP_BUY_PUBLIC_MESSAGE_LIMIT),
    )


def _invalidate_public_snapshot() -> None:
    global _snapshot_expires_at, _snapshot_value
    with _snapshot_lock:
        _snapshot_value = None
        _snapshot_expires_at = 0.0


def _optional_user_principal(request: Request, settings: Settings) -> Principal | None:
    try:
        principal = principal_from_request(request, settings)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise
    return principal if principal.user is not None else None


def _required_user_principal(
    request: Request,
    settings: Settings,
) -> Principal:
    principal = _optional_user_principal(request, settings)
    if principal is not None and principal.user is not None:
        return principal
    raise api_error(401, "A signed-in user session is required", "unauthorized")


async def _own_pledge(principal: Principal | None) -> BedrockGroupBuyPledge | None:
    if principal is None or principal.user is None:
        return None
    return await run_in_threadpool(
        STORE.get_bedrock_group_buy_pledge,
        principal.user.id,
    )


async def _save_or_withdraw(
    principal: Principal,
    values: Mapping[str, object],
) -> BedrockGroupBuyPledge | None:
    assert principal.user is not None
    pledge = pledge_from_mapping(
        values,
        user_id=principal.user.id,
        workspace_id=principal.workspace.id,
    )
    if pledge is None:
        await run_in_threadpool(
            STORE.withdraw_bedrock_group_buy_pledge,
            principal.user.id,
        )
        return None
    return await run_in_threadpool(STORE.upsert_bedrock_group_buy_pledge, pledge)


async def _request_values(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be an object")
        return {str(key): value for key, value in payload.items()}
    form = await request.form()
    return {str(key): value for key, value in form.items()}


def _private_pledge_dict(pledge: BedrockGroupBuyPledge) -> dict[str, object]:
    return {
        "full_name": pledge.full_name,
        "title": pledge.title,
        "company_name": pledge.company_name,
        "company_url": pledge.company_url,
        "monthly_minimum_microdollars": pledge.monthly_minimum_microdollars,
        "expected_bedrock_monthly_microdollars": (pledge.expected_bedrock_monthly_microdollars),
        "expected_all_llm_monthly_microdollars": (pledge.expected_all_llm_monthly_microdollars),
        "last_month_llm_spend_microdollars": (pledge.last_month_llm_spend_microdollars),
        "last_month_spend_sources": list(pledge.last_month_spend_sources),
        "publish_message": pledge.publish_message,
        "public_message": pledge.public_message,
        "created_at": pledge.created_at,
        "updated_at": pledge.updated_at,
    }


def _assert_same_origin(request: Request, settings: Settings) -> None:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        if settings.environment.lower() == "production":
            raise api_error(403, "Same-origin request required", "forbidden")
        return
    parsed = urlparse(source)
    source_host = (parsed.hostname or "").lower()
    request_host = (request.url.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or source_host != request_host:
        raise api_error(403, "Same-origin request required", "forbidden")
