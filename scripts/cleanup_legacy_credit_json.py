"""Strip retired money fields from Spanner JSON credit metadata.

The command is read-only unless ``--apply`` is supplied. It first verifies the
global typed-ledger invariant and every target workspace's configured typed
shards. Apply then revalidates each row transactionally before changing it.

Examples:
  uv run python scripts/cleanup_legacy_credit_json.py
  uv run python scripts/cleanup_legacy_credit_json.py --apply
  uv run python scripts/cleanup_legacy_credit_json.py --workspace WORKSPACE_ID
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
from trusted_router.storage_gcp_credit_json_cleanup import (
    cleanup_credit_json,
    legacy_credit_workspace_ids,
)


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", action="append", dest="workspaces")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2

    active_store = create_store(Settings()) if store is None else store
    invariant = audit_typed_invariants(active_store)
    print(f"typed invariant: {invariant.summary()}")
    if not invariant.clean:
        print("ERROR: typed invariant is not clean; refusing JSON cleanup", file=sys.stderr)
        return 1

    workspace_ids = (
        sorted(set(args.workspaces))
        if args.workspaces
        else legacy_credit_workspace_ids(active_store)
    )
    preflight = [cleanup_credit_json(active_store, workspace_id) for workspace_id in workspace_ids]
    blocked = [result for result in preflight if not result.ready]
    for result in blocked:
        print(
            f"BLOCKED {result.workspace_id}: {result.reason}; "
            f"expected={result.expected_shards} observed={result.observed_shards}"
        )
    if blocked:
        print("ERROR: preflight failed; no rows changed", file=sys.stderr)
        return 1

    pending = [result for result in preflight if result.needs_cleanup]
    print(
        f"preflight: targets={len(workspace_ids)} stale_rows={len(pending)} "
        f"already_clean={len(workspace_ids) - len(pending)}"
    )
    if not args.apply:
        print("DRY-RUN: no rows changed; pass --apply after reviewing preflight")
        return 0

    applied = 0
    for result in pending:
        updated = cleanup_credit_json(active_store, result.workspace_id, apply=True)
        if not updated.ready or not updated.applied:
            print(
                f"ERROR: transactional revalidation failed for {result.workspace_id}: "
                f"{updated.reason or 'row was not updated'}",
                file=sys.stderr,
            )
            return 1
        applied += 1

    remaining = legacy_credit_workspace_ids(active_store)
    if remaining:
        print(f"ERROR: {len(remaining)} stale rows remain after apply", file=sys.stderr)
        return 1
    print(f"APPLIED: stripped retired money keys from {applied} credit metadata rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
