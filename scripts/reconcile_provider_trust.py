#!/usr/bin/env python3
"""PayPal/Adyen historical, rolling-deploy and recurring trust facts (inert)."""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_router.adyen_trust_history import AdyenAccountingSource, read_payment_accounting_report
from trusted_router.config import Settings
from trusted_router.paypal_trust_history import (
    PayPalHistoryClient,
    history_start,
    refetch_paypal_adverse,
    scan_paypal_created_range,
)
from trusted_router.provider_trust_postgres import provider_reconciliation_repository
from trusted_router.provider_trust_reconcile import run_provider_backfill, run_provider_recurring
from trusted_router.services.provider_trust import timestamp
from trusted_router.storage import create_store
from trusted_router.synthetic.alerts import ops_alert


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("paypal", "adyen"), required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--mode", choices=("backfill", "recurring"), required=True)
    parser.add_argument("--history-start", type=timestamp)
    parser.add_argument("--drained-at", type=timestamp)
    parser.add_argument("--report-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required to write trust facts and markers")
    settings = Settings()
    now = datetime.now(UTC).replace(microsecond=0)
    scan: Any
    refetch: Any
    if args.provider == "paypal":
        if not settings.paypal_enabled:
            parser.error("PayPal credentials are not configured")
        client = PayPalHistoryClient(settings)
        environment = ("sandbox" if "sandbox" in settings.paypal_api_base_url else "live")
        def scan(start: datetime, end: datetime) -> Any:
            return scan_paypal_created_range(client, account_id=args.account_id, start=start, end=end, recorded_at=now)
        def refetch(row: Any, at: datetime) -> Any:
            return refetch_paypal_adverse(client, row, at)
        if args.history_start is not None:
            args.history_start = history_start(args.history_start, now=now)
    else:
        if args.report_manifest is None:
            parser.error("Adyen requires --report-manifest with complete report coverage")
        manifest = json.loads(args.report_manifest.read_text())
        environment = settings.adyen_environment
        if manifest["environment"] != environment or manifest["account_id"] != settings.adyen_merchant_account or args.account_id != settings.adyen_merchant_account:
            parser.error("Adyen report account/environment mismatch")
        rows = [row for file in manifest["files"]
                for row in read_payment_accounting_report(args.report_manifest.parent / file)]
        source = AdyenAccountingSource(
            rows, account_id=args.account_id, environment=environment,
            covered_from=timestamp(manifest["covered_from"]),
            covered_through=timestamp(manifest["covered_through"]),
            reference_key=str(settings.adyen_reference_key),
        )
        def scan(start: datetime, end: datetime) -> Any:
            return source.scan(start, end, now)
        refetch = source.refetch
    repository = provider_reconciliation_repository(create_store(settings))
    if args.mode == "backfill":
        if args.history_start is None or args.drained_at is None:
            parser.error("Backfill requires --history-start and --drained-at")
        result = run_provider_backfill(
            repository, scan, provider=args.provider, account_id=args.account_id,
            environment=environment, history_start=args.history_start,
            drained_at=args.drained_at, now=now,
        )
    else:
        result = run_provider_recurring(
            repository, scan, refetch, provider=args.provider, account_id=args.account_id,
            environment=environment, cadence_seconds=settings.trust_reconcile_interval_seconds,
            now=now, alert_horizon=lambda row: ops_alert(
                f"trust.reconcile.outstanding provider={row.provider} adverse_ref={row.adverse_ref}",
                fingerprint=["trust.reconcile.outstanding", row.provider, row.adverse_ref],
            ),
        )
    print(json.dumps(dataclasses.asdict(result), default=str, sort_keys=True))
    return 0 if result.marker.completed_at is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
