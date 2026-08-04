"""HOME side of deferred settlement on the native Spanner store.

A peer plane served a federated key's CREDITS traffic while this plane was
unreachable, recorded the spend as debt, and is now delivering it. This module
applies that debt to the real ledger — exactly once per
``(source_plane, authorization_id)`` — and answers every replay with the
RECORDED verdict.

Debit-only by construction: the single balance mutation is ``total_usage`` UP.
There is no code path here that can create spendable credits, which is what
makes deferred settlement safe to expose behind a per-peer token at all.

Same discipline as storage_gcp_credit_transfer: every write is DML with a
client timestamp (never PENDING_COMMIT_TIMESTAMP — the same-table PCT trap),
every decision reads inside the same serializable transaction it writes in,
and the insert-once claim row IS the verdict.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from trusted_router.storage_gcp_counter_dml import insert_entity_dml_at, update_entity_body_dml
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_io import run_in_transaction_with_retry

#: Insert-once verdict rows, one per (source_plane, authorization_id).
FEDERATED_SETTLEMENT_CLAIM_KIND = "federated_settlement_claim"
#: Per-(source_plane, workspace, UTC day) applied totals — the aggregate clamp.
FEDERATED_SETTLEMENT_WINDOW_KIND = "federated_settlement_window"

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _claim_id(source_plane: str, authorization_id: str) -> str:
    return f"{source_plane}:{authorization_id}"


def _window_id(source_plane: str, workspace_id: str, day: str) -> str:
    return f"{source_plane}:{workspace_id}:{day}"


def _read_entity_body(
    transaction: Any, param_types: Any, kind: str, entity_id: str
) -> dict[str, Any] | None:
    rows = list(
        transaction.execute_sql(
            "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
            params={"kind": kind, "id": entity_id},
            param_types={"kind": param_types.STRING, "id": param_types.STRING},
        )
    )
    if not rows:
        return None
    body = rows[0][0]
    return json.loads(body) if isinstance(body, str) else dict(body)


def _book_usage(
    transaction: Any, param_types: Any, workspace_id: str, cost: int, now: Any
) -> None:
    """total_usage += cost on shard 0, UNCONDITIONAL — and UPSERT-shaped.

    Deliberately no headroom predicate: the spend already happened on the
    peer while this plane was unreachable. Booking it into a negative
    available balance is the honest ledger; refusing to book it would lose
    the debit, which is the one failure money code cannot have.

    UPSERT, not a bare UPDATE, for the same reason the Postgres settle path
    learned in 2a: a zero-row UPDATE on a missing shard-0 row is SILENT, and
    treating it as "workspace unknown" turns a deleted/unbackfilled balance
    row into a terminal dead-letter on the peer — a valid debit dropped
    forever. Workspace existence is the CALLER's check, against the
    workspace entity itself; by the time we are here, the spend books, into
    a recreated zero-credit row if it must.
    """
    updated = transaction.execute_update(
        f"UPDATE {CREDIT_BALANCE_TABLE} SET total_usage = total_usage + @amt "  # noqa: S608 - constant table name
        "WHERE workspace_id = @ws AND shard = 0",
        params={"amt": cost, "ws": workspace_id},
        param_types={"amt": param_types.INT64, "ws": param_types.STRING},
    )
    if updated == 1:
        return
    log.error(
        "apply_federated_usage: workspace %s exists but has no shard-0 balance "
        "row; recreating it at zero credits so the debit cannot vanish",
        workspace_id,
    )
    transaction.execute_update(
        f"INSERT INTO {CREDIT_BALANCE_TABLE} "  # noqa: S608 - constant table name
        "(workspace_id, shard, total_credits, total_usage, reserved, updated_at) "
        "VALUES (@ws, 0, 0, @amt, 0, @now)",
        params={"amt": cost, "ws": workspace_id, "now": now},
        param_types={
            "amt": param_types.INT64,
            "ws": param_types.STRING,
            "now": param_types.TIMESTAMP,
        },
    )


def apply_federated_usage(
    database: Any,
    param_types: Any,
    *,
    source_plane: str,
    authorization_id: str,
    workspace_id: str,
    cost_microdollars: int,
    daily_cap_microdollars: int,
) -> str:
    """Apply one peer-recorded settlement. Returns the verdict string.

    applied | already | conflict | workspace_unknown | clamped — the same
    vocabulary as the InMemory twin; the route maps them to structured HTTP
    verdicts the peer's forwarder classifies on.

    Ordering inside the transaction is load-bearing:
      1. claim read      — a replay must return the recorded verdict without
                           touching anything else;
      2. clamp read      — checked BEFORE any write, so a clamped row leaves
                           no residue and can apply cleanly tomorrow;
      3. usage booking   — the row count is the workspace-existence check,
                           and a zero-row booking aborts the transaction
                           before the claim exists;
      4. claim + window  — written last, so they can never exist without the
                           booking they describe.

    The window is keyed by THIS plane's clock. A peer-supplied timestamp
    choosing its own window would let a compromised peer spread a burst
    across arbitrary days.
    """
    cost = int(cost_microdollars)
    if cost <= 0:
        raise ValueError("cost_microdollars must be positive")
    now = _now()
    day = now.date().isoformat()
    claim_id = _claim_id(source_plane, authorization_id)
    window_id = _window_id(source_plane, workspace_id, day)

    def txn(transaction: Any) -> str:
        existing = _read_entity_body(
            transaction, param_types, FEDERATED_SETTLEMENT_CLAIM_KIND, claim_id
        )
        if existing is not None:
            if (
                existing.get("workspace_id") == workspace_id
                and int(existing.get("cost_microdollars", -1)) == cost
            ):
                return "already"
            return "conflict"

        window = _read_entity_body(
            transaction, param_types, FEDERATED_SETTLEMENT_WINDOW_KIND, window_id
        )
        applied_today = int((window or {}).get("applied_microdollars", 0))
        if applied_today + cost > int(daily_cap_microdollars):
            return "clamped"

        # Workspace existence is checked against the workspace ENTITY, never
        # inferred from the balance UPDATE's row count. A missing shard-0
        # balance row on a live workspace is drift to repair (book into a
        # recreated row), not a verdict — the peer dead-letters
        # workspace_unknown terminally, so conflating the two drops a valid
        # debit forever.
        if _read_entity_body(transaction, param_types, "workspace", workspace_id) is None:
            return "workspace_unknown"
        _book_usage(transaction, param_types, workspace_id, cost, now)

        insert_entity_dml_at(
            transaction,
            param_types,
            FEDERATED_SETTLEMENT_CLAIM_KIND,
            claim_id,
            json.dumps(
                {
                    "source_plane": source_plane,
                    "authorization_id": authorization_id,
                    "workspace_id": workspace_id,
                    "cost_microdollars": cost,
                    "applied_at": now.isoformat().replace("+00:00", "Z"),
                },
                separators=(",", ":"),
            ),
            now,
        )
        window_body = json.dumps(
            {
                "source_plane": source_plane,
                "workspace_id": workspace_id,
                "day": day,
                "applied_microdollars": applied_today + cost,
            },
            separators=(",", ":"),
        )
        if window is None:
            insert_entity_dml_at(
                transaction,
                param_types,
                FEDERATED_SETTLEMENT_WINDOW_KIND,
                window_id,
                window_body,
                now,
            )
        else:
            update_entity_body_dml(
                transaction,
                param_types,
                FEDERATED_SETTLEMENT_WINDOW_KIND,
                window_id,
                window_body,
                now,
            )
        return "applied"

    return run_in_transaction_with_retry(database, txn)
