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


class KeyWindowLimitExceeded(ValueError):
    """A per-window key spend limit blocked the request. Carries which window
    so callers can compute Retry-After from the window's reset time."""

    def __init__(self, window: str) -> None:
        super().__init__(f"key {window} spend limit exceeded")
        self.window = window


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
