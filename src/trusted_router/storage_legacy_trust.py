"""Pause rejection and debt-aware release for the synchronous fallback."""
from __future__ import annotations

import json
from typing import Any


class BillingPausedError(ValueError):
    def __init__(self) -> None:
        super().__init__("billing_paused")


def postgres_pause(conn: Any, workspace_id: str) -> tuple[bool, int]:
    rows = conn.execute(
        "SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance "
        "WHERE workspace_id = %s FOR UPDATE", (workspace_id,), prepare=False,
    ).fetchall()
    return (any(str(row[0] or '') not in {'', '[]'} for row in rows),
            max((int(row[1] or 0) for row in rows), default=0))


def recover_released_postgres(conn: Any, workspace_id: str, store: Any) -> None:
    """Consume newly available credit in the same transaction as a release."""
    rows = conn.execute(
        "SELECT event_id, recovered_micro, unrecovered_micro FROM tr_trust_event "
        "WHERE workspace_id = %s AND kind = 'payment' AND unrecovered_micro > 0 "
        "ORDER BY occurred_at, event_id FOR UPDATE", (workspace_id,), prepare=False,
    ).fetchall()
    for event_id, recovered, debt in rows:
        balance = conn.execute(
            "SELECT total_credits, total_usage, reserved FROM tr_credit_balance "
            "WHERE workspace_id = %s AND shard = 0 FOR UPDATE", (workspace_id,), prepare=False,
        ).fetchone()
        if balance is None:
            return
        take = min(int(debt), max(0, int(balance[0]) - int(balance[1]) - int(balance[2])))
        if not take:
            break
        conn.execute("UPDATE tr_credit_balance SET total_credits = total_credits - %s "
                     "WHERE workspace_id = %s AND shard = 0", (take, workspace_id), prepare=False)
        conn.execute("UPDATE tr_trust_event SET recovered_micro = %s, unrecovered_micro = %s, "
                     "debit_status = %s WHERE workspace_id = %s AND event_id = %s",
                     (int(recovered or 0) + take, int(debt) - take,
                      'debited' if take == int(debt) else 'partial', workspace_id, event_id), prepare=False)

    remaining = conn.execute("SELECT event_id FROM tr_trust_event WHERE workspace_id = %s "
                             "AND kind = 'payment' AND unrecovered_micro > 0", (workspace_id,)).fetchall()
    if not remaining and rows:
        from trusted_router.storage_models import Workspace
        workspace = store._read_entity_tx(conn, "workspace", workspace_id, Workspace, for_update=True)
        if workspace is not None:
            causes = sorted(set(workspace.billing_pause_causes) - {"principal_recovery"})
            conn.execute("UPDATE tr_credit_balance SET billing_pause_causes = %s::jsonb, "
                         "pause_epoch = pause_epoch + 1 WHERE workspace_id = %s", (json.dumps(causes), workspace_id))
            workspace.billing_pause_causes = causes
            workspace.billing_paused = bool(causes)
            store._write_entity_tx(conn, "workspace", workspace_id, workspace)


def create_spanner_legacy_authorization(keys: Any, authorization: Any, index_id: str | None) -> Any:
    """Defend the retired JSON writer, including BYOK callers with no reservation."""
    from trusted_router.storage_gcp_io import run_in_transaction_with_retry
    from trusted_router.storage_models import ApiKey, GatewayAuthorization
    from trusted_router.trust_eligibility import billing_paused_tx

    io = keys._io

    def create(tx: Any) -> Any:
        prior = io.read_entity_tx(tx, "gateway_authorization_idempotency", index_id, dict) if index_id else None
        if prior and prior.get("reason") == "billing_paused":
            return None
        if prior and prior.get("authorization_id"):
            existing = io.read_entity_tx(tx, "gateway_authorization", str(prior["authorization_id"]), GatewayAuthorization)
            if existing is not None:
                return existing
        if billing_paused_tx(tx, io.param_types, authorization.workspace_id):
            # C1 removed GCP's legacy credit reserve operation. New legacy
            # callers can hold only the legacy key counter (including BYOK).
            if authorization.credit_reservation_id is not None:
                raise RuntimeError("legacy credit reservation requires typed recovery")
            key = io.read_entity_tx(tx, "api_key", authorization.key_hash, ApiKey)
            if key is not None:
                key.reserved_microdollars = max(0, key.reserved_microdollars - authorization.estimated_microdollars)
                io.write_entity_tx(tx, "api_key", key.hash, key)
            if index_id:
                io.write_entity_tx(tx, "gateway_authorization_idempotency", index_id, {"reason": "billing_paused"})
            return None
        io.write_entity_tx(tx, "gateway_authorization", authorization.id, authorization)
        if index_id:
            io.write_entity_tx(tx, "gateway_authorization_idempotency", index_id, {"authorization_id": authorization.id})
        return authorization

    result = run_in_transaction_with_retry(io.database, create)
    if result is None:
        raise BillingPausedError()
    return result
