from __future__ import annotations

import hashlib

import httpx
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.routable_payouts import (
    ROUTABLE_RELEASE_STATUSES,
    normalize_routable_status,
)
from trusted_router.services.routable_payouts import (
    RoutableAPIError,
    RoutableClient,
    verify_routable_webhook,
)
from trusted_router.storage import STORE


def register(router: APIRouter) -> None:
    @router.post("/internal/routable/webhook")
    async def routable_webhook(
        request: Request,
        settings: SettingsDep,
    ) -> Response:
        raw_body = await request.body()
        payload = verify_routable_webhook(
            raw_body=raw_body,
            headers=request.headers,
            settings=settings,
        )
        if payload is None:
            return Response(status_code=401)
        try:
            await _process_event(payload, settings)
        except (RoutableAPIError, ValueError):
            # Routable explicitly permits 503 and retries it. Keeping this path
            # under a three-second upstream budget satisfies its five-second
            # webhook deadline without acknowledging an unprocessed event.
            return Response(status_code=503, headers={"Retry-After": "1"})
        event_id = hashlib.sha256(raw_body).hexdigest()
        await run_in_threadpool(
            STORE.record_webhook_event_once,
            "routable",
            event_id,
        )
        return Response(status_code=200)


async def _process_event(payload: dict[str, object], settings: Settings) -> None:
    event_resource = str(payload["event_resource"])
    if event_resource != "payable":
        return
    if not settings.routable_credentials_configured:
        raise ValueError("Routable API credentials are not configured")
    payable_id = str(payload["object_id"])
    client = RoutableClient(
        settings,
        timeout=httpx.Timeout(3.0, connect=1.0),
    )
    payable = await client.retrieve_payable(payable_id)
    cashout = await run_in_threadpool(
        STORE.get_earnings_cashout_by_routable_payable,
        payable_id,
    )
    if cashout is None:
        external_id = str(payable.get("external_id") or "")
        if external_id:
            cashout = await run_in_threadpool(
                STORE.get_earnings_cashout_by_external_id,
                external_id,
            )
    if cashout is None:
        return
    status = normalize_routable_status(payable.get("status"))
    if status in ROUTABLE_RELEASE_STATUSES:
        await run_in_threadpool(
            STORE.release_earnings_cashout,
            cashout.user_id,
            cashout.id,
            state=str(status),
            routable_status=status,
            error_code="routable_terminal_failure",
        )
        return
    await run_in_threadpool(
        STORE.mark_earnings_cashout,
        cashout.user_id,
        cashout.id,
        state=status or "submitted",
        routable_payable_id=payable_id,
        routable_status=status,
        error_code=None,
    )
