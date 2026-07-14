"""Operator CLI for typed-billing activation and repair.

Sequences workspace pause (quiesce), drain check, fail-closed activation
reconciliation, the invariant auditor, and reserved repair into explicit
operator steps. Every mutating step is fail-closed and requires --apply.

SAFE BATCH FLIP (codex Step-6 design):

  # 1. quiesce the batch (no new work / no new keys; settle still drains)
  python scripts/deploy/ramp_typed_billing.py pause WS1 WS2 --apply

  # 2. wait until drained, then confirm every workspace is flip-ready
  python scripts/deploy/ramp_typed_billing.py status WS1 WS2

  # 3. seed the typed counters (fail-closed: refuses unless never-typed + drained)
  python scripts/deploy/ramp_typed_billing.py reconcile WS1 WS2 --apply

  # 4. [MANUAL] add WS1,WS2 to TR_TYPED_BILLING_WORKSPACE_IDS and deploy all
  #    regions WHILE STILL PAUSED; confirm every region serves the new allowlist.

  # 5. canary one, then unpause the batch
  python scripts/deploy/ramp_typed_billing.py unpause WS1 WS2 --apply

  # standing tripwire (run on a schedule + before each batch)
  python scripts/deploy/ramp_typed_billing.py audit

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
from trusted_router.storage_gcp_counter_reconcile import (
    audit_typed_invariants,
    reconcile_for_flip,
    repair_typed_reserved,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("pause", "unpause", "status", "reconcile", "repair"):
        p = sub.add_parser(name)
        p.add_argument("workspaces", nargs="+")
        if name in ("pause", "unpause", "reconcile", "repair"):
            p.add_argument("--apply", action="store_true", help="actually mutate (else dry-run)")
        if name == "pause":
            p.add_argument("--reason", default="typed-billing ramp")
    sub.add_parser("audit")

    args = parser.parse_args()
    store = create_store(Settings())

    if args.cmd == "audit":
        report = audit_typed_invariants(store)
        print(report.summary())
        for scope, detail in report.samples.items():
            print(f"  VIOLATION {scope}: {detail}")
        return 0 if report.clean else 1

    rc = 0
    for ws in args.workspaces:
        if args.cmd in ("pause", "unpause"):
            paused = args.cmd == "pause"
            if args.apply:
                ok = store.update_workspace(
                    ws, billing_paused=paused,
                    billing_pause_reason=(args.reason if paused else ""),
                )
                print(f"{ws}: {'PAUSED' if paused else 'UNPAUSED'}{'' if ok else ' (NOT FOUND)'}")
            else:
                print(f"{ws}: would {'pause' if paused else 'unpause'} (dry-run; pass --apply)")

        elif args.cmd == "status":
            flip = reconcile_for_flip(store, ws, apply=False)
            print(f"{ws}: flip-ready={flip.ready}" + ("" if flip.ready else f" — {flip.reasons}"))

        elif args.cmd == "reconcile":
            flip = reconcile_for_flip(store, ws, apply=args.apply)
            if not flip.ready:
                print(f"{ws}: NOT flip-ready — {flip.reasons}")
                rc = 1
            elif flip.applied:
                print(f"{ws}: SEEDED credit={flip.credit_seeded} keys={flip.keys_seeded}")
            else:
                print(f"{ws}: ready (dry-run; pass --apply to seed)")

        elif args.cmd == "repair":
            rp = repair_typed_reserved(store, ws, apply=args.apply)
            if not rp.ready:
                print(f"{ws}: NOT repair-ready — {rp.reasons}")
                rc = 1
            elif rp.applied:
                print(f"{ws}: REPAIRED credit reserved {rp.credit_reserved_before}->{rp.credit_reserved_after}, keys={rp.keys_repaired}")
            else:
                print(f"{ws}: would set credit reserved {rp.credit_reserved_before}->{rp.credit_reserved_after} (dry-run; pass --apply)")

    return rc


if __name__ == "__main__":
    sys.exit(main())
