"""Reach the account owner: push, email, sms, or voice.

The destination is NOT a parameter. It is resolved from the api_key to the
workspace to that workspace's owner, and only ever to contacts that owner has
proved they control. That single decision is what makes this safe to expose on
an ordinary api_key: it cannot be aimed at a stranger, so it is a
self-notification primitive rather than a messaging API.

WHERE THIS RUNS
---------------
The control plane, never the enclave. A customer's agent decides to escalate
and calls this from outside; message bodies and contact details therefore never
enter the attested inference path, and the zero-retention story stays a
statement about inference alone instead of something that has to be
re-litigated for every notification.

THE GATE
--------
Every channel requires a VERIFIED PHONE — including email, which costs us a
hundredth of a cent to send. The phone is not protecting the recipient (there
is only one possible recipient); it is a cost floor on account farming.
TrustedRouter sends all customer notifications from its own 10DLC brand, so
sender reputation is shared, and the cheapest channel is the one a farmed
account would abuse.

Push is the exception on PRICE, not on the gate: it is free because it costs us
nothing and is delivered by our own client, so the cheapest way for an agent to
reach its human is to install SREChat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trusted_router.config import Settings
from trusted_router.phone_verification import CODE_TTL_SECONDS
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.services.telephony import get_telephony_service
from trusted_router.storage_models import User

log = logging.getLogger(__name__)

NotifyChannel = Literal["push", "email", "sms", "voice"]

# (owner, subject, body) -> (delivered, detail). Injected rather than imported
# so this module stays free of device-token plumbing and stays testable without
# a push provider.
PushSender = Callable[[User, str, str], tuple[bool, str]]
CHANNELS: tuple[NotifyChannel, ...] = ("push", "email", "sms", "voice")

RefusalReason = Literal[
    "unknown_channel",
    "no_owner",
    "phone_not_verified",
    "email_not_attached",
    "email_not_verified",
    "no_push_device",
    "empty_body",
    "channel_unavailable",
]


@dataclass(frozen=True)
class NotifyOutcome:
    delivered: bool
    channel: NotifyChannel
    price_microdollars: int
    detail: str
    carrier: str | None = None
    refusal: RefusalReason | None = None

    @property
    def billable(self) -> bool:
        """Only a delivered notification is billable.

        A carrier outage must not charge the customer: they asked for their
        human to be reached and their human was not reached. Charging for the
        attempt would also make an outage look like revenue, which is exactly
        the signal we least want distorted.
        """
        return self.delivered and self.price_microdollars > 0


def price_for(channel: NotifyChannel, settings: Settings) -> int:
    if channel == "push":
        return settings.notify_push_price_microdollars
    return settings.notify_price_microdollars


def _refuse(channel: NotifyChannel, reason: RefusalReason, detail: str) -> NotifyOutcome:
    return NotifyOutcome(
        delivered=False, channel=channel, price_microdollars=0, detail=detail, refusal=reason
    )


def spoken_code(code: str) -> str:
    """A verification code as a phone call says it.

    Digits are spaced so a speech engine reads "1 2 3 4 5 6" rather than "one
    hundred twenty-three thousand", and the whole thing is repeated because the
    listener is usually reaching for a pen the first time.
    """
    spaced = " ".join(code.strip())
    return f"Your verification code is {spaced}. Again, {spaced}."


def send_verification_code(
    settings: Settings,
    phone: str,
    code: str,
    *,
    channel: Literal["sms", "voice"] = "sms",
    preferred_carrier: str | None = None,
) -> tuple[bool, str]:
    """Deliver a verification code to a number nobody has proved yet.

    This is the ONE place TrustedRouter messages an unverified number, which is
    unavoidable — proving a number requires contacting it. What keeps it from
    being an open SMS relay is upstream, in phone_verification: a resend floor,
    a short expiry, and an attempt cap that burns the code. Nothing here should
    be reachable without those.

    Voice matters more than it looks. A2P 10DLC registration is per carrier and
    takes days, and an unregistered sender's SMS is rejected outright — but a
    phone call needs no registration at all. So voice is the delivery path that
    works while registration is pending, and the only reason the rest of the
    notify gate is reachable at all in that window.

    The code is never logged: the return detail is the carrier's, and callers
    log that rather than the message.
    """
    service = get_telephony_service(settings)
    if not service.enabled:
        return False, "no carrier is configured for verification codes"

    if channel == "voice":
        body = spoken_code(code)
    else:
        minutes = max(1, CODE_TTL_SECONDS // 60)
        body = f"your verification code is {code}. It expires in {minutes} minutes."

    result = service.send(channel, phone, body, preferred_carrier=preferred_carrier)
    return result.delivered, result.detail


def check_owner_reachable(user: User | None, channel: NotifyChannel) -> NotifyOutcome | None:
    """Refusal for this owner/channel, or None if the send may proceed.

    Separate from sending so the API can answer "would this work?" without
    spending anything, and so the gate is testable without carriers.
    """
    if user is None:
        return _refuse(channel, "no_owner", "this api key's workspace has no owner")

    if not user.phone_verified:
        return _refuse(
            channel,
            "phone_not_verified",
            "a verified phone number is required before any notification can be sent; "
            "verify one in settings",
        )

    if channel == "email":
        if not user.email:
            return _refuse(channel, "email_not_attached", "this account has no email attached")
        if not user.email_verified:
            return _refuse(channel, "email_not_verified", "this account's email is not verified")

    if channel in {"sms", "voice"} and not user.phone:
        return _refuse(channel, "phone_not_verified", "no phone number on file")

    return None


class NotifyService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(
        self,
        *,
        owner: User | None,
        channel: NotifyChannel,
        subject: str,
        body: str,
        preferred_carrier: str | None = None,
        push_sender: PushSender | None = None,
    ) -> NotifyOutcome:
        if channel not in CHANNELS:
            return _refuse(channel, "unknown_channel", f"channel must be one of {list(CHANNELS)}")

        if not (body or "").strip():
            # A notification with no content wastes a page and teaches its
            # reader to ignore the next one.
            return _refuse(channel, "empty_body", "body is required")

        refusal = check_owner_reachable(owner, channel)
        if refusal is not None:
            return refusal

        assert owner is not None  # narrowed by check_owner_reachable
        price = price_for(channel, self._settings)

        if channel == "push":
            return self._send_push(owner, subject, body, price, push_sender)
        if channel == "email":
            return self._send_email(owner, subject, body, price)
        return self._send_telephony(owner, channel, subject, body, price, preferred_carrier)

    # -- channels -----------------------------------------------------------

    def _send_push(
        self,
        owner: User,
        subject: str,
        body: str,
        price: int,
        push_sender: PushSender | None,
    ) -> NotifyOutcome:
        if push_sender is None:
            return _refuse(
                "push",
                "channel_unavailable",
                "push delivery is not configured on this deployment",
            )
        delivered, detail = push_sender(owner, subject, body)
        if not delivered:
            # Most often "no device registered", which is a prompt to install
            # SREChat rather than an error in the caller's request.
            return NotifyOutcome(False, "push", 0, detail, refusal="no_push_device")
        return NotifyOutcome(True, "push", price, detail, carrier="apns")

    def _send_email(self, owner: User, subject: str, body: str, price: int) -> NotifyOutcome:
        service = get_email_service(self._settings)
        if not service.enabled:
            return _refuse("email", "channel_unavailable", "email delivery is not configured")
        sent = service.send(
            EmailMessage(
                to=owner.email or "",
                subject=subject or "Notification",
                text_body=body,
                mail_class="transactional",
            )
        )
        if not sent:
            return NotifyOutcome(False, "email", 0, "email provider refused the message")
        return NotifyOutcome(True, "email", price, "sent", carrier="ses")

    def _send_telephony(
        self,
        owner: User,
        channel: NotifyChannel,
        subject: str,
        body: str,
        price: int,
        preferred_carrier: str | None,
    ) -> NotifyOutcome:
        service = get_telephony_service(self._settings)
        if not service.enabled:
            return _refuse(channel, "channel_unavailable", f"{channel} delivery is not configured")

        text = f"{subject}: {body}" if subject else body
        result = service.send(
            "sms" if channel == "sms" else "voice",
            owner.phone or "",
            text,
            preferred_carrier=preferred_carrier,
        )
        if not result.delivered:
            return NotifyOutcome(False, channel, 0, result.detail)
        if result.failed_primary:
            log.warning("notify %s delivered on the fallback carrier: %s", channel, result.attempts)
        return NotifyOutcome(True, channel, price, result.detail, carrier=result.carrier)


def get_notify_service(settings: Settings) -> NotifyService:
    return NotifyService(settings)
