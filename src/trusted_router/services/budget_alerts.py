"""Per-key budget ALERT emails.

When a key is in alert mode (`budget_alert_only`, the default), crossing a
daily/weekly/monthly window budget does NOT block the request — it emails the
workspace owner instead ("know when weird shit is happening" without stopping a
working app). Called from the gateway settle path AFTER the window usage is
booked, off the hot path via BackgroundTasks.

Dedup: at most one email per window per window-instance in the common case. The
key carries a JSON `budget_alerted` marker ({window: window_start_iso}); we mark
before sending so a sequential retry doesn't re-notify, and a window reset (new
floor) re-arms the alert. The marker read+write is NOT transactional, so two
settles crossing the same window in the same instant can both send a duplicate
alert. Accepted BY DESIGN (Joseph): an occasional extra "heads up" email is
harmless (no money effect — the enforcement gate is unaffected) and strict
at-most-once would need a conditional/transactional claim on the hot money-path
key row, not worth the complexity for an alert.
"""

from __future__ import annotations

import logging

from trusted_router.config import Settings
from trusted_router.money import microdollars_to_decimal
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.spend_windows import key_window_limits, utcnow, window_floors

log = logging.getLogger(__name__)


def maybe_send_budget_alerts(
    *, api_key_hash: str, workspace_id: str, settings: Settings
) -> None:
    """Email the workspace owner for each window whose budget this key just
    crossed (alert mode only). No-op for limit-mode keys, keys without window
    budgets, or a store with no window-usage view."""
    from trusted_router.storage import STORE

    key = STORE.get_key_by_hash(api_key_hash)
    if key is None or not key.budget_alert_only:
        return
    # Alert on the CONFIGURED thresholds (key_window_limits), NOT enforced_window_limits
    # — enforced is {} in alert mode, but we still want to detect the crossing.
    limits = key_window_limits(key)  # {window: microdollars}; empty = nothing to alert on
    if not limits:
        return
    usage = STORE.typed_key_usage(api_key_hash)
    if usage is None:
        return  # no window-usage view (typed off) — nothing to compare against
    windows: dict[str, int] = usage.get("windows") or {}

    floors = window_floors(utcnow())
    alerted = dict(key.budget_alerted or {})
    crossings: list[tuple[str, int, int]] = []
    for window, limit in limits.items():
        used = int(windows.get(window, 0))
        if used < limit:
            continue
        floor_iso = floors[window].isoformat()
        if alerted.get(window) == floor_iso:
            continue  # already alerted for THIS window instance
        crossings.append((window, used, limit))
        alerted[window] = floor_iso
    if not crossings:
        return
    # Mark before sending so a sequential retry doesn't re-notify. Not
    # transactional -> a rare concurrent duplicate is accepted (see module docstring).
    STORE.update_key(api_key_hash, {"budget_alerted": alerted})
    _deliver(api_key_hash, key.name, workspace_id, crossings, settings)


def _deliver(
    api_key_hash: str,
    key_name: str,
    workspace_id: str,
    crossings: list[tuple[str, int, int]],
    settings: Settings,
) -> None:
    from trusted_router.storage import STORE

    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        return
    owner = STORE.get_user(workspace.owner_user_id)
    if owner is None or not owner.email:
        log.info("budget_alert.no_owner_email key=%s ws=%s", api_key_hash, workspace_id)
        return
    message = build_budget_alert_email(
        to=owner.email,
        key_name=key_name,
        workspace_name=workspace.name,
        crossings=crossings,
    )
    try:
        get_email_service(settings).send(message)
    except Exception:  # best-effort: the window is already marked (at-most-once); make a drop observable
        log.exception("budget_alert.delivery_failed key=%s ws=%s", api_key_hash, workspace_id)


def _usd(micro: int) -> str:
    return f"${microdollars_to_decimal(micro)}"


def build_budget_alert_email(
    *,
    to: str,
    key_name: str,
    workspace_name: str,
    crossings: list[tuple[str, int, int]],
    from_name: str = "TrustedRouter",
) -> EmailMessage:
    windows = ", ".join(w for w, _, _ in crossings)
    subject = f"[{from_name}] Budget alert: key “{key_name}” crossed its {windows} budget"
    lines = [
        f"Heads up — the API key “{key_name}” in workspace “{workspace_name}” has "
        "crossed a spend budget you set as an alert.",
        "",
        "The key is STILL WORKING (alert mode does not block requests). This is "
        "just so you know spend is higher than expected:",
        "",
    ]
    for window, used, limit in crossings:
        lines.append(f"  • {window}: {_usd(used)} used of a {_usd(limit)} budget")
    lines += [
        "",
        "To hard-stop this key when a budget is crossed, switch it to Limit mode "
        "in the console (Console → API Keys → uncheck “Alert only”).",
    ]
    text = "\n".join(lines)
    html_rows = "".join(
        f"<li>{win}: <strong>{_usd(used)}</strong> used of a {_usd(lim)} budget</li>"
        for win, used, lim in crossings
    )
    html = (
        f"<p>Heads up — the API key “<strong>{key_name}</strong>” in workspace "
        f"“{workspace_name}” has crossed a spend budget you set as an alert.</p>"
        "<p>The key is <strong>still working</strong> (alert mode does not block "
        "requests). This is just so you know spend is higher than expected:</p>"
        f"<ul>{html_rows}</ul>"
        "<p>To hard-stop this key when a budget is crossed, switch it to Limit mode "
        "in the console (Console → API Keys → uncheck “Alert only”).</p>"
    )
    return EmailMessage(to=to, subject=subject, text_body=text, html_body=html)
