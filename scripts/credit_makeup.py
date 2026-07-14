"""One-shot credit-grant for Gabriella's workspace (2026-05-22 makeup).

Reason: Stripe webhook was firing on $5+$2 checkouts (verified via
events evt_1TaKM6QuWGscJyRc and evt_1TaKNXQuWGscJyRc) but TR's
handler was not crediting the ledger. Customer paid, balance stayed
$0. Granting $107 manually ($5 + $2 makeup + $100 goodwill) via the
production Spanner backend's `credit_workspace_typed_direct`, which is the
SAME code path the working webhook would have used — so the credit
appears in the ledger indistinguishably from a real grant + the
stripe_event row is recorded for idempotency.

Idempotent: re-running this script after success returns False
(the deterministic event_id is already in stripe_events table).

Usage:
    cd /Users/jperla/claude/quill-router
    uv run python scripts/credit_makeup.py
"""

from __future__ import annotations

import os
import sys

# Force production storage backend regardless of local .env state.
os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import create_store

STORE = create_store(Settings())

WORKSPACE_ID = "50842583-5803-4966-a190-1c09d331ebaa"
AMOUNT_DOLLARS = 107
AMOUNT_MICRODOLLARS = AMOUNT_DOLLARS * MICRODOLLARS_PER_DOLLAR
EVENT_ID = "manual_makeup_2026-05-22_evt_1TaKM6QuWGscJyRc_plus_2_plus_100"


def _typed_total_credits(workspace_id: str) -> int | None:
    pt = STORE._param_types
    with STORE._database.snapshot() as snap:
        rows = list(snap.execute_sql(
            "SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@w AND shard=0",
            params={"w": workspace_id},
            param_types={"w": pt.STRING},
        ))
    return int(rows[0][0]) if rows else None


def main() -> int:
    before = STORE.get_credit_account(WORKSPACE_ID)
    if before is None:
        print(
            f"ERROR: credit account for workspace {WORKSPACE_ID} not found. "
            f"The user may not have completed signup or the workspace id is wrong. "
            f"Aborting.",
            file=sys.stderr,
        )
        return 1
    typed_before = _typed_total_credits(WORKSPACE_ID)
    print(f"before: total_credits_microdollars = {before.total_credits_microdollars}")
    print(f"before: typed total_credits = {typed_before or 0}")

    granted = STORE.credit_workspace_typed_direct(
        workspace_id=WORKSPACE_ID,
        amount_microdollars=AMOUNT_MICRODOLLARS,
        event_id=EVENT_ID,
    )

    if not granted:
        print(
            f"NO-OP: event_id '{EVENT_ID}' already recorded — credit was already "
            f"granted on a prior run.",
            file=sys.stderr,
        )
        typed_after = _typed_total_credits(WORKSPACE_ID)
        if typed_after != typed_before:
            print(
                f"WARN: no-op changed typed total from {typed_before} to {typed_after}",
                file=sys.stderr,
            )
            return 3
        return 0

    typed_after = _typed_total_credits(WORKSPACE_ID)
    delta = (typed_after or 0) - (typed_before or 0)
    print(
        f"after:  typed total_credits = {typed_after or 0} "
        f"(+{delta:,} microdollars = +${delta / MICRODOLLARS_PER_DOLLAR:.2f})"
    )
    if delta != AMOUNT_MICRODOLLARS:
        print(
            f"WARN: delta {delta} != expected {AMOUNT_MICRODOLLARS}. "
            f"Re-read race or concurrent write?",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
