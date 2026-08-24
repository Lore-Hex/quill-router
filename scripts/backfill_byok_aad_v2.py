#!/usr/bin/env python3
"""Audit or backfill BYOK/control-secret envelopes from AAD v1 to v2.

The default is a read-only audit. Applying requires an exact expected count,
which prevents an operator from accidentally migrating a larger or different
dataset than the one they reviewed.

Examples:
  uv run python scripts/backfill_byok_aad_v2.py --backend spanner
  uv run python scripts/backfill_byok_aad_v2.py --backend spanner \
    --apply --expected-v1 7
  uv run python scripts/backfill_byok_aad_v2.py --backend postgres
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from trusted_router.byok_aad_backfill import (
    BackfillRunner,
    EntityStore,
    PostgresEntityStore,
    SpannerEntityStore,
)
from trusted_router.key_management import KeyWrapperConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("spanner", "postgres"), required=True)
    parser.add_argument("--apply", action="store_true", help="Rewrite v1 envelopes to v2")
    parser.add_argument(
        "--expected-v1",
        type=int,
        help="Required with --apply; mutation is refused unless the audit count matches exactly",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--kms-operations-per-second", type=float, default=5.0)
    parser.add_argument("--after-kind")
    parser.add_argument("--after-id")
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
    parser.add_argument(
        "--kms-key-name",
        default=os.environ.get("TR_BYOK_KMS_KEY_NAME", ""),
        help="Wrapping KMS key; required with --apply",
    )
    return parser


def _spanner_store(args: argparse.Namespace) -> EntityStore:
    from google.cloud import spanner

    client = spanner.Client(project=args.project, disable_builtin_metrics=True)
    database = client.instance(args.spanner_instance).database(args.spanner_database)
    return SpannerEntityStore(database, spanner.param_types)


def _store(args: argparse.Namespace) -> EntityStore:
    if args.backend == "spanner":
        return _spanner_store(args)
    return PostgresEntityStore(args.postgres_dsn)


def _summary(label: str, stats: Any) -> None:
    print(f"{label}={json.dumps(stats.as_dict(), sort_keys=True)}")


def main() -> int:
    args = _parser().parse_args()
    if (args.after_kind is None) != (args.after_id is None):
        raise SystemExit("--after-kind and --after-id must be supplied together")
    if args.apply and args.expected_v1 is None:
        raise SystemExit("--expected-v1 is required with --apply")
    if args.expected_v1 is not None and args.expected_v1 < 0:
        raise SystemExit("--expected-v1 cannot be negative")
    if args.apply and not args.kms_key_name.strip():
        raise SystemExit("--kms-key-name or TR_BYOK_KMS_KEY_NAME is required with --apply")
    after = None
    if args.after_kind is not None and args.after_id is not None:
        after = (args.after_kind, args.after_id)

    store = _store(args)
    audit = BackfillRunner(store, apply=False).run(batch_size=args.batch_size, after=after)
    _summary("preflight", audit)
    if audit.failures or audit.unsupported_algorithms:
        print("REFUSED: preflight found malformed or unsupported envelopes")
        return 2
    if not args.apply:
        return 0
    if audit.v1_envelopes != args.expected_v1:
        print(
            "REFUSED: expected-v1 gate did not match "
            f"expected={args.expected_v1} actual={audit.v1_envelopes}"
        )
        return 2
    if audit.v1_envelopes == 0:
        print("nothing to migrate")
        return 0

    migrated = BackfillRunner(
        store,
        settings=KeyWrapperConfig(byok_kms_key_name=args.kms_key_name.strip()),
        apply=True,
        kms_operations_per_second=args.kms_operations_per_second,
    ).run(batch_size=args.batch_size, after=after)
    _summary("migration", migrated)

    verification = BackfillRunner(store, apply=False).run(batch_size=args.batch_size)
    _summary("verification", verification)
    if (
        verification.v1_envelopes
        or verification.failures
        or verification.unsupported_algorithms
    ):
        print("FAILED: post-write audit is not clean")
        return 3
    print("complete: every discovered envelope is v2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
