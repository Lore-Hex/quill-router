"""Receive SES bounce/complaint notifications via SNS.

How this hangs together:

1. We verify the domain identity in Amazon SES (DKIM/SPF/DMARC).
2. We create an SNS topic, e.g. `arn:aws:sns:us-east-1:…:ses-feedback`.
3. In SES we configure the verified identity to publish bounce and
   complaint events to that topic.
4. We subscribe this endpoint (`/internal/ses/notifications`) to the
   topic. SNS first POSTs a `SubscriptionConfirmation` with a
   `SubscribeURL`; this handler GETs the URL to confirm.
5. From then on, each bounce/complaint arrives as a `Notification` whose
   `Message` is the JSON SES feedback envelope. We parse the recipient
   email out and call `STORE.block_email_sending(...)` so the EmailService
   skips future sends to that address.

Signature verification is mandatory — see `sns_verify.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from trusted_router.errors import api_error
from trusted_router.sns_verify import SnsVerificationError, verify_sns_message
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_ses_notification_routes(router: APIRouter) -> None:
    @router.post("/internal/ses/notifications")
    async def ses_notification(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            envelope: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api_error(400, "invalid JSON", ErrorType.BAD_REQUEST) from exc

        try:
            verify_sns_message(envelope)
        except SnsVerificationError as exc:
            log.warning("ses_notification.signature_invalid reason=%s", exc)
            raise api_error(403, "SNS signature verification failed", ErrorType.FORBIDDEN) from exc

        message_id = str(envelope.get("MessageId") or "")
        if message_id and not STORE.record_sns_message_once(message_id):
            return JSONResponse({"data": {"replayed": True, "message_id": message_id}})

        msg_type = envelope.get("Type")
        if msg_type == "SubscriptionConfirmation":
            subscribe_url = envelope.get("SubscribeURL")
            if not isinstance(subscribe_url, str):
                raise api_error(400, "missing SubscribeURL", ErrorType.BAD_REQUEST)
            try:
                response = httpx.get(subscribe_url, timeout=10.0)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.exception("ses_notification.subscribe_failed url=%s", subscribe_url)
                raise api_error(502, "failed to confirm SNS subscription", ErrorType.INTERNAL_ERROR) from exc
            log.info("ses_notification.subscribed topic=%s", envelope.get("TopicArn"))
            return JSONResponse({"data": {"confirmed": True, "topic_arn": envelope.get("TopicArn")}})

        if msg_type == "UnsubscribeConfirmation":
            return JSONResponse({"data": {"unsubscribed": True}})

        # Notification path: parse the SES feedback envelope.
        feedback_raw = envelope.get("Message")
        if not isinstance(feedback_raw, str):
            return JSONResponse({"data": {"ignored": True, "reason": "non-string Message"}})
        try:
            feedback: dict[str, Any] = json.loads(feedback_raw)
        except json.JSONDecodeError:
            return JSONResponse({"data": {"ignored": True, "reason": "Message is not JSON"}})

        kind = str(feedback.get("notificationType") or feedback.get("eventType") or "")
        blocked = _apply_feedback(kind, feedback)
        return JSONResponse({"data": {"kind": kind, "blocked_emails": blocked}})


def _apply_feedback(kind: str, feedback: dict[str, Any]) -> list[str]:
    """Inspect a parsed SES feedback envelope and add email blocks.

    Returns the list of blocked email addresses for telemetry. Both the
    legacy `notificationType` and new EventBridge-style `eventType` field
    names are supported.
    """
    blocked: list[str] = []
    if kind in {"Bounce", "bounce"}:
        bounce = feedback.get("bounce", {}) or {}
        bounce_type = bounce.get("bounceType")
        # Only PERMANENT bounces stop sends. Transient bounces (mailbox full,
        # greylisting) self-resolve and shouldn't suppress permanently.
        if bounce_type and bounce_type.lower() != "permanent":
            return blocked
        feedback_id = bounce.get("feedbackId") or feedback.get("mail", {}).get("messageId")
        for recipient in bounce.get("bouncedRecipients", []) or []:
            email = recipient.get("emailAddress") if isinstance(recipient, dict) else None
            if isinstance(email, str) and email:
                STORE.block_email_sending(
                    email=email,
                    reason="bounce",
                    bounce_type=bounce_type,
                    feedback_id=str(feedback_id) if feedback_id else None,
                )
                blocked.append(email.lower())
    elif kind in {"Complaint", "complaint"}:
        complaint = feedback.get("complaint", {}) or {}
        feedback_id = complaint.get("feedbackId") or feedback.get("mail", {}).get("messageId")
        for recipient in complaint.get("complainedRecipients", []) or []:
            email = recipient.get("emailAddress") if isinstance(recipient, dict) else None
            if isinstance(email, str) and email:
                STORE.block_email_sending(
                    email=email,
                    reason="complaint",
                    feedback_id=str(feedback_id) if feedback_id else None,
                )
                blocked.append(email.lower())
    return blocked
