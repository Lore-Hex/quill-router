"""Cover or mark every workspace whose credit rows break the debt rules.

The fast-admission design's section 4.7: no credit shard is negative unless
every shard of its workspace is marked in debt, and a workspace is marked only
while its signed sum is negative. Since the debt rules shipped, every write
keeps that. A workspace that went negative before them, or whose mark older
code did not clear, does not; this pass settles each one with the same
primitive the writers use (`storage_gcp_credit_debt.cover_or_mark`): it covers
a negative shard from the workspace's other shards, lowest first, or marks
every shard, or clears a mark the balance no longer bears out.

Run it once every serving revision has the debt rules, and again after a
rollback and roll forward. It is idempotent.

Read-only by default: one low-priority read of every credit row, then a report.
Pass ``--apply`` to write, one transaction per workspace, each of which reads
the workspace's rows again before it writes.

Examples:
  uv run python scripts/settle_negative_shards.py
  uv run python scripts/settle_negative_shards.py --apply
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-clickhouse")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")

from trusted_router import credit_debt
from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_credit_debt import cover_or_mark

# Every credit row, about eighteen thousand of them: small enough to read whole
# at low priority, and filtered here rather than by an expression in SQL.
_ROWS_SQL = (
    "SELECT workspace_id, shard, total_credits - total_usage - reserved, "
    "COALESCE(in_debt, FALSE) FROM tr_credit_balance"
)
_READ_OPTIONS: dict[str, Any] = {"request_options": {"priority": "PRIORITY_LOW"}}


@dataclass(frozen=True)
class Workspace:
    """A workspace whose rows break the rules: its headroom per shard, and its mark."""

    workspace_id: str
    headroom: tuple[int, ...]
    marked: bool

    @property
    def action(self) -> str:
        squared = credit_debt.square(self.headroom)
        if squared.marked:
            return "mark" if not self.marked else "keep"
        if squared.headroom != self.headroom:
            return "cover"
        return "clear" if self.marked else "keep"


def find_workspaces(store: Any) -> list[Workspace]:
    """The workspaces with a negative shard or a mark that the rules would change."""

    rows: dict[str, dict[int, tuple[int, bool]]] = defaultdict(dict)
    with store._database.snapshot() as snapshot:
        for workspace_id, shard, headroom, marked in snapshot.execute_sql(_ROWS_SQL, **_READ_OPTIONS):
            rows[str(workspace_id)][int(shard)] = (int(headroom), bool(marked))
    found = []
    for workspace_id in sorted(rows):
        shards = rows[workspace_id]
        if sorted(shards) != list(range(len(shards))):
            print(f"SKIP: {workspace_id} has an incomplete shard set {sorted(shards)}")
            continue
        headroom = tuple(shards[shard][0] for shard in range(len(shards)))
        marked = any(shards[shard][1] for shard in range(len(shards)))
        if min(headroom) >= 0 and not marked:
            continue
        workspace = Workspace(workspace_id, headroom, marked)
        if workspace.action != "keep":
            found.append(workspace)
    return found


def run(store: Any, *, apply: bool = False) -> dict[str, int]:
    """Report every workspace the rules would change; with apply, change them."""

    workspaces = find_workspaces(store)
    counts: dict[str, int] = defaultdict(int)
    for workspace in workspaces:
        counts[workspace.action] += 1
        print(
            f"{'APPLY' if apply else 'DRY-RUN'}: {workspace.action} {workspace.workspace_id} "
            f"headroom={list(workspace.headroom)} sum={sum(workspace.headroom)} "
            f"marked={workspace.marked}"
        )
        if apply:
            store._run_in_transaction(
                lambda transaction, workspace_id=workspace.workspace_id, now=datetime.now(UTC): (
                    cover_or_mark(transaction, store._param_types, workspace_id, now=now)
                )
            )
    summary = {action: counts[action] for action in ("cover", "mark", "clear")}
    print(
        f"{'APPLIED' if apply else 'WOULD APPLY'}: "
        + " ".join(f"{action}={count}" for action, count in summary.items())
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write, one transaction per workspace")
    args = parser.parse_args(argv)
    run(create_store(Settings()), apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
