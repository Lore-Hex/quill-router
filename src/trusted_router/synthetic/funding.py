"""Self-funding for the synthetic monitor workspace.

Each deployment runs its own database, so the monitor workspace on each cloud
must be funded on each cloud. When it runs dry the deep inference probes fail
with 402 — and a monitor that cannot pay for a model call proves nothing about
the model path. That is exactly how AWS and Azure served green status pages
while every ``openai_sdk_pong``/``responses_pong`` probe failed (2026-08-10).

The fix is a monthly, idempotent, config-as-code grant applied lazily on the
monitor's own authorize calls: no cron, no per-cloud manual step, no way to
forget a deployment. ``credit_workspace_typed_direct`` is idempotent per
event id, so replays and racing processes cannot double-grant; the in-process
marker only saves the ledger round-trip after the first check of the month.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.security import lookup_hash_api_key

logger = logging.getLogger(__name__)

# (workspace_id, "YYYY-MM") pairs already ensured by THIS process. Purely a
# round-trip saver: correctness comes from the ledger's per-event idempotency.
_ENSURED: set[tuple[str, str]] = set()

# Cache of lookup_hash(settings.synthetic_monitor_api_key) so the authorize
# hot path never re-hashes for non-monitor traffic. Keyed by the raw setting
# value so config changes (tests, rotation) invalidate naturally.
_MONITOR_HASH: dict[str, str] = {}


def monitor_lookup_hash(settings: Settings) -> str | None:
    key = settings.synthetic_monitor_api_key
    if not key:
        return None
    cached = _MONITOR_HASH.get(key)
    if cached is None:
        cached = lookup_hash_api_key(key)
        _MONITOR_HASH[key] = cached
    return cached


def ensure_monitor_funding(
    store: Any,
    settings: Settings,
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Grant this month's monitor budget if it has not been granted yet.

    Returns True only when a grant was actually applied. Safe to call on every
    monitor authorize: after the first call of the month it is one set lookup.
    """
    dollars = settings.synthetic_monitor_monthly_grant_dollars
    if dollars <= 0:
        return False
    month = (now or datetime.now(UTC)).strftime("%Y-%m")
    marker = (workspace_id, month)
    if marker in _ENSURED:
        return False
    # The event id carries the month, so each month is exactly one grant per
    # workspace no matter how many processes race here.
    event_id = f"synthetic-monitor-grant-{month}"
    granted = bool(
        store.credit_workspace_typed_direct(
            workspace_id, dollars_to_microdollars(dollars), event_id
        )
    )
    _ENSURED.add(marker)
    if granted:
        logger.info(
            "synthetic monitor funded: workspace=%s month=%s amount_usd=%s",
            workspace_id,
            month,
            dollars,
        )
    return granted


def reset_for_tests() -> None:
    _ENSURED.clear()
    _MONITOR_HASH.clear()
