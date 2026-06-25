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
stores without typed tables.

Routing decision = the same cohort gate the authorize path uses
(typed_billing_enabled_for_workspace), evaluated from the caller's Settings.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from trusted_router.storage_gcp_authorize import typed_billing_enabled_for_workspace
from trusted_router.storage_models import CreditAccount


def _typed_enabled(workspace_id: str, settings: Any) -> bool:
    return typed_billing_enabled_for_workspace(
        workspace_id,
        allowlist_csv=settings.typed_billing_workspace_ids,
        denylist_csv=settings.typed_billing_workspace_denylist,
    )


def _has_typed_tables(store: Any) -> bool:
    # GCP store only; InMemory has no typed Spanner tables.
    return hasattr(store, "_database") and hasattr(store, "_param_types")


def _read_typed_credit(store: Any, workspace_id: str) -> tuple | None:
    pt = store._param_types
    with store._database.snapshot() as snap:
        rows = list(snap.execute_sql(
            "SELECT total_credits, total_usage, reserved FROM tr_credit_balance "
            "WHERE workspace_id=@pk AND shard=0",
            params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
    return tuple(rows[0]) if rows else None


def typed_aware_credit_account(
    store: Any, workspace_id: str, *, settings: Any
) -> CreditAccount | None:
    """Return the workspace's CreditAccount with total_credits/total_usage/
    reserved overlaid from the typed table when the workspace is typed. JSON
    metadata (auto-refill config, stripe ids) is preserved. No-op for legacy
    workspaces, stores without typed tables, or a not-yet-seeded typed row."""
    account = store.get_credit_account(workspace_id)
    if account is None or not _has_typed_tables(store) or not _typed_enabled(workspace_id, settings):
        return account
    typed = _read_typed_credit(store, workspace_id)
    if typed is None:
        return account  # typed enforcement on but row not seeded yet — JSON is the best estimate
    return dataclasses.replace(
        account,
        total_credits_microdollars=int(typed[0]),
        total_usage_microdollars=int(typed[1]),
        reserved_microdollars=int(typed[2]),
    )

# NOTE: the key-usage/remaining display overlay (typed_aware_key over tr_key_limit)
# is the immediate follow-up — it threads Settings into the /v1/keys + console key
# routes, so it lands as its own focused change.
