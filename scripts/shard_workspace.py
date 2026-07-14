"""Safely split or consolidate a workspace's credit and uncapped key rows.

The operation is intentionally two phase so a failed verification can never
silently resume billing:

  # Pause, drain-check, and atomically split. Re-run while parked if draining.
  python scripts/shard_workspace.py prepare --workspace WS --shards 16 --apply

  # Owner email is accepted only when it resolves to exactly one workspace.
  python scripts/shard_workspace.py status --owner-email owner@example.com --shards 16

  # Verify the committed shape + global billing invariants, then unpause.
  python scripts/shard_workspace.py finish --workspace WS --shards 16 --apply

Eligible uncapped API-key usage rows are split to the same count; capped keys
remain on one exact row. Reverse with the same commands and ``--shards 1``.
Without ``--apply`` every command is read-only. A failed prepare/finish always
leaves the workspace paused.
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
from trusted_router.storage import create_store
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.storage_gcp_counters import MAX_CREDIT_SHARDS
from trusted_router.storage_gcp_credit_shard_admin import (
    CreditReshardResult,
    inspect_credit_reshard,
    reshard_credit_account,
)
from trusted_router.storage_gcp_key_shard_admin import (
    KeyUsageReshardResult,
    inspect_key_usage_reshard,
    reshard_key_usage,
)
from trusted_router.storage_models import ApiKey


def _resolve_workspace(store: Any, args: argparse.Namespace) -> str:
    """Resolve the operator target without guessing among workspaces."""
    if args.workspace:
        return str(args.workspace)

    user = store.find_user_by_email(str(args.owner_email))
    if user is None:
        raise ValueError("owner email does not match a user")
    workspaces = [
        workspace
        for workspace in store.list_workspaces_for_user(user.id)
        if workspace.owner_user_id == user.id
    ]
    if len(workspaces) != 1:
        raise ValueError(
            f"owner has {len(workspaces)} workspaces; select one with --workspace"
        )
    return str(workspaces[0].id)


def _print_status(status: CreditReshardResult) -> None:
    print(
        f"{status.workspace_id}: current_shards={status.current_shard_count} "
        f"target_shards={status.target_shard_count} ready={status.ready} "
        f"applied={status.applied}"
    )
    print(
        "  typed totals: "
        f"credits={status.total_credits_micro} usage={status.total_usage_micro} "
        f"reserved={status.reserved_micro}"
    )
    print(
        "  open reservations: "
        f"typed={status.typed_open_reservations} legacy={status.legacy_open_reservations}"
    )
    for reason in status.reasons:
        print(f"  BLOCKED: {reason}")


def _print_key_status(status: KeyUsageReshardResult) -> None:
    print(
        f"  key {status.key_hash}: current_shards={status.current_shard_count} "
        f"target_shards={status.target_shard_count} ready={status.ready} "
        f"applied={status.applied} usage={status.usage_micro} "
        f"byok_usage={status.byok_usage_micro} reserved={status.reserved_micro}"
    )
    for reason in status.reasons:
        print(f"    BLOCKED: {reason}")


def _key_target(key: ApiKey, requested_shards: int) -> int:
    has_limit = any(
        value is not None
        for value in (
            key.limit_microdollars,
            key.limit_daily_microdollars,
            key.limit_weekly_microdollars,
            key.limit_monthly_microdollars,
        )
    )
    return 1 if has_limit else requested_shards


def _prepare_keys(store: Any, workspace_id: str, requested_shards: int) -> bool:
    clean = True
    for key in store.api_keys.list_for_workspace(workspace_id):
        target = _key_target(key, requested_shards)
        if target != requested_shards:
            print(f"  key {key.hash}: capped; keeping exact usage_shards=1")
        status = reshard_key_usage(store, key.hash, target, apply=True)
        _print_key_status(status)
        clean = clean and status.ready
    return clean


def _verify_keys(store: Any, workspace_id: str, requested_shards: int) -> bool:
    clean = True
    for key in store.api_keys.list_for_workspace(workspace_id):
        status = inspect_key_usage_reshard(
            store,
            key.hash,
            _key_target(key, requested_shards),
        )
        _print_key_status(status)
        clean = clean and status.ready
    return clean


def _pause(store: Any, workspace_id: str) -> bool:
    updated = store.update_workspace(
        workspace_id,
        billing_paused=True,
        billing_pause_reason="credit-row reshard prepare",
    )
    return updated is not None and bool(updated.billing_paused)


def run_status(store: Any, args: argparse.Namespace) -> int:
    _print_status(inspect_credit_reshard(store, args.workspace, args.shards))
    _verify_keys(store, args.workspace, args.shards)
    return 0


def run_prepare(store: Any, args: argparse.Namespace) -> int:
    if not args.apply:
        status = inspect_credit_reshard(store, args.workspace, args.shards)
        _print_status(status)
        _verify_keys(store, args.workspace, args.shards)
        print("DRY-RUN: would pause, wait for all holds, then atomically reshard")
        return 0
    if not _pause(store, args.workspace):
        print("ERROR: could not pause workspace", file=sys.stderr)
        return 1
    status = reshard_credit_account(store, args.workspace, args.shards, apply=True)
    _print_status(status)
    if not status.ready:
        print("Workspace remains paused. Re-run prepare after holds drain.")
        draining = any("drain" in reason or "open" in reason for reason in status.reasons)
        return 2 if draining else 1
    if not _prepare_keys(store, args.workspace, args.shards):
        print("Workspace remains paused because an API-key reshard failed.", file=sys.stderr)
        return 1
    audit = audit_typed_invariants(store)
    print(audit.summary())
    if not audit.clean:
        print("Workspace remains paused because the invariant audit failed.", file=sys.stderr)
        return 1
    print(
        "Prepared and verified. Workspace remains paused; run finish with the "
        "same --shards value to resume billing."
    )
    return 0


def run_finish(store: Any, args: argparse.Namespace) -> int:
    status = inspect_credit_reshard(store, args.workspace, args.shards)
    _print_status(status)
    if not status.ready:
        print("ERROR: refusing to unpause; reshard verification is not clean", file=sys.stderr)
        return 1
    if not _verify_keys(store, args.workspace, args.shards):
        print("ERROR: refusing to unpause; API-key shard verification failed", file=sys.stderr)
        return 1
    audit = audit_typed_invariants(store)
    print(audit.summary())
    if not audit.clean:
        print("ERROR: refusing to unpause; invariant audit failed", file=sys.stderr)
        return 1
    if not args.apply:
        print("DRY-RUN: would unpause this verified workspace")
        return 0
    updated = store.update_workspace(
        args.workspace,
        billing_paused=False,
        billing_pause_reason="",
    )
    if updated is None or updated.billing_paused:
        print("ERROR: failed to verify workspace unpause", file=sys.stderr)
        return 1
    print(
        f"{args.workspace}: unpaused with {args.shards} credit shards and "
        "eligible key usage shards"
    )
    return 0


def _shard_count_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("shards must be an integer") from exc
    if value < 1 or value > MAX_CREDIT_SHARDS:
        raise argparse.ArgumentTypeError(
            f"shards must be between 1 and {MAX_CREDIT_SHARDS}"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("status", run_status),
        ("prepare", run_prepare),
        ("finish", run_finish),
    ):
        command = sub.add_parser(name)
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--workspace")
        target.add_argument("--owner-email")
        command.add_argument("--shards", required=True, type=_shard_count_arg)
        if name != "status":
            command.add_argument("--apply", action="store_true")
        command.set_defaults(handler=handler)
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = create_store(Settings())
    try:
        args.workspace = _resolve_workspace(store, args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"resolved workspace: {args.workspace}")
    return int(args.handler(store, args))


if __name__ == "__main__":
    raise SystemExit(main())
