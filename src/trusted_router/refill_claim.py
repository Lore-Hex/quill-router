"""Workspace-scoped off-session payment identity. Decisions run inside the store transaction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import CreditAccount


@dataclass(frozen=True)
class RefillClaim:
    key: str
    payload_hash: str
    opened_at: str
    dispatched_at: str
    state: str = "sending"


def refill_now() -> datetime:
    return datetime.now(UTC)


def recent_block(account: CreditAccount, now: datetime) -> str | None:
    status = account.last_auto_refill_status or ""
    if not account.last_auto_refill_at or not (status == "pending" or status.startswith("failed:")):
        return None
    try:
        last = datetime.fromisoformat(account.last_auto_refill_at)
    except ValueError:
        return "reconcile_required"
    elapsed = now - (last if last.tzinfo else last.replace(tzinfo=UTC))
    if status == "pending" and elapsed < timedelta(minutes=5):
        return "pending"
    if status.startswith("failed:") and elapsed < timedelta(minutes=5):
        return "rate_limited"
    return None


def decide_claim(
    account: CreditAccount,
    expected: CreditAccount,
    balance: tuple[int, int, int],
    current: RefillClaim | None,
    *,
    candidate: str,
    used: bool,
    payload_hash: str,
    now: datetime,
) -> tuple[str, RefillClaim | None]:
    if not account.auto_refill_enabled:
        return "disabled", None
    if account != expected:
        return "account_changed", None
    credits, usage, reserved = balance
    if credits - usage - reserved >= account.auto_refill_threshold_microdollars:
        return "above_threshold", None
    blocked = recent_block(account, now)
    if blocked:
        return blocked, None
    stamp = now.isoformat()
    if current and current.state in {"sending", "accepted"}:
        interval = timedelta(minutes=30 if current.state == "accepted" else 5)
        if now - datetime.fromisoformat(current.dispatched_at) < interval:
            return "pending", None
        # An accepted intent needs its webhook, not another charge. Stripe may
        # prune keys after 24h, so stop automatic retries well before then.
        if (
            current.state == "accepted"
            or current.payload_hash != payload_hash
            or now - datetime.fromisoformat(current.opened_at) >= timedelta(hours=23)
        ):
            return "reconcile_required", None
        return "claimed", replace(current, dispatched_at=stamp)
    if current and current.state != "succeeded" and not current.state.startswith("failed:"):
        return "reconcile_required", None
    if account.last_auto_refill_status == "pending":
        # Legacy intent without a recoverable Stripe key.
        return "reconcile_required", None
    if used:
        return "already_attempted", None
    return "claimed", RefillClaim(candidate, payload_hash, stamp, stamp)


def record_result(
    account: CreditAccount,
    claim: RefillClaim | None,
    *,
    key: str | None,
    status: str,
    stamp: str,
) -> tuple[bool, RefillClaim | None]:
    if claim is None:
        if key is not None:
            return False, None
    else:
        if key != claim.key or claim.state == "succeeded":
            return False, claim
        if claim.state.startswith("failed:") and status != "succeeded":
            return False, claim
        claim = replace(claim, state="accepted" if status == "pending" else status)
    account.last_auto_refill_at = stamp
    account.last_auto_refill_status = status
    return True, claim
