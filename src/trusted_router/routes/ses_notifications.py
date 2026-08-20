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
import sys
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.ses_suppression import (
    SesSuppressionService,
    SesSuppressionSyncError,
)
from trusted_router.sns_verify import SnsVerificationError, verify_sns_message
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_ses_notification_routes(router: APIRouter, settings: Settings) -> None:
    account_suppression = SesSuppressionService(settings)

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
            # TEMP(2026-07-05): mirror the failure to stderr so it reaches
            # Cloud Logging — module logs ship to Axiom only, and this 403
            # has been rejecting every SNS delivery for days. Metadata only
            # (field names, type, cert host); never the Message content.
            cert_url = str(envelope.get("SigningCertURL") or envelope.get("SigningCertUrl") or "")
            print(
                "ses_notification.signature_invalid "
                f"reason={str(exc)!r} keys={sorted(envelope.keys())} "
                f"type={envelope.get('Type')!r} "
                f"sig_version={envelope.get('SignatureVersion')!r} "
                f"cert_host={urlparse(cert_url).hostname!r}",
                file=sys.stderr,
                flush=True,
            )
            raise api_error(403, "SNS signature verification failed", ErrorType.FORBIDDEN) from exc

        message_id = str(envelope.get("MessageId") or "")

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
            if message_id:
                STORE.record_sns_message_once(message_id)
            return JSONResponse({"data": {"confirmed": True, "topic_arn": envelope.get("TopicArn")}})

        if msg_type == "UnsubscribeConfirmation":
            if message_id:
                STORE.record_sns_message_once(message_id)
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
        # Apply the suppression first so a transient storage failure leaves the
        # SNS message retryable. The message-id claim only controls reporting:
        # suppression writes are idempotent, while duplicate SNS deliveries
        # must not inflate bounce/complaint counts.
        try:
            blocked_count = _apply_feedback(
                kind,
                feedback,
                account_suppression,
                emit_log=False,
            )
        except SesSuppressionSyncError as exc:
            log.error("ses_notification.account_suppression_sync_failed")
            raise api_error(
                503,
                "SES suppression synchronization failed",
                ErrorType.INTERNAL_ERROR,
            ) from exc
        replayed = bool(message_id and not STORE.record_sns_message_once(message_id))
        if replayed:
            blocked_count = 0
        else:
            _log_feedback(kind, feedback, blocked_count)
        return JSONResponse(
            {
                "data": {
                    "kind": kind,
                    "blocked_count": blocked_count,
                    "replayed": replayed,
                }
            }
        )


def _apply_feedback(
    kind: str,
    feedback: dict[str, Any],
    account_suppression: SesSuppressionService,
    *,
    emit_log: bool = True,
) -> int:
    """Inspect a parsed SES feedback envelope and add email blocks.

    Returns a count, never recipient identities. Both the legacy
    `notificationType` and new EventBridge-style `eventType` field names are
    supported. Processing is idempotent because suppression writes use the
    normalized recipient as their key.
    """
    blocked_count = 0
    tags = _mail_tags(feedback)
    if kind in {"Bounce", "bounce"}:
        raw_bounce = feedback.get("bounce")
        bounce = raw_bounce if isinstance(raw_bounce, dict) else {}
        raw_bounce_type = bounce.get("bounceType")
        bounce_type = str(raw_bounce_type) if raw_bounce_type else None
        raw_recipients = bounce.get("bouncedRecipients")
        recipients = raw_recipients if isinstance(raw_recipients, list) else []
        # Only PERMANENT bounces stop sends. Transient bounces (mailbox full,
        # greylisting) self-resolve and shouldn't suppress permanently.
        if bounce_type and bounce_type.lower() != "permanent":
            recipients = []
        else:
            feedback_id = bounce.get("feedbackId") or _mail_message_id(feedback)
            for recipient in recipients:
                email = recipient.get("emailAddress") if isinstance(recipient, dict) else None
                if isinstance(email, str) and email:
                    STORE.block_email_sending(
                        email=email,
                        reason="bounce",
                        bounce_type=bounce_type,
                        feedback_id=str(feedback_id) if feedback_id else None,
                        **tags,
                    )
                    account_suppression.suppress(email, "BOUNCE")
                    blocked_count += 1
    elif kind in {"Complaint", "complaint"}:
        raw_complaint = feedback.get("complaint")
        complaint = raw_complaint if isinstance(raw_complaint, dict) else {}
        raw_recipients = complaint.get("complainedRecipients")
        recipients = raw_recipients if isinstance(raw_recipients, list) else []
        feedback_id = complaint.get("feedbackId") or _mail_message_id(feedback)
        for recipient in recipients:
            email = recipient.get("emailAddress") if isinstance(recipient, dict) else None
            if isinstance(email, str) and email:
                STORE.block_email_sending(
                    email=email,
                    reason="complaint",
                    feedback_id=str(feedback_id) if feedback_id else None,
                    **tags,
                )
                account_suppression.suppress(email, "COMPLAINT")
                blocked_count += 1
    if emit_log:
        _log_feedback(kind, feedback, blocked_count)
    return blocked_count


