"""Authorize-time billing-pause evidence shared by the typed and legacy paths.

``Settings.spend_lease_trust_eligibility_enabled`` arms this gate (the name
predates the spend-lease pilot's removal). The typed authorize transaction and
the legacy entity path both read the pause state inside their own transaction
so a pause committed concurrently conflicts with, and rejects, the hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def billing_paused_row(row: Sequence[Any]) -> bool:
    """Interpret pause evidence identically for SELECT and returning DML.

    The epoch is conflict evidence only; its value is not an authorize input.
    Preserve the historical string predicate, including NULL and empty arrays.
    """
    return str(row[0] or "") not in ("", "[]")


def billing_paused_tx(
    reader: Any, pt: Any, workspace_id: str, *, shard: int | None = None
) -> bool:
    # Reading the epoch establishes a conflict even if a pause was cleared
    # before this transaction retries. A new authorization takes no holds.
    params: dict[str, Any] = {"ws": workspace_id}
    types = {"ws": pt.STRING}
    suffix = ""
    if shard is not None:
        suffix = " AND shard=@shard"
        params["shard"] = shard
        types["shard"] = pt.INT64
    rows = reader.execute_sql(
        "SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance WHERE workspace_id=@ws" + suffix,  # noqa: S608 - fixed shard clause
        params=params,
        param_types=types,
    )
    return any(billing_paused_row(row) for row in rows)
