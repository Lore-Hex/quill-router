#!/usr/bin/env python3
"""Reconcile Stripe/x402 created tails and outstanding adverse objects."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from datetime import UTC, datetime
from typing import Any

from trusted_router.config import Settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository
from trusted_router.stripe_trust_history import (
    adverse_lifecycle_status,
    latest_adverse_event,
    scan_created_range,
    scan_stripe_responses,
    stamp_adverse_source_event,
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
    # Event-based occurred_at, subtype and watermark from the max-watermark
    # Event of the object's current status: the shape the live writer
    # converges to, so the refetched fact is a replay, not a mismatch.
    stamp_adverse_source_event(
        body,
        latest_adverse_event(
            stripe_client,
            kind=row.kind,
            adverse_ref=row.adverse_ref,
            occurred_at=row.occurred_at,
            lifecycle_status=adverse_lifecycle_status(body, kind=row.kind),
        ),
    )
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
    # Explicit because every Cloud Run job carries TR_ENVIRONMENT=worker, which
    # would key the marker under an environment nothing else ever reads.
    parser.add_argument("--environment", default="production")
    parser.add_argument("--source-version", default=SOURCE_VERSION)
    parser.add_argument("--providers", default="stripe,x402")
    parser.add_argument("--now", type=_parse_time, help=argparse.SUPPRESS)
    return parser


REFUSAL_REMEDY = (
    "re-execute the historical backfill job (trusted-router-trust-backfill, --apply; "
    "runbook step 6) until its marker completes, then resume the reconciler"
)


def main(argv: list[str] | None = None, *, stripe_client: Any | None = None) -> int:
    # INFO on the root logger: the runbook greps the job log for
    # ``trust.reconcile.outstanding provider=... value=0``; the default WARNING
    # root dropped those lines. Sentry: ``ops_alert`` reaches Sentry only after
    # ``init_sentry`` (the pattern every live job follows).
    logging.basicConfig(level=logging.INFO)
    args = _parser().parse_args(argv)
    settings = Settings()
    init_sentry(settings)
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
        try:
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
                environment=args.environment,
                source=SOURCE,
                source_version=args.source_version,
                cadence_seconds=settings.trust_reconcile_interval_seconds,
                now=now,
                alert_horizon=_alert_horizon,
            )
        except RuntimeError as exc:
            # A refused provider (absent or incomplete historical marker) pages
            # with its remedy and does not skip the other provider.
            ops_alert(
                f"trust.reconcile.refused provider={provider} reason={exc} "
                f"remedy={REFUSAL_REMEDY}",
                fingerprint=["trust.reconcile.refused", provider],
                tags={"provider": provider},
            )
            exit_code = 1
            continue
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