def _log_feedback(kind: str, feedback: dict[str, Any], blocked_count: int) -> None:
    if kind not in {"Bounce", "bounce", "Complaint", "complaint"}:
        return
    tags = _mail_tags(feedback)
    raw_bounce = feedback.get("bounce")
    bounce = raw_bounce if isinstance(raw_bounce, dict) else {}
    raw_complaint = feedback.get("complaint")
    complaint = raw_complaint if isinstance(raw_complaint, dict) else {}
    bounce_type = str(bounce.get("bounceType") or "none")
    raw_recipients = (
        bounce.get("bouncedRecipients")
        if kind in {"Bounce", "bounce"}
        else complaint.get("complainedRecipients")
    )
    recipient_count = len(raw_recipients) if isinstance(raw_recipients, list) else 0
    feedback_id = bounce.get("feedbackId") or complaint.get("feedbackId")
    ses_message_id = _mail_message_id(feedback)
    log.warning(
        "ses_feedback.received kind=%s bounce_type=%s recipients=%d blocked=%d "
        "class=%s profile=%s source=%s medium=%s campaign=%s message_id=%s feedback_id=%s",
        kind.lower(),
        bounce_type,
        recipient_count,
        blocked_count,
        tags["mail_class"] or "unknown",
        tags["sender_profile"] or "unknown",
        tags["acquisition_source"] or "unknown",
        tags["acquisition_medium"] or "unknown",
        tags["acquisition_campaign"] or "unknown",
        ses_message_id or "unknown",
        feedback_id or "unknown",
        extra={
            "event": "ses_feedback.received",
            "feedback_kind": kind.lower(),
            "bounce_type": bounce_type,
            "recipient_count": recipient_count,
            "blocked_count": blocked_count,
            "ses_message_id": ses_message_id or "unknown",
            "feedback_id": str(feedback_id or "unknown"),
            **{name: value or "unknown" for name, value in tags.items()},
        },
    )


def _mail_tags(feedback: dict[str, Any]) -> dict[str, str | None]:
    mail = feedback.get("mail")
    raw_tags = mail.get("tags") if isinstance(mail, dict) else None
    if not isinstance(raw_tags, dict):
        raw_tags = {}

    def first(name: str) -> str | None:
        value = raw_tags.get(name)
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0][:256]
        if isinstance(value, str):
            return value[:256]
        return None

    return {
        "mail_class": first("mail_class"),
        "sender_profile": first("sender_profile"),
        "acquisition_source": first("acquisition_source"),
        "acquisition_medium": first("acquisition_medium"),
        "acquisition_campaign": first("acquisition_campaign"),
    }


def _mail_message_id(feedback: dict[str, Any]) -> str | None:
    mail = feedback.get("mail")
    if not isinstance(mail, dict):
        return None
    message_id = mail.get("messageId")
    return str(message_id) if message_id else None
