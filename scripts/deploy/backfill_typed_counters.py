"""Step 2 CLI: backfill the typed counter mirror and report drift.

See docs/design/billing-typed-counters.md.

  # report drift only (read-only); exit 1 if any drift
  python scripts/deploy/backfill_typed_counters.py --compare

  # backfill missing/stale typed rows, then report
  python scripts/deploy/backfill_typed_counters.py --backfill

Run --backfill until --compare is CLEAN; the Step 3 enforcement flip is gated on
a clean comparator. Requires the typed tables to exist
(scripts/deploy/migrate_typed_counters.sh) and is safe to re-run (idempotent).
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_counter_reconcile import backfill, compare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="write missing/stale typed rows")
    parser.add_argument("--dry-run", action="store_true", help="with --backfill, count only")
    parser.add_argument("--compare", action="store_true", help="report drift (default)")
    args = parser.parse_args()

    store = create_store(Settings())

    if args.backfill:
        counts = backfill(store, dry_run=args.dry_run)
        verb = "would mirror" if args.dry_run else "mirrored"
        # NOTE: this counts every JSON row that would be (re)mirrored, not only
        # missing/stale ones — it is NOT a drift signal. The comparator below is.
        print(f"backfill: {verb} credit={counts['credit']} api_key={counts['api_key']} rows")

    # ALWAYS finish with the comparator — including after --dry-run — so the exit
    # code can never be mistaken for a clean-gate signal (codex Step-2 #4).
    report = compare(store)
    print(report.summary())
    for entity_id, drift in report.samples.items():
        print(f"  DRIFT {entity_id}: {drift}")
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
