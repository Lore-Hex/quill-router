#!/usr/bin/env python3
"""Backfill Stripe/x402 trust facts from finite created-time list ranges."""

from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository
from trusted_router.stripe_trust_history import scan_created_range
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.trust_reconcile_job import run_historical_backfill
from trusted_router.trust_reconciliation import (
    STRIPE_CONSISTENCY_DELAY_SECONDS,
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
)

SOURCE = STRIPE_TRUST_SOURCE
SOURCE_VERSION = STRIPE_TRUST_SOURCE_VERSION


def _parse_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--history-start", required=True, type=_parse_time)
    parser.add_argument("--closed-through", type=_parse_time)
    parser.add_argument("--drain-window-start", type=_parse_time)
    parser.add_argument("--environment")
    parser.add_argument("--source-version", default=SOURCE_VERSION)
    parser.add_argument("--providers", default="stripe,x402")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, stripe_client: Any | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.apply:
        print("REFUSED: historical trust backfill requires --apply")
        return 2
    settings = Settings()
    now = datetime.now(UTC).replace(microsecond=0)
    closed_through = args.closed_through or now - timedelta(
        seconds=STRIPE_CONSISTENCY_DELAY_SECONDS
    )
    scan_start = min(
        value
        for value in (args.history_start, args.drain_window_start)
        if value is not None
    )
    if stripe_client is None:
        if not settings.stripe_secret_key:
            print("REFUSED: TR_STRIPE_SECRET_KEY is not configured")
            return 2
        import stripe

        stripe.api_key = settings.stripe_secret_key
        stripe_client = stripe
    scan = scan_created_range(
        stripe_client,
        start=scan_start,
        end=closed_through,
        recorded_at=now,
    )
    repository = trust_reconciliation_repository(create_store(settings))
    providers = {
        value.strip().lower() for value in args.providers.split(",") if value.strip()
    }
    if not providers or not providers <= {"stripe", "x402"}:
        raise ValueError("--providers must contain stripe and/or x402")
    exit_code = 0
    for provider in sorted(providers):
        result = run_historical_backfill(
            repository,
            scan,
            provider=provider,
            account_id=args.account_id,
            environment=args.environment or settings.environment,
            source=SOURCE,
            source_version=args.source_version,
            history_start=scan_start,
            closed_through=closed_through,
            consistency_delay_seconds=STRIPE_CONSISTENCY_DELAY_SECONDS,
            now=now,
        )
        print(json.dumps(dataclasses.asdict(result), default=str, sort_keys=True))
        if not result.marker.is_complete:
            ops_alert(
                "trust.backfill.unmatched "
                f"provider={provider} unmatched={result.marker.unmatched_count} "
                f"semantic_mismatch={result.marker.semantic_mismatch_count}",
                fingerprint=["trust.backfill.unmatched", provider],
                tags={"provider": provider},
            )
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
