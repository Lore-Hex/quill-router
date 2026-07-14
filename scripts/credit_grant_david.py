"""One-shot $40 credit grant for david@superstruct.tech (manual top-up).

User (Joseph) asked on 2026-07-07 to give this account $40 of credit.

Uses STORE.credit_workspace_once with a deterministic event_id so re-running
is a clean no-op (returns False). Verifies the typed counter after.

Usage:
    cd /Users/jperla/claude/qr-billing
    PYTHONPATH=src uv run python scripts/credit_grant_david.py
"""

from __future__ import annotations

import os

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

EMAIL = "david@superstruct.tech"
AMOUNT_DOLLARS = 40
AMOUNT_MICRODOLLARS = AMOUNT_DOLLARS * MICRODOLLARS_PER_DOLLAR
EVENT_ID = "manual_grant_david_superstruct_2026-07-07_forty_dollars"


def _typed_total_credits(workspace_id: str) -> int | None:
    pt = STORE._param_types
    with STORE._database.snapshot() as snap:
        rows = list(snap.execute_sql(
            "SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@w AND shard=0",
            params={"w": workspace_id}, param_types={"w": pt.STRING},
        ))
    return int(rows[0][0]) if rows else None


def main() -> int:
    user = STORE.find_user_by_email(EMAIL)
    if user is None:
        print(f"ERROR: no user found with email {EMAIL!r}")
        return 1
    print(f"user: id={user.id} email={user.email}")

    workspaces = STORE.list_workspaces_for_user(user.id)
    if not workspaces:
        print(f"ERROR: user {user.id} has no workspaces")
        return 1
    workspace = workspaces[0]
    print(f"granting ${AMOUNT_DOLLARS} ({AMOUNT_MICRODOLLARS} u$) to workspace {workspace.id} ({workspace.name!r})")

    before = STORE.get_credit_account(workspace.id)
    if before is None:
        print(f"ERROR: credit account for workspace {workspace.id} not found")
        return 1
    typed_before = _typed_total_credits(workspace.id)
    print(f"  typed deposited before: ${(typed_before or 0) / MICRODOLLARS_PER_DOLLAR:.2f}")

    granted = STORE.credit_workspace_once(workspace.id, AMOUNT_MICRODOLLARS, EVENT_ID)
    print("  credit applied" if granted else f"  no-op: event {EVENT_ID!r} already applied")

    typed_after = _typed_total_credits(workspace.id)
    print(f"  typed deposited after: ${(typed_after or 0) / MICRODOLLARS_PER_DOLLAR:.2f}")
    expected = (typed_before or 0) + (AMOUNT_MICRODOLLARS if granted else 0)
    if typed_after != expected:
        print(f"  WARNING: typed counter {typed_after} did not match expected {expected}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
