"""Typed-aware balance reads.

After the 2026-06-25 ownership split, a typed workspace's authoritative
`reserved` / `total_usage` (credit) and `usage` / `byok_usage` / `reserved` (key)
live in the typed Spanner tables (tr_credit_balance / tr_key_limit), booked by
the typed authorize/finalize DML. The JSON `credit` / `api_key` rows are
intentionally stale for those columns once a workspace is typed (only
typed snapshots are live). So any DISPLAY or DECISION that
reads the JSON counters — the console balance, `/credits`, auto-refill, key
usage/remaining — would see a stale-LOW usage and an overstated available
balance (e.g. auto-refill would never fire → the card is never charged →
underbill). These helpers overlay the authoritative typed counters onto the
JSON-loaded object for typed workspaces, and are a no-op for legacy workspaces or
stores without typed tables. After C1, typed-overlay eligibility is capability
based: a typed store with a typed row wins, and the in-memory store remains the
single-book test twin.
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

    A typed counter row wins whenever it exists. The in-memory single-book store
    falls back to its CreditMoney snapshot. Non-money JSON metadata remains the
    caller's responsibility.
    """
    active_store = STORE if store is None else store
    typed_store = typed_billing_store(active_store)
    if typed_store is not None:
        typed = typed_store.typed_credit_snapshot(workspace_id)
        if typed is not None:
            return _summary(int(typed[0]), int(typed[1]), int(typed[2]))

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
