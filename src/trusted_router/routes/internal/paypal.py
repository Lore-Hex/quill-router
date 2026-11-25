"""PayPal webhook handler for one-time prepaid credit purchases."""

from __future__ import annotations

import json
import uuid
from functools import partial
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.money import money_pair
from trusted_router.services.paypal_billing import (
    credit_paypal_capture,
    verify_paypal_webhook_signature,
)
from trusted_router.services.paypal_topup_guard import observe_paypal_capture
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.post("/internal/paypal/webhook")
    async def paypal_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise api_error(400, "Invalid PayPal webhook JSON", ErrorType.BAD_REQUEST) from exc
        if not isinstance(event, dict):
            raise api_error(400, "Invalid PayPal webhook payload", ErrorType.BAD_REQUEST)

        await run_in_threadpool(
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
            result = await run_in_threadpool(credit_paypal_capture, event)
            if result.credited:
                # Normally the capture route credits first and this delivery
                # is a duplicate; observe here only when the webhook credited.
                await run_in_threadpool(
                    partial(
                        observe_paypal_capture,
                        STORE,
                        workspace_id=result.workspace_id,
                        seller_protection=result.seller_protection,
                        payer_email_matches=None,
                        settings=settings,
                    )
                )
            return {
                "data": {
                    "event_id": event_id,
                    "order_id": result.order_id,
                    "capture_id": result.capture_id,
                    "workspace_id": result.workspace_id,
                    "credited": result.credited,
                    "status": result.status,
                    "held": result.held,
                    **money_pair("amount", result.amount_microdollars),
                    **money_pair("processing_fee", result.processing_fee_microdollars),
                    **money_pair("total", result.charge_amount_microdollars),
                }
            }
        from trusted_router.services.paypal_topup_guard import latch_after_paypal_adverse
        from trusted_router.services.paypal_trust import record_paypal_adverse

        results = await run_in_threadpool(record_paypal_adverse, event)
        if results:
            # After the adverse fact committed: a dispute or reversal on a
            # new workspace latches it, so what is left cannot be spent.
            latches = await run_in_threadpool(
                latch_after_paypal_adverse, STORE, results, settings=settings
            )
            data: dict[str, Any] = {
                "event_id": event_id,
                "adverse": [result.outcome for _, result in results],
            }
            if latches:
                data["latches"] = latches
            return {"data": data}
        return {"data": {"ignored": True, "event_id": event_id}}
