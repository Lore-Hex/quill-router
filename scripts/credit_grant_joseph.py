"""One-shot credit-grant for joseph@jperla.com (TR account top-up).

User asked for $1 of credit on their personal TR account during a
debug session — they hit 402 in the chat playground because their
workspace ran out of credit, added $25 via the normal billing path,
then asked separately for $1 manual makeup.

Uses STORE.credit_workspace_once with a deterministic event_id so
re-running is a clean no-op (returns False).

Usage:
    cd /Users/jperla/claude/quill-router
    uv run python scripts/credit_grant_joseph.py
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

EMAIL = "joseph@jperla.com"
AMOUNT_DOLLARS = 1
AMOUNT_MICRODOLLARS = AMOUNT_DOLLARS * MICRODOLLARS_PER_DOLLAR
EVENT_ID = "manual_grant_joseph_2026-06-02_one_dollar"


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
    print(f"workspaces: {[(w.id, w.name) for w in workspaces]}")

    # Default to the first workspace (typically the personal one
    # created at signup). Surface in stdout so we can verify before
    # the credit lands.
    workspace = workspaces[0]
    print(f"granting ${AMOUNT_DOLLARS} ({AMOUNT_MICRODOLLARS} μ$) to workspace {workspace.id}")

    before = STORE.get_credit_account(workspace.id)
    if before is None:
        print(f"ERROR: credit account for workspace {workspace.id} not found")
        return 1
    print(f"  balance before: ${before.balance_microdollars / MICRODOLLARS_PER_DOLLAR:.4f}")

    granted = STORE.credit_workspace_once(
        workspace.id, AMOUNT_MICRODOLLARS, EVENT_ID
    )
    if not granted:
        print(f"  no-op: event {EVENT_ID!r} already applied")
    else:
        print("  credit applied")

    after = STORE.get_credit_account(workspace.id)
    print(f"  balance after:  ${after.balance_microdollars / MICRODOLLARS_PER_DOLLAR:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
