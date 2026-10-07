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

Read-only by default: one low-priority read of every credit row, a read of
each candidate workspace's credit account by its complete key (a workspace
whose rows are not exactly its configured shards is reported and skipped), and
a report. Pass ``--apply`` to write, one transaction per workspace, each of
which reads the workspace's rows again, checks them against the configured
count, and writes only what they still need; the summary counts what the
transactions committed.

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
from trusted_router.storage_gcp_counters import credit_shard_count
from trusted_router.storage_gcp_credit_debt import CreditRowsIncomplete, settle_credit_rows

# Every credit row, about eighteen thousand of them: small enough to read whole
# at low priority, and filtered here rather than by an expression in SQL.
_ROWS_SQL = (
    "SELECT workspace_id, shard, total_credits - total_usage - reserved, "
    "COALESCE(in_debt, FALSE) FROM tr_credit_balance"
)
_READ_OPTIONS: dict[str, Any] = {"request_options": {"priority": "PRIORITY_LOW"}}
ACTIONS = ("cover", "mark", "clear")


def action_for(headroom: tuple[int, ...], marks: tuple[bool, ...]) -> str:
    """What squaring these rows does: cover, mark, clear, or keep them as they are."""

    squared = credit_debt.square(headroom)
    if squared.marked:
        return "keep" if all(marks) else "mark"
    if squared.headroom != headroom:
        return "cover"
    return "clear" if any(marks) else "keep"


@dataclass(frozen=True)
class Workspace:
    """A workspace whose rows break the rules: each shard's headroom and mark."""

    workspace_id: str
    headroom: tuple[int, ...]
    marks: tuple[bool, ...]

    @property
    def action(self) -> str:
        return action_for(self.headroom, self.marks)


def find_workspaces(store: Any) -> list[Workspace]:
    """The workspaces with a negative shard or a mark that the rules would change.

    A workspace whose rows are not exactly the shards its credit account
    configures, read by its complete key, is reported and skipped.
    """

    rows: dict[str, dict[int, tuple[int, bool]]] = defaultdict(dict)
    with store._database.snapshot() as snapshot:
        for workspace_id, shard, headroom, marked in snapshot.execute_sql(_ROWS_SQL, **_READ_OPTIONS):
            rows[str(workspace_id)][int(shard)] = (int(headroom), bool(marked))
    found = []
    for workspace_id in sorted(rows):
        shards = rows[workspace_id]
        if min(value for value, _marked in shards.values()) >= 0 and not any(
            marked for _value, marked in shards.values()
        ):
            continue
        account = store.get_credit_account(workspace_id)
        if account is None:
            print(f"SKIP: {workspace_id} has credit rows but no credit account")
            continue
        count = credit_shard_count(account)
        if sorted(shards) != list(range(count)):
            print(f"SKIP: {workspace_id} has shards {sorted(shards)}, configured {count}")
            continue
        workspace = Workspace(
            workspace_id,
            tuple(shards[shard][0] for shard in range(count)),
            tuple(shards[shard][1] for shard in range(count)),
        )
        if workspace.action != "keep":
            found.append(workspace)
    return found


def run(store: Any, *, apply: bool = False) -> dict[str, int]:
    """Report every workspace the rules would change; with apply, change them.

    A dry run returns the actions it selected. An apply returns what the
    transactions committed: each reads the rows again, so a workspace that
    money repaired since the read is counted as unchanged.
    """

    counts: dict[str, int] = defaultdict(int)
    for workspace in find_workspaces(store):
        print(
            f"{'APPLY' if apply else 'DRY-RUN'}: {workspace.action} {workspace.workspace_id} "
            f"headroom={list(workspace.headroom)} sum={sum(workspace.headroom)} "
            f"marks={list(workspace.marks)}"
        )
        if not apply:
            counts[workspace.action] += 1
            continue
        try:
            before, _after = store._run_in_transaction(
                lambda transaction, workspace=workspace, now=datetime.now(UTC): settle_credit_rows(
                    transaction, store._param_types, workspace.workspace_id,
                    now=now, shard_count=len(workspace.headroom),
                )
            )
        except CreditRowsIncomplete:
            print(f"SKIP: {workspace.workspace_id}'s shard set changed since it was read")
            continue
        committed = action_for(before.headroom, before.marks)
        counts[committed if committed != "keep" else "unchanged"] += 1
        print(f"  committed: {committed if committed != 'keep' else 'nothing, already settled'}")
    summary = {action: counts[action] for action in ACTIONS}
    if apply:
        summary["unchanged"] = counts["unchanged"]
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
