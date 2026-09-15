"""Funded-key support on the control surface, which already owns SES access."""

import logging
import re

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.public import _inquiry_rate_ok
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.services.lightning import LightningCredits
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, repr=False)
    message: str = Field(min_length=1, max_length=3000, repr=False)


def register_lightning_support_routes(router: APIRouter) -> None:
    @router.post("/lightning/feedback", include_in_schema=False)
    def feedback(body: Feedback, request: Request, settings: SettingsDep) -> dict[str, bool]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 256:
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
        raw = header[7:]
        # Recheck revocation and funding here even if the funding website
        # already verified them. Never trust identity fields from the form.
        try:
            value = LightningCredits(STORE).account(raw)
        except ValueError as exc:
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED) from exc
        if not value.support_eligible:
            raise api_error(403, "Funded account required", ErrorType.FORBIDDEN)
        email, message = body.email.strip(), body.message.strip()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", email) or not message:
            raise api_error(400, "Valid email and message required", ErrorType.BAD_REQUEST)
        if raw in email + message or re.search(r"sk-tr-v1-", email + message, re.IGNORECASE):
            raise api_error(400, "Remove API keys from feedback", ErrorType.BAD_REQUEST)
        # Defense in depth for direct callers of this control-plane endpoint.
        # The funding website additionally enforces a durable account limit.
        if not _inquiry_rate_ok("lightning-support:" + value.account_id):
            raise api_error(429, "Feedback rate limited", ErrorType.RATE_LIMITED)
        try:
            sent = get_email_service(settings).send(EmailMessage(
                to=settings.support_email, reply_to=email, subject="LightningRouter feedback",
                text_body=(f"User ID: {value.user_id}\nWorkspace ID: {value.account_id}\n"
                           f"Reply to: {email}\n\n{message}\n"), mail_class="support_inquiry",
            ))
        except Exception:  # noqa: BLE001 - never log feedback or credentials
            sent = False
        if not sent:
            logging.getLogger(__name__).error("lightning.feedback_delivery_failed")
            raise api_error(503, "Feedback delivery unavailable", ErrorType.SERVICE_UNAVAILABLE)
        return {"sent": True}
