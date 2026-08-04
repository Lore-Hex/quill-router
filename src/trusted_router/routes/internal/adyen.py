"""HMAC-authenticated Adyen standard webhook receiver."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.services.adyen_billing import (
    apply_adyen_notification,
    notification_items,
    prepare_adyen_notification,
    verify_adyen_notification,
)
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.post("/internal/adyen/webhook")
    async def adyen_webhook(request: Request, settings: SettingsDep) -> PlainTextResponse:
        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise api_error(400, "Invalid Adyen webhook JSON", ErrorType.BAD_REQUEST) from exc
        if not isinstance(payload, dict):
            raise api_error(400, "Invalid Adyen webhook payload", ErrorType.BAD_REQUEST)

        items = notification_items(payload)
        # Validate the whole batch before mutating anything. If one item is
        # forged or malformed, Adyen can retry the batch without leaving a
        # partially accepted payment behind.
        for item in items:
            verify_adyen_notification(item, settings)
        live_value: Any = payload.get("live")
        if isinstance(live_value, bool):
            live = live_value
        elif isinstance(live_value, str) and live_value.lower() in {"true", "false"}:
            live = live_value.lower() == "true"
        else:
            raise api_error(400, "Invalid Adyen webhook environment", ErrorType.BAD_REQUEST)
        prepared = [
            prepare_adyen_notification(item, live=live, settings=settings)
            for item in items
        ]
        for notification in prepared:
            apply_adyen_notification(notification)
        return PlainTextResponse("[accepted]")
