"""Grant prepaid credit through the authoritative typed ledger.

Dry-run is the default. ``--event-id`` is required and makes retries idempotent.

Example:
  uv run python scripts/grant_credit.py \
    --email user@example.com --amount 100 --event-id manual_grant_2026_07_17 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars, format_money_precise
from trusted_router.storage import create_store
from trusted_router.typed_balance import LiveCreditSummary, live_credit_summary


def _select_workspace(store: Any, email: str, workspace_id: str | None) -> Any:
    user = store.find_user_by_email(email)
    if user is None:
        raise ValueError(f"no user found for {email}")
    workspaces = store.list_workspaces_for_user(user.id)
    if workspace_id is not None:
        matches = [workspace for workspace in workspaces if workspace.id == workspace_id]
        if len(matches) != 1:
            raise ValueError("workspace is not accessible to that user")
        return matches[0]
    if len(workspaces) == 1:
        return workspaces[0]
    personal = [workspace for workspace in workspaces if workspace.name == "Personal Workspace"]
    if len(personal) == 1:
        return personal[0]
    raise ValueError("user has multiple workspaces; pass --workspace-id explicitly")


def _print_summary(label: str, summary: LiveCreditSummary) -> None:
    print(
        f"{label}: available={format_money_precise(summary['available'])} "
        f"credits={format_money_precise(summary['total_credits'])} "
        f"usage={format_money_precise(summary['total_usage'])} "
        f"reserved={format_money_precise(summary['reserved'])}"
    )


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--amount", required=True, help="positive USD amount")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--workspace-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2
    try:
        amount = dollars_to_microdollars(args.amount)
        if amount <= 0:
            raise ValueError("amount must be positive")
        active_store = create_store(Settings()) if store is None else store
        workspace = _select_workspace(active_store, args.email, args.workspace_id)
        before = live_credit_summary(workspace.id, store=active_store)
        if before is None:
            raise ValueError("authoritative credit account not found")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"workspace: {workspace.id} ({workspace.name})")
    _print_summary("before", before)
    print(f"grant: {format_money_precise(amount)} event_id={args.event_id}")
    if not args.apply:
        print("DRY-RUN: no credit granted; pass --apply after reviewing the target")
        return 0

    granted = active_store.credit_workspace_typed_direct(workspace.id, amount, args.event_id)
    after = live_credit_summary(workspace.id, store=active_store)
    if after is None:
        print("ERROR: authoritative credit account disappeared after grant", file=sys.stderr)
        return 1
    print("result: applied" if granted else "result: no-op (event already applied)")
    _print_summary("after", after)
    if granted and after["total_credits"] < before["total_credits"] + amount:
        print("ERROR: typed total credits did not reflect the grant", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
