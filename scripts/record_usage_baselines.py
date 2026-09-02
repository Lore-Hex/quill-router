"""Record a verified pre-ledger usage baseline for workspaces that still need one.

Read-only unless ``--apply``. It records no number of its own: the baseline is
the remainder the retained typed ledger cannot account for. Before terminal-row
retention began, that remainder was pre-ledger usage. It is no longer safe to
infer this automatically because settled reservations expire after 30 days.
Recording changes no counter and moves no money, but a wrong baseline would
make an incomplete audit look exact.

Most workspaces need nothing. Where the ledger already explains the counter the
baseline is zero and no row is written; only a counter that EXCEEDS its ledger
has history to record.

  uv run python scripts/record_usage_baselines.py            # preflight
  uv run python scripts/record_usage_baselines.py --apply \
    --verified-retained-ledger-complete

Apply is fail-closed unless the operator explicitly confirms independent
evidence that no terminal rows are missing from the proposed retained ledger.
It also refuses to overwrite an existing baseline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_counter_reconcile import (
    propose_usage_baselines,
    record_usage_baseline,
)


def _usd(microdollars: int) -> str:
    return f"${microdollars / 1_000_000:,.2f}"


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", action="append", dest="workspaces")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--verified-retained-ledger-complete",
        action="store_true",
        help="confirm independent evidence that no settled rows have aged out",
    )
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2
    if args.apply and not args.verified_retained_ledger_complete:
        print(
            "ERROR: refusing to infer a baseline from a retained ledger after "
            "30-day row expiry. Independently verify that the proposed ledger "
            "is complete, then pass --verified-retained-ledger-complete.",
            file=sys.stderr,
        )
        return 2

    active_store = create_store(Settings()) if store is None else store
    proposals = propose_usage_baselines(active_store)
    if args.workspaces:
        wanted = set(args.workspaces)
        proposals = [p for p in proposals if p.workspace_id in wanted]

    outstanding = [p for p in proposals if p.needed]
    print(f"workspaces needing a baseline: {len(outstanding)}")
    total = sum(p.baseline_microdollars for p in outstanding)
    for proposal in sorted(outstanding, key=lambda p: -p.baseline_microdollars):
        print(
            f"  {proposal.workspace_id}  baseline={_usd(proposal.baseline_microdollars)}"
            f"  (counter {_usd(proposal.typed_total_usage)}"
            f" vs ledger {_usd(proposal.ledger_booked)})"
        )
    already = [p for p in proposals if p.already_recorded is not None]
    for proposal in already:
        print(f"  {proposal.workspace_id}: already recorded, leaving alone")
    print(f"total baseline to record: {_usd(total)}")

    if not outstanding:
        print("nothing to do")
        return 0
    if not args.apply:
        print("DRY-RUN: no rows written; pass --apply after reviewing the list above")
        return 0

    recorded_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    written = 0
    for proposal in outstanding:
        if record_usage_baseline(
            active_store,
            proposal,
            recorded_at=recorded_at,
            apply=True,
            retained_ledger_complete=True,
        ):
            written += 1
        else:
            print(f"  skipped {proposal.workspace_id}: a baseline appeared concurrently")
    print(f"recorded {written} baseline(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
