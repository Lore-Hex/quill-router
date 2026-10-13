"""Set a workspace's trust override through the store, as the internal admin
route does (``POST /internal/admin/workspaces/{id}/trust-override``), for a
workspace of our own that no payment will ever raise to the tier leases
need: rollout P1's synthetic workspace (docs/runbooks/fastpath-p1.md).

Dry-run is the default: it prints the workspace and the tier its credit rows
hold. ``--apply`` writes the override and the recomputed effective tier into
every credit shard, which the fast path's grant reads.

Example:
  uv run python scripts/set_trust_override.py --workspace-id WS --tier 1 \
    --operator josephjavierperla@tt.live --reason "fastpath P1 synthetic workspace" --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-clickhouse")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")

from trusted_router.config import Settings
from trusted_router.storage import create_store


def _shard_tiers(store: Any, workspace_id: str) -> list[tuple[int, int | None, int | None]]:
    """Each credit shard's (shard, trust_tier, trust_override_tier)."""
    with store._database.snapshot() as snap:
        rows = snap.execute_sql(
            "SELECT shard, trust_tier, trust_override_tier FROM tr_credit_balance "
            "WHERE workspace_id=@pk ORDER BY shard",
            params={"pk": workspace_id},
            param_types={"pk": store._param_types.STRING},
        )
        return [(int(r[0]), None if r[1] is None else int(r[1]), None if r[2] is None else int(r[2])) for r in rows]


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--tier", required=True, type=int, help="0 through 3")
    parser.add_argument("--operator", required=True, help="who sets it, recorded with the override")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-clickhouse":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-clickhouse", file=sys.stderr)
        return 2
    if not 0 <= args.tier <= 3:
        print("ERROR: tier must be 0 through 3", file=sys.stderr)
        return 1
    active_store = create_store(Settings()) if store is None else store
    workspace = active_store.get_workspace(args.workspace_id)
    if workspace is None:
        print(f"ERROR: no workspace {args.workspace_id}", file=sys.stderr)
        return 1
    before = _shard_tiers(active_store, workspace.id)
    print(f"workspace: {workspace.id} ({workspace.name})")
    print(f"before: shards (shard, tier, override) = {before}")
    print(f"override: tier {args.tier}, by {args.operator}: {args.reason}")
    if not args.apply:
        print("DRY-RUN: no override set; pass --apply after reviewing the workspace")
        return 0
    try:
        record = active_store.set_workspace_trust_override(
            workspace.id,
            tier=args.tier,
            identity_bypass=False,
            operator_identity=args.operator,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    after = _shard_tiers(active_store, workspace.id)
    print(f"result: override tier {record.tier} set at {record.set_at.isoformat()}")
    print(f"after: shards (shard, tier, override) = {after}")
    if any(tier is None or tier < args.tier for _, tier, _ in after):
        print("ERROR: a credit shard's effective tier is still below the override", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
