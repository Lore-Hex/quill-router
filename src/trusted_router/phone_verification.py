"""Phone ownership proof, as pure functions over a User record.

Kept backend-agnostic on purpose: every store persists `User` as a whole
record, so each backend only has to read-modify-write and the rules live in
exactly one place instead of being reimplemented three times and drifting.

WHY A PHONE AT ALL
------------------
Notifications go only to the account owner, so they cannot be aimed at a
stranger. What a verified phone actually buys is a cost floor on ACCOUNT
FARMING: TrustedRouter sends every customer's notifications from its own A2P
10DLC brand, which makes sender reputation a shared asset, and email — the
cheapest channel to abuse — is the one an unverified account would reach for.
So the phone gates every channel, including email.

A six-digit code is worth about 20 bits, which is nothing without limits, so
this file is mostly limits: a short expiry, a hard attempt cap that burns the
code rather than the clock, and a resend floor. The code is stored hashed with
the same primitives as an API key, because a leaked database should not hand
over the ability to verify someone else's phone.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from dataclasses import dataclass
from typing import Literal

from trusted_router.security import hash_api_key, new_hash_salt, verify_api_key
from trusted_router.storage_models import User, iso_now, utcnow

CODE_TTL_SECONDS = 600  # ten minutes: long enough for a slow SMS, short enough to matter
MAX_ATTEMPTS = 5
RESEND_FLOOR_SECONDS = 60

ConfirmStatus = Literal["ok", "no_pending", "expired", "too_many_attempts", "mismatch"]


class PhoneNumberError(ValueError):
    """The number is not something a carrier will accept."""


@dataclass(frozen=True)
class ConfirmResult:
    status: ConfirmStatus
    attempts_remaining: int = 0

    @property
    def verified(self) -> bool:
        return self.status == "ok"


def normalize_phone(raw: str) -> str:
    """To E.164, or raise.

    Carriers reject anything else with a vague 400 rather than "your number has
    dots in it", so the shape is enforced here where the error can be useful.
    """
    if not raw or not raw.strip():
        raise PhoneNumberError("phone number is required")
    cleaned = re.sub(r"[\s().-]", "", raw.strip())
    if not cleaned.startswith("+"):
        raise PhoneNumberError("phone number must be E.164 and start with '+', e.g. +15551234567")
    digits = cleaned[1:]
    if not digits.isdigit():
        raise PhoneNumberError("phone number may contain only digits after '+'")
    if not 8 <= len(digits) <= 15:  # E.164 allows at most 15 digits
        raise PhoneNumberError("phone number must have between 8 and 15 digits")
    return "+" + digits


def new_code() -> str:
    """Six digits, uniformly random, leading zeros preserved."""
    return f"{secrets.randbelow(1_000_000):06d}"


def can_resend(user: User, *, now: dt.datetime | None = None) -> tuple[bool, int]:
    """(allowed, seconds_to_wait). A resend floor keeps a retry loop — or
    someone else's phone — from being texted continuously."""
    now = now or utcnow()
    sent_at = getattr(user, "phone_code_sent_at", None)
    if not sent_at:
        return True, 0
    try:
        previous = dt.datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        return True, 0
    elapsed = (now - previous).total_seconds()
    if elapsed >= RESEND_FLOOR_SECONDS:
        return True, 0
    return False, int(RESEND_FLOOR_SECONDS - elapsed)


def begin(
    user: User,
    phone: str,
    *,
    channel: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Stage `phone` against the user and return the code to send.

    The number is held in `pending_phone` rather than `phone`: an unconfirmed
    number must never be reachable, or "start verification" would itself be a
    way to send someone a message.
    """
    now = now or utcnow()
    normalized = normalize_phone(phone)
    code = new_code()
    salt = new_hash_salt()

    user.pending_phone = normalized
    user.phone_code_salt = salt
    user.phone_code_hash = hash_api_key(code, salt)
    user.phone_code_expires_at = (
        (now + dt.timedelta(seconds=CODE_TTL_SECONDS)).isoformat().replace("+00:00", "Z")
    )
    user.phone_code_attempts = 0
    user.phone_code_sent_at = now.isoformat().replace("+00:00", "Z")
    user.phone_code_channel = channel if channel in {"sms", "voice"} else None
    return code


def confirm(user: User, code: str, *, now: dt.datetime | None = None) -> ConfirmResult:
    """Check a code and, on success, promote the pending number."""
    now = now or utcnow()

    if not user.pending_phone or not user.phone_code_hash or not user.phone_code_salt:
        return ConfirmResult("no_pending")

    expires_at = user.phone_code_expires_at
    if expires_at:
        try:
            deadline = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            deadline = now
        if now >= deadline:
            _clear_pending(user)
            return ConfirmResult("expired")

    if (user.phone_code_attempts or 0) >= MAX_ATTEMPTS:
        _clear_pending(user)
        return ConfirmResult("too_many_attempts")

    if not verify_api_key((code or "").strip(), user.phone_code_salt, user.phone_code_hash):
        user.phone_code_attempts = (user.phone_code_attempts or 0) + 1
        remaining = MAX_ATTEMPTS - user.phone_code_attempts
        if remaining <= 0:
            # Burn the code, not just the clock: 20 bits of entropy survives
            # unlimited guessing, so exhausting the attempts must invalidate it.
            _clear_pending(user)
            return ConfirmResult("too_many_attempts")
        return ConfirmResult("mismatch", attempts_remaining=remaining)

    user.phone = user.pending_phone
    user.phone_verified = True
    user.phone_verified_at = iso_now()
    _clear_pending(user)
    return ConfirmResult("ok")


def clear(user: User) -> None:
    """Forget the phone entirely. Used when a user removes it."""
    user.phone = None
    user.phone_verified = False
    user.phone_verified_at = None
    _clear_pending(user)


def cancel_pending(user: User) -> None:
    """Discard an in-flight proof without changing a verified number."""
    _clear_pending(user)


def _clear_pending(user: User) -> None:
    user.pending_phone = None
    user.phone_code_hash = None
    user.phone_code_salt = None
    user.phone_code_expires_at = None
    user.phone_code_attempts = 0
    user.phone_code_sent_at = None
    user.phone_code_channel = None
