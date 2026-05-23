"""One-shot credit-grant for Gabriella's workspace (2026-05-22 makeup).

Reason: Stripe webhook was firing on $5+$2 checkouts (verified via
events evt_1TaKM6QuWGscJyRc and evt_1TaKNXQuWGscJyRc) but TR's
handler was not crediting the ledger. Customer paid, balance stayed
$0. Granting $107 manually ($5 + $2 makeup + $100 goodwill) via the
production Spanner backend's `credit_workspace_once`, which is the
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
    print(f"before: total_credits_microdollars = {before.total_credits_microdollars}")

    granted = STORE.credit_workspace_once(
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
        # Not an error; idempotent.
        return 0

    after = STORE.get_credit_account(WORKSPACE_ID)
    if after is None:
        print(
            "ERROR: credit_workspace_once returned True but get_credit_account "
            "returned None on re-read. Inconsistent state.",
            file=sys.stderr,
        )
        return 2

    delta = after.total_credits_microdollars - before.total_credits_microdollars
    print(
        f"after:  total_credits_microdollars = {after.total_credits_microdollars} "
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
