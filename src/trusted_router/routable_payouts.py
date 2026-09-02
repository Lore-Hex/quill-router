from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trusted_router.money import MICRODOLLARS_PER_DOLLAR

ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS = 100 * MICRODOLLARS_PER_DOLLAR
MICRODOLLARS_PER_CENT = MICRODOLLARS_PER_DOLLAR // 100

ROUTABLE_PAYOUT_PROFILE_KIND = "routable_payout_profile"
ROUTABLE_PAYOUT_PROFILE_COMPANY_KIND = "routable_payout_profile_company"
EARNINGS_CASHOUT_KIND = "earnings_cashout"
EARNINGS_CASHOUT_IDEMPOTENCY_KIND = "earnings_cashout_idempotency"
EARNINGS_CASHOUT_PAYABLE_KIND = "earnings_cashout_payable"
EARNINGS_CASHOUT_EXTERNAL_KIND = "earnings_cashout_external"

ROUTABLE_RELEASE_STATUSES = frozenset({"canceled"})
ROUTABLE_PAID_STATUSES = frozenset({"completed", "externally_paid"})
ROUTABLE_ACTION_REQUIRED_STATUSES = frozenset({"failed", "issue"})
ROUTABLE_PENDING_STATUSES = frozenset(
    {
        "compliance_hold",
        "created",
        "initiated",
        "needs_approval",
        "pending",
        "po_discrepancy_hold",
        "processing",
        "ready_to_send",
        "scheduled",
    }
)
ROUTABLE_KNOWN_STATUSES = (
    ROUTABLE_RELEASE_STATUSES
    | ROUTABLE_PAID_STATUSES
    | ROUTABLE_PENDING_STATUSES
    | ROUTABLE_ACTION_REQUIRED_STATUSES
)
ROUTABLE_DEFINITIVE_NO_EFFECT_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 422}
)

_SAFE_ERROR_CODE = re.compile(r"[^a-z0-9_.:-]+")
_REVERSE_TIME_MAX = 9_999_999_999_999_999_999
_ROUTABLE_TIMEZONE = ZoneInfo("America/Los_Angeles")


def new_payout_id() -> str:
    reverse_time = _REVERSE_TIME_MAX - time.time_ns()
    return f"po_{reverse_time:019d}_{uuid.uuid4().hex}"


def payout_entity_id(user_id: str, payout_id: str) -> str:
    return f"{user_id}#{payout_id}"


def payout_idempotency_entity_id(user_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{user_id}\0{idempotency_key}".encode()).hexdigest()


def payout_request_fingerprint(
    *,
    user_id: str,
    amount_microdollars: int,
    routable_company_id: str,
    payment_method_id: str,
) -> str:
    encoded = json.dumps(
        {
            "amount_microdollars": amount_microdollars,
            "payment_method_id": payment_method_id,
            "routable_company_id": routable_company_id,
            "user_id": user_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def routable_company_external_id(user_id: str) -> str:
    digest = hashlib.sha256(f"trustedrouter-routable-user\0{user_id}".encode()).hexdigest()
    return f"tr-user-{digest[:40]}"


def routable_payable_external_id(payout_id: str) -> str:
    return f"trustedrouter-{payout_id}"


def routable_idempotency_key(payout_id: str) -> str:
    return f"tr-payout-{payout_id}"


def routable_send_date(created_at: str) -> str:
    """Freeze an immediate payout to its creation date in Pacific time."""

    timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(_ROUTABLE_TIMEZONE).date().isoformat()


def routable_amount(amount_microdollars: int) -> str:
    if amount_microdollars <= 0:
        raise ValueError("cashout amount must be positive")
    if amount_microdollars % MICRODOLLARS_PER_CENT:
        raise ValueError("cashout amount must use whole cents")
    dollars = Decimal(amount_microdollars) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return format(dollars, ".2f")


def normalize_routable_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in ROUTABLE_KNOWN_STATUSES else None


def safe_routable_error_code(value: object) -> str:
    normalized = str(value or "routable_error").strip().lower()[:96]
    normalized = _SAFE_ERROR_CODE.sub("_", normalized).strip("_")
    return normalized or "routable_error"


def routable_error_is_definitive_no_effect(status_code: int | None) -> bool:
    """Return true only when Routable has definitively rejected the request.

    Network failures, rate limits, and server errors are ambiguous: a payable
    may have been created even though TrustedRouter did not receive its ID.
    Those cases remain reserved and are reconciled by external ID.
    """

    return status_code in ROUTABLE_DEFINITIVE_NO_EFFECT_HTTP_STATUSES


def validate_routable_release_status(status: str | None) -> None:
    """Permit balance release only after rejection or final cancellation."""

    if status is not None and status not in ROUTABLE_RELEASE_STATUSES:
        raise ValueError("Routable cash-out balance can be released only when canceled")
