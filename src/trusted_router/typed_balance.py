"""Authoritative workspace balance reads.

Production money lives only in ``tr_credit_balance``. The JSON ``credit`` row is
metadata-only and must never be used as a monetary fallback. The in-memory test
store has its own single balance book, exposed through ``credit_money_snapshot``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from trusted_router.storage import STORE, typed_billing_store


class LiveCreditSummary(TypedDict):
    total_credits: int
    total_usage: int
    reserved: int
    available: int


def live_credit_summary(
    workspace_id: str,
    *,
    store: Any | None = None,
) -> LiveCreditSummary | None:
    """Return the live money counters for display/API reads.

    Production reads only the typed counter table. The in-memory single-book
    store reads its CreditMoney snapshot. Non-money JSON metadata remains the
    caller's responsibility.
    """
    active_store = STORE if store is None else store
    typed_store = typed_billing_store(active_store)
    if typed_store is not None:
        typed = typed_store.typed_credit_snapshot(workspace_id)
        if typed is None:
            return None
        return _summary(int(typed[0]), int(typed[1]), int(typed[2]))

    # InMemoryStore is the deterministic single-book test twin. Production
    # Spanner stores deliberately do not implement this method.
    snapshot_fn = getattr(active_store, "credit_money_snapshot", None)
    if snapshot_fn is not None:
        money = snapshot_fn(workspace_id)
        if money is None:
            return None
        return _summary(int(money[0]), int(money[1]), int(money[2]))
    return None


def _summary(total_credits: int, total_usage: int, reserved: int) -> LiveCreditSummary:
    return {
        "total_credits": total_credits,
        "total_usage": total_usage,
        "reserved": reserved,
        "available": max(0, total_credits - total_usage - reserved),
    }


# NOTE: the key-usage/remaining display overlay (typed_aware_key over tr_key_limit)
# is the immediate follow-up — it threads Settings into the /v1/keys + console key
# routes, so it lands as its own focused change.
