"""Operator CLI for the authoritative typed credit ledger.

Supports workspace quiescing, the standing invariant audit, and reserved-counter
repair. The old JSON-to-typed activation commands were removed with the retired
JSON money ledger. Every mutating step is fail-closed and requires ``--apply``.
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
    repair_typed_reserved,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("pause", "unpause", "repair"):
        p = sub.add_parser(name)
        p.add_argument("workspaces", nargs="+")
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
