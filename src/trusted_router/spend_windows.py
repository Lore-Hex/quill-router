"""Fixed UTC calendar spend windows for per-key daily/weekly/monthly limits.

The windows are deliberately FIXED and LAZY (docs/design: key window limits):
- daily   = UTC midnight to midnight
- weekly  = ISO week, Monday 00:00 UTC
- monthly = 1st of the month 00:00 UTC

Counters live on the hot `tr_key_limit` row and are reset lazily: the settle
UPDATE (release_key) and the authorize check both compare the stored window
start against the current floor and treat an older window as zero. No cron, no
background jobs, no scans — approximate by design (in-flight holds are not
counted; a window boundary mid-request books to the window the settle lands in).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

# Window names, in the order they appear everywhere (columns, API fields).
WINDOWS = ("daily", "weekly", "monthly")

# Suggested per-window budgets, OFFERED (not applied) in the console when a user
# opts into spend limits. Anchored to the $200/mo plan; the ratios keep the
# windows coherent: weekly ~= half the monthly, daily ~= a fifth of the weekly.
# These are hints only — a key has no window limits unless the user sets them.
SUGGESTED_MONTHLY_MICRODOLLARS = 200_000_000  # $200/mo — matches the plan


def suggested_window_limits() -> dict[str, int]:
    """Suggested {window: microdollars} anchored to the monthly plan amount:
    weekly = monthly // 2, daily = weekly // 5 (=> $200 / $100 / $20)."""
    monthly = SUGGESTED_MONTHLY_MICRODOLLARS
    weekly = monthly // 2
    daily = weekly // 5
    return {"daily": daily, "weekly": weekly, "monthly": monthly}


SPEND_WINDOW_DECISION_STATE = "spend_window_rate_limit_decision"


@dataclass(frozen=True)
class KeyWindowLimitDecision:
    """The authoritative result of one per-key spend-window check.

    ``remaining`` is the settled spend headroom observed by the check. In-flight
    holds are intentionally absent because the limiter itself is approximate and
    does not count them. Keeping the response metadata on this object prevents a
    later counter read from disagreeing with the allow/deny decision.
    """

    window: str
    limit: int
    remaining: int
    resets_at: dt.datetime
    reset_seconds: int
    allowed: bool


@dataclass(frozen=True)
class KeyLimitReserveResult:
    """One key-cap admission result, including the hold actually taken.

    ``reserved_microdollars`` is deliberately separate from the requested
    estimate. An uncapped or BYOK-excluded request takes no lifetime-cap hold,
    and settlement must preserve that authorize-time fact even if the key's
    configuration changes while the request is in flight.
    """

    window_decision: KeyWindowLimitDecision | None
    reserved_microdollars: int


class KeyWindowLimitExceeded(ValueError):
    """A per-window key spend limit blocked the request."""

    def __init__(self, decision: KeyWindowLimitDecision) -> None:
        super().__init__(f"key {decision.window} spend limit exceeded")
        self.decision = decision
        self.window = decision.window


class KeyLimitExceeded(ValueError):
    """The lifetime key cap blocked after an optional window check passed."""

    def __init__(self, decision: KeyWindowLimitDecision | None = None) -> None:
        super().__init__("key limit exceeded")
        self.decision = decision


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)

# ApiKey JSON config field per window (mirrored to tr_key_limit *_limit_micro).
LIMIT_FIELDS = {
    "daily": "limit_daily_microdollars",
    "weekly": "limit_weekly_microdollars",
    "monthly": "limit_monthly_microdollars",
}


def window_floors(now: dt.datetime) -> dict[str, dt.datetime]:
    """The current window start (UTC) for each window, given tz-aware `now`."""
    now = now.astimezone(dt.UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - dt.timedelta(days=now.weekday())  # ISO: Monday
    month = day.replace(day=1)
    return {"daily": day, "weekly": week, "monthly": month}


def window_resets_at(window: str, now: dt.datetime) -> dt.datetime:
    """When the given window next resets (UTC): the start of the next window."""
    floors = window_floors(now)
    if window == "daily":
        return floors["daily"] + dt.timedelta(days=1)
    if window == "weekly":
        return floors["weekly"] + dt.timedelta(days=7)
    if window == "monthly":
        start = floors["monthly"]
        return (start + dt.timedelta(days=32)).replace(day=1)
    raise ValueError(f"unknown window {window!r}")


def decide_key_window_limits(
    window_limits: dict[str, int],
    used_by_window: dict[str, int],
    amount_microdollars: int,
    *,
    now: dt.datetime,
) -> KeyWindowLimitDecision | None:
    """Return the window that governs this request, or ``None`` if uncapped.

    A rejecting window wins in the stable daily/weekly/monthly enforcement
    order. When every configured window allows the request, the window with the
    least absolute spend headroom is the useful policy for an agent deciding
    whether it can afford its next call.
    """
    decisions: list[KeyWindowLimitDecision] = []
    for window in WINDOWS:
        configured = window_limits.get(window)
        if configured is None:
            continue
        limit = int(configured)
        used = int(used_by_window.get(window, 0))
        remaining = max(0, limit - used)
        resets_at = window_resets_at(window, now)
        reset_seconds = max(0, math.ceil((resets_at - now).total_seconds()))
        decisions.append(
            KeyWindowLimitDecision(
                window=window,
                limit=limit,
                remaining=remaining,
                resets_at=resets_at,
                reset_seconds=reset_seconds,
                allowed=amount_microdollars <= remaining,
            )
        )
    if not decisions:
        return None
    rejected = next((decision for decision in decisions if not decision.allowed), None)
    if rejected is not None:
        return rejected
    return min(decisions, key=lambda decision: decision.remaining)


def spend_window_headers(
    decision: KeyWindowLimitDecision,
    *,
    retry_after: bool = False,
) -> dict[str, str]:
    """Serialize one spend-window verdict using the public HTTP convention."""
    headers = {
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(decision.reset_seconds),
    }
    if retry_after:
        headers["Retry-After"] = str(max(1, decision.reset_seconds))
    return headers


def remember_spend_window_decision(
    request: Any | None,
    decision: KeyWindowLimitDecision | None,
) -> None:
    """Make a verdict available to response middleware without another read."""
    if request is not None and decision is not None:
        setattr(request.state, SPEND_WINDOW_DECISION_STATE, decision)


def spend_window_limit_error_message(decision: KeyWindowLimitDecision) -> str:
    reset = decision.resets_at.isoformat().replace("+00:00", "Z")
    return f"API key {decision.window} spend limit exceeded; resets at {reset}"


def key_window_limits(key: object) -> dict[str, int]:
    """The window limits CONFIGURED on an ApiKey (micro-dollars), omitting unset
    windows. Empty dict = no window limits. Used for alert-threshold reads AND,
    via enforced_window_limits, for blocking."""
    out: dict[str, int] = {}
    for window, field in LIMIT_FIELDS.items():
        value = getattr(key, field, None)
        if value is not None:
            out[window] = int(value)
    return out


def enforced_window_limits(key: object) -> dict[str, int]:
    """The window limits that BLOCK (429). Empty when the key is in alert mode
    (`budget_alert_only`) — alert-mode budgets never stop a working app; they
    email at settle instead (services/budget_alerts.py). Limit-mode keys enforce
    their configured windows."""
    if getattr(key, "budget_alert_only", False):
        return {}
    return key_window_limits(key)
