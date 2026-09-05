#!/usr/bin/env python3
"""Reconcile Stripe/x402 created tails and outstanding adverse objects."""

from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository
from trusted_router.stripe_trust_history import (
    latest_adverse_event_watermark,
    scan_created_range,
    scan_stripe_responses,
)
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.trust_reconcile_job import run_recurring_reconciliation
from trusted_router.trust_reconciliation import (
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
    OutstandingAdverse,
)

SOURCE = STRIPE_TRUST_SOURCE
SOURCE_VERSION = STRIPE_TRUST_SOURCE_VERSION


def _parse_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    recursive = getattr(value, "_to_dict_recursive", None)
    if callable(recursive):
        converted = recursive()
        if isinstance(converted, dict):
            return converted
    return dict(value)


def _latest_event_watermark(stripe_client: Any, row: OutstandingAdverse) -> str | None:
    return latest_adverse_event_watermark(
        stripe_client,
        kind=row.kind,
        adverse_ref=row.adverse_ref,
        occurred_at=row.occurred_at,
    )


def refetch_stripe_adverse(
    stripe_client: Any,
    row: OutstandingAdverse,
    observed_at: datetime,
) -> tuple[Any, OutstandingAdverse]:
    obj = (
        stripe_client.Refund.retrieve(row.adverse_ref)
        if row.kind == "refund"
        else stripe_client.Dispute.retrieve(row.adverse_ref)
    )
    body = _mapping(obj)
    watermark = _latest_event_watermark(stripe_client, row)
    if watermark is not None:
        body["_trust_ordering_watermark"] = watermark
    payment_intent = stripe_client.PaymentIntent.retrieve(row.original_payment_ref)
    scan = scan_stripe_responses(
        payment_intents=(),
        refunds=(body,) if row.kind == "refund" else (),
        disputes=(body,) if row.kind == "dispute" else (),
        known_payment_intents=(payment_intent,),
        recorded_at=observed_at,
    )
    if len(scan.adverse) != 1 or len(scan.outstanding) > 1:
        raise RuntimeError("Stripe retrieve did not yield exactly one canonical adverse object")
    event = scan.adverse[0]
    refreshed = (
        scan.outstanding[0]
        if scan.outstanding
        else OutstandingAdverse(
            provider=event.provider,
            kind=event.kind,
            adverse_ref=event.adverse_ref,
            original_payment_ref=event.original_payment_ref,
            lifecycle_status=event.lifecycle_status,
            occurred_at=event.occurred_at,
        )
    )
    return event, refreshed


def _alert_horizon(row: OutstandingAdverse) -> None:
    ops_alert(
        "trust.reconcile.outstanding "
        f"provider={row.provider} adverse_ref={row.adverse_ref} "
        f"terminal_by_horizon=true",
        fingerprint=["trust.reconcile.outstanding", row.provider, row.adverse_ref],
        tags={"provider": row.provider, "adverse_ref": row.adverse_ref},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment")
    parser.add_argument("--source-version", default=SOURCE_VERSION)
    parser.add_argument("--providers", default="stripe,x402")
    parser.add_argument("--now", type=_parse_time, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None, *, stripe_client: Any | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()
    now = args.now or datetime.now(UTC).replace(microsecond=0)
    if stripe_client is None:
        if not settings.stripe_secret_key:
            print("REFUSED: TR_STRIPE_SECRET_KEY is not configured")
            return 2
        import stripe

        stripe.api_key = settings.stripe_secret_key
        stripe_client = stripe
    repository = trust_reconciliation_repository(create_store(settings))
    providers = {
        value.strip().lower() for value in args.providers.split(",") if value.strip()
    }
    if not providers or not providers <= {"stripe", "x402"}:
        raise ValueError("--providers must contain stripe and/or x402")
    exit_code = 0
    for provider in sorted(providers):
        result = run_recurring_reconciliation(
            repository,
            lambda start, end: scan_created_range(
                stripe_client,
                start=start,
                end=end,
                recorded_at=now,
                include_event_watermarks=True,
            ),
            lambda row, observed: refetch_stripe_adverse(
                stripe_client, row, observed
            ),
            provider=provider,
            account_id=args.account_id,
            environment=args.environment or settings.environment,
            source=SOURCE,
            source_version=args.source_version,
            cadence_seconds=settings.trust_reconcile_interval_seconds,
            now=now,
            alert_horizon=_alert_horizon,
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
