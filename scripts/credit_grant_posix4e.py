"""One-shot $20 credit grant for alex@fralex.art (manual top-up).

User (Joseph) asked to gift $20 of credit to the account that ran the
difficulty-response-curves benchmark and reported the max_completion_tokens
truncation bug (2026-07-06). That account is the "bench" API key on workspace
ea7dd3d8 (owner alex@fralex.art / github posix4e).

Uses STORE.credit_workspace_once with a deterministic event_id so re-running
is a clean no-op (returns False). Mirrors through to the typed counter under
TR_TYPED_COUNTER_MIRROR=1. Verifies the typed counter after.

Usage:
    cd /Users/jperla/claude/qr-billing
    PYTHONPATH=src uv run python scripts/credit_grant_posix4e.py
"""

from __future__ import annotations

import os

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")
os.environ["TR_TYPED_COUNTER_MIRROR"] = "1"

from trusted_router.config import Settings
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import create_store

STORE = create_store(Settings())

EMAIL = "alex@fralex.art"
AMOUNT_DOLLARS = 20
AMOUNT_MICRODOLLARS = AMOUNT_DOLLARS * MICRODOLLARS_PER_DOLLAR
EVENT_ID = "manual_grant_posix4e_2026-07-06_twenty_dollars"


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
    print(f"  JSON deposited before:  ${before.total_credits_microdollars / MICRODOLLARS_PER_DOLLAR:.2f}")
    print(f"  typed deposited before: ${(_typed_total_credits(workspace.id) or 0) / MICRODOLLARS_PER_DOLLAR:.2f}")

    granted = STORE.credit_workspace_once(workspace.id, AMOUNT_MICRODOLLARS, EVENT_ID)
    print("  credit applied" if granted else f"  no-op: event {EVENT_ID!r} already applied")

    after = STORE.get_credit_account(workspace.id)
    typed_after = _typed_total_credits(workspace.id)
    print(f"  JSON deposited after:  ${after.total_credits_microdollars / MICRODOLLARS_PER_DOLLAR:.2f}")
    print(f"  typed deposited after: ${(typed_after or 0) / MICRODOLLARS_PER_DOLLAR:.2f}")
    if typed_after != after.total_credits_microdollars:
        print("  WARNING: typed counter did not match JSON - check the mirror!")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
