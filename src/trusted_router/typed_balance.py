"""Typed-aware balance reads.

After the 2026-06-25 ownership split, a typed workspace's authoritative
`reserved` / `total_usage` (credit) and `usage` / `byok_usage` / `reserved` (key)
live in the typed Spanner tables (tr_credit_balance / tr_key_limit), booked by
the typed authorize/finalize DML. The JSON `credit` / `api_key` rows are
intentionally stale for those columns once a workspace is typed (only
total_credits / key config are mirrored back). So any DISPLAY or DECISION that
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

import dataclasses
from typing import Any, TypedDict

from trusted_router.storage import STORE, typed_billing_store
from trusted_router.storage_models import CreditAccount


class LiveCreditSummary(TypedDict):
    total_credits: int
    total_usage: int
    reserved: int
    available: int


def typed_aware_credit_account(
    store: Any, workspace_id: str, *, settings: Any
) -> CreditAccount | None:
    """Return the workspace's CreditAccount with total_credits/total_usage/
    reserved overlaid from the typed table when the workspace is typed. JSON
    metadata (auto-refill config, stripe ids) is preserved. No-op for legacy
    workspaces, stores without the typed capability, or a not-yet-seeded typed
    row.

    The typed read goes through the TypedBillingStore capability (a point-read
    method on the store) instead of reaching into the store's private Spanner
    handles — the reason isinstance(store, TypedBillingStore) replaced the old
    hasattr(_database) probe (#39)."""
    account = store.get_credit_account(workspace_id)
    typed_store = typed_billing_store(store)
    if account is None or typed_store is None:
        return account
    typed = typed_store.typed_credit_snapshot(workspace_id)
    if typed is None:
        return account  # typed-capable store, but no row yet — JSON is the best estimate
    return dataclasses.replace(
        account,
        total_credits_microdollars=int(typed[0]),
        total_usage_microdollars=int(typed[1]),
        reserved_microdollars=int(typed[2]),
    )


def live_credit_summary(
    workspace_id: str,
    *,
    store: Any | None = None,
) -> LiveCreditSummary | None:
    """Return the live money counters for display/API reads.

    A typed counter row wins whenever it exists. Workspaces without a typed row
    yet, plus the in-memory single-book store, fall back to the JSON
    CreditAccount. Non-money JSON metadata remains the caller's responsibility.
    """
    active_store = STORE if store is None else store
    typed_store = typed_billing_store(active_store)
    if typed_store is not None:
        typed = typed_store.typed_credit_snapshot(workspace_id)
        if typed is not None:
            return _summary(int(typed[0]), int(typed[1]), int(typed[2]))

    account = active_store.get_credit_account(workspace_id)
    if account is None:
        return None
    return _summary(
        account.total_credits_microdollars,
        account.total_usage_microdollars,
        account.reserved_microdollars,
    )


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
