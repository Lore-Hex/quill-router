"""PayPal webhook handler for one-time prepaid credit purchases."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.money import money_pair
from trusted_router.routes.internal.webhook_work import run_provider_webhook_work
from trusted_router.services.paypal_billing import (
    credit_paypal_capture,
    verify_paypal_webhook_signature,
)
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.post("/internal/paypal/webhook")
    async def paypal_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise api_error(400, "Invalid PayPal webhook JSON", ErrorType.BAD_REQUEST) from exc
        if not isinstance(event, dict):
            raise api_error(400, "Invalid PayPal webhook payload", ErrorType.BAD_REQUEST)

        await run_provider_webhook_work(
            "paypal",
            verify_paypal_webhook_signature,
            headers=request.headers,
            event=event,
            settings=settings,
        )

        event_id = str(event.get("id") or uuid.uuid4())
        if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
            # Signature verification and the idempotent credit transaction are
            # synchronous. Keep both off the shared async control-plane loop so
            # a slow PayPal API/Store cannot stall unrelated login or console
            # requests.
            result = await run_provider_webhook_work(
                "paypal", credit_paypal_capture, event
            )
            return {
                "data": {
                    "event_id": event_id,
                    "order_id": result.order_id,
                    "capture_id": result.capture_id,
                    "workspace_id": result.workspace_id,
                    "credited": result.credited,
                    "status": result.status,
                    **money_pair("amount", result.amount_microdollars),
                    **money_pair("processing_fee", result.processing_fee_microdollars),
                    **money_pair("total", result.charge_amount_microdollars),
                }
            }
        return {"data": {"ignored": True, "event_id": event_id}}
