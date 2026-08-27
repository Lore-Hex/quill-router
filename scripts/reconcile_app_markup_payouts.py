"""Reconcile settled app markup against its exactly-once owner payouts.

Read-only by default; pass ``--apply`` to credit missing payouts through the
same deterministic movement id used by inline settlement.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from reconcile_custom_model_payouts import (
    _movement_from_row,
    _parse_iso,
    _scan_authorizations,
    _store_target,
)

from trusted_router.app_markup_billing import (
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.config import Settings
from trusted_router.storage import create_store


def reconcile_app_markup_payouts(store: Any, *, since: datetime, apply: bool = False) -> int:
    movements = {
        (movement.account_id, movement.movement_id)
        for movement in _scan_app_payout_movements(store, since)
    }
    missing = 0
    for authorization in _scan_authorizations(store, since):
        if (
            authorization.finalization_outcome != "settled"
            or not authorization.app_id
            or not authorization.app_owner_user_id
            or authorization.app_markup_basis_points <= 0
            or not authorization.finalized_generation_id
        ):
            continue
        generation = store.get_generation(authorization.finalized_generation_id)
        if generation is None or generation.app_markup_microdollars <= 0:
            continue
        amount = app_markup_owner_share_microdollars(generation.app_markup_microdollars)
        event_id = app_markup_payout_event_id(authorization.id)
        account_id = f"user:{authorization.app_owner_user_id}"
        if amount <= 0 or (account_id, event_id) in movements:
            continue
        missing += 1
        applied = apply and bool(
            store.credit_user_earnings(
                authorization.app_owner_user_id,
                amount,
                event_id,
                custom_model_id=authorization.app_id,
                payer_workspace_id=authorization.workspace_id,
            )
        )
        print(
            json.dumps(
                {
                    "type": "missing_app_markup_payout",
                    "authorization_id": authorization.id,
                    "app_id": authorization.app_id,
                    "amount_microdollars": amount,
                    "applied": applied,
                },
                sort_keys=True,
            )
        )
    return missing


def _scan_app_payout_movements(store: Any, since: datetime) -> list[Any]:
    target = _store_target(store)
    if hasattr(target, "credit_movements"):
        return [
            movement
            for movement in target.credit_movements.values()
            if movement.kind == "app_markup_payout" and _parse_iso(movement.created_at) >= since
        ]
    query = (
        "SELECT account_id, movement_id, kind, amount_microdollars, "
        "counterparty_account_id, custom_model_id, authorization_id, created_at "
        "FROM tr_credit_movement WHERE kind='app_markup_payout' AND created_at>=@since"
    )
    if hasattr(target, "_database") and hasattr(target, "_param_types"):
        with target._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                query,
                params={"since": since},
                param_types={"since": target._param_types.TIMESTAMP},
            )
            return [_movement_from_row(row) for row in rows]
    if hasattr(target, "_run_transaction"):
        def read(conn: Any) -> list[Any]:
            rows = conn.execute(
                query.replace("@since", "%s"),
                (since,),
            ).fetchall()
            return [_movement_from_row(row) for row in rows]

        return list(target._run_transaction(read))
    raise TypeError(f"unsupported reconciliation store: {type(target).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    since = args.since or datetime.now(UTC) - timedelta(hours=48)
    missing = reconcile_app_markup_payouts(
        create_store(Settings()), since=since, apply=args.apply
    )
    print(json.dumps({"type": "summary", "missing_payouts": missing, "apply": args.apply}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
