#!/usr/bin/env python3
"""Step-4 precondition: prove one cloud holds no v1 BYOK envelope, and record it.

Step 4 of docs/design/byok-aad-v2-migration.md deletes v1 envelope support. It
is the one step in that plan that cannot be rolled back: a v1 row that survives
it is a customer's provider key that nothing can open again. Until now the only
thing gating it was a sentence in a table. This is the executable form of that
sentence, one cloud at a time.

Read-only. It never decrypts, never writes to the database, and never needs KMS
access — it classifies envelopes by their stored `algorithm` field.

  uv run python scripts/check_no_v1_envelopes.py --backend spanner --cloud gcp
  uv run python scripts/check_no_v1_envelopes.py --backend postgres --cloud aws \
      --record --operator you@lorehex.co
  uv run python scripts/check_no_v1_envelopes.py --status-only

Exit codes:
  0  this cloud attests zero v1 envelopes (outcome clean or empty_witnessed)
  2  it does not, including the case where the run scanned nothing
  3  the run could not be completed at all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trusted_router.byok_aad_backfill import (
    AuditableStore,
    PostgresEntityStore,
    SpannerEntityStore,
    attestation_for,
    check_no_v1_envelopes,
)
from trusted_router.byok_v1_attestations import (
    STANDALONE_CLOUDS,
    load_ledger,
    record_attestation,
    surface_fingerprint,
    zero_v1_blockers,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=STANDALONE_CLOUDS)
    parser.add_argument("--backend", choices=("spanner", "postgres"))
    parser.add_argument(
        "--record",
        action="store_true",
        help="Write a passing result into the attestation ledger",
    )
    parser.add_argument("--operator", default="", help="Who ran this; required with --record")
    parser.add_argument("--note", default="", help="Free-text context stored with the attestation")
    parser.add_argument("--ledger", type=Path, help="Ledger path (defaults to the committed one)")
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Print what the ledger currently blocks and exit; touches no database",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--census-sample-limit", type=int, default=1000)
    parser.add_argument(
        "--project",
        default=os.environ.get("TR_GCP_PROJECT_ID", "quill-cloud-proxy"),
    )
    parser.add_argument(
        "--spanner-instance",
        default=os.environ.get("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6"),
    )
    parser.add_argument(
        "--spanner-database",
        default=os.environ.get("TR_SPANNER_DATABASE_ID", "trusted-router"),
    )
    parser.add_argument("--postgres-dsn", default=os.environ.get("TR_POSTGRES_DSN", ""))
    return parser


def _store(args: argparse.Namespace) -> AuditableStore:
    # No --after-kind/--after-id here, deliberately, unlike the backfill script:
    # a precondition that can be resumed from a cursor is a precondition that
    # can be made to skip the rows it would have failed on.
    if args.backend == "spanner":
        from google.cloud import spanner

        client = spanner.Client(project=args.project, disable_builtin_metrics=True)
        database = client.instance(args.spanner_instance).database(args.spanner_database)
        return SpannerEntityStore(database, spanner.param_types)
    return PostgresEntityStore(args.postgres_dsn)


def _print_ledger_status(ledger: dict[str, Any]) -> None:
    blockers = zero_v1_blockers(ledger)
    print(f"ledger surfaces={surface_fingerprint()}")
    if not blockers:
        print("ledger: every standalone cloud attests zero v1 envelopes")
        return
    print("ledger: step 4 is blocked")
    for blocker in blockers:
        print(f"  - {blocker}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.status_only:
        _print_ledger_status(load_ledger(args.ledger))
        return 0
    if args.cloud is None or args.backend is None:
        raise SystemExit("--cloud and --backend are required unless --status-only is given")
    if args.record and not args.operator.strip():
        raise SystemExit("--operator is required with --record")

    try:
        store = _store(args)
        result = check_no_v1_envelopes(
            store,
            cloud=args.cloud,
            batch_size=args.batch_size,
            sample_limit=args.census_sample_limit,
        )
    except Exception as exc:  # a run that could not complete attests nothing
        print(f"INCONCLUSIVE: the precondition did not complete: {type(exc).__name__}: {exc}")
        print(
            "This is exit 3, not exit 0. A run that could not query the database has not "
            "established that the database holds no v1 envelopes."
        )
        return 3

    print(f"result={json.dumps(result.as_dict(), sort_keys=True)}")
    if not result.passed:
        print(f"NOT AN ATTESTATION [{result.outcome}] for {args.cloud}: {result.detail}")
        print(
            "Nothing was recorded. 'the audit found no v1 rows' and 'the audit could have "
            "found v1 rows and did not' are different claims; only the second one gates step 4."
        )
        return 2

    print(f"ATTESTS ZERO V1 [{result.outcome}] for {args.cloud}: {result.detail}")
    if args.record:
        attestation = attestation_for(
            result,
            backend=args.backend,
            operator=args.operator.strip(),
            note=args.note.strip(),
        )
        record_attestation(attestation, path=args.ledger)
        print(f"recorded {args.cloud} in the attestation ledger")
    else:
        print("not recorded: pass --record --operator <you> to write it into the ledger")
    _print_ledger_status(load_ledger(args.ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
