"""Backfill Stripe/x402 trust facts from finite created-time list ranges.

Two modes, mutually exclusive:

* ``--plan``: no writes. Prints one JSON line per PaymentIntent in range
  (credited-locally yes/no and the action ``--apply`` would take) and one per
  refund/dispute (the transition, the recovery debit and the latch it implies),
  then a summary line.
* ``--apply``: the only writing path. Payment facts are written only with local
  credit evidence in the same transaction; adverse facts go through the live
  writers; the completion marker is upserted.

Runs as ``python -m trusted_router.trust_backfill_cli`` inside the image.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trusted_router.config import Settings
from trusted_router.sentry_config import init_sentry
from trusted_router.storage import create_store
from trusted_router.storage_trust_reconciliation import (
    TrustReconciliationRepository,
    trust_reconciliation_repository,
)
from trusted_router.stripe_trust_history import StripeTrustScan, scan_created_range
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.trust_reconcile_job import (
    plan_historical_backfill,
    run_historical_backfill,
)
from trusted_router.trust_reconciliation import (
    STRIPE_CONSISTENCY_DELAY_SECONDS,
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
)

SOURCE = STRIPE_TRUST_SOURCE
SOURCE_VERSION = STRIPE_TRUST_SOURCE_VERSION
PROVIDERS = frozenset({"stripe", "x402"})


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
    parser.add_argument("--environment", default="production")
    parser.add_argument("--source-version", default=SOURCE_VERSION)
    parser.add_argument("--providers", default="stripe,x402")
    parser.add_argument(
        "--credited-events",
        type=Path,
        help=(
            "JSON object {payment_intent_id: [stripe_event_id, ...]} attesting which "
            "Stripe Event credited each PaymentIntent older than Stripe's Events "
            "retention; the marker still has to exist locally in the transaction"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the plan; write nothing")
    mode.add_argument("--apply", action="store_true", help="the only writing mode")
    return parser


def load_credited_events(path: Path | None) -> dict[str, tuple[str, ...]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text())
    if not isinstance(raw, Mapping):
        raise ValueError("--credited-events must be a JSON object keyed by PaymentIntent id")
    resolved: dict[str, tuple[str, ...]] = {}
    for payment_ref, value in raw.items():
        ids = [value] if isinstance(value, str) else list(value)
        if not all(isinstance(item, str) and item for item in ids):
            raise ValueError(f"--credited-events entry for {payment_ref!r} must list event ids")
        resolved[str(payment_ref)] = tuple(dict.fromkeys(ids))
    return resolved


def _providers(raw: str) -> tuple[str, ...]:
    providers = {value.strip().lower() for value in raw.split(",") if value.strip()}
    if not providers or not providers <= PROVIDERS:
        raise ValueError("--providers must contain stripe and/or x402")
    return tuple(sorted(providers))


def _json_line(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def print_plan(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    providers: tuple[str, ...],
    history_start: datetime,
    closed_through: datetime,
) -> int:
    """Emit the read-only plan; returns the number of uncredited PaymentIntents."""

    uncredited = 0
    for provider in providers:
        plan = plan_historical_backfill(repository, scan, provider=provider)
        for payment in plan.payments:
            print(_json_line({"plan": "payment", **dataclasses.asdict(payment)}))
        for adverse in plan.adverse:
            print(_json_line({"plan": "adverse", **dataclasses.asdict(adverse)}))
        uncredited += plan.uncredited_count
        print(
            _json_line(
                {
                    "plan": "summary",
                    "provider": provider,
                    "history_start": history_start,
                    "closed_through": closed_through,
                    "payments": len(plan.payments),
                    "payments_credited_locally": sum(
                        row.credited_locally for row in plan.payments
                    ),
                    "payments_uncredited": plan.uncredited_count,
                    "payments_already_present": sum(
                        row.action == "already_present" for row in plan.payments
                    ),
                    "adverse": len(plan.adverse),
                    "adverse_apply": sum(row.action == "apply" for row in plan.adverse),
                    "recovery_debit_micro": sum(
                        row.recovery_debit_micro for row in plan.adverse
                    ),
                    "latches_implied": sum(row.latch_implied for row in plan.adverse),
                    "unmatched_ids": list(plan.unmatched_ids),
                    "writes": 0,
                }
            )
        )
    return uncredited


def main(argv: list[str] | None = None, *, stripe_client: Any | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.plan and not args.apply:
        print("REFUSED: historical trust backfill requires --plan (read-only) or --apply")
        return 2
    providers = _providers(args.providers)
    credited_events = load_credited_events(args.credited_events)
    # INFO root logger for the ``trust.backfill.unmatched`` lines the runbook
    # greps; ``init_sentry`` so ``ops_alert`` pages (every live job does both).
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    init_sentry(settings)
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
        credited_events=credited_events,
    )
    repository = trust_reconciliation_repository(create_store(settings))
    if args.plan:
        print_plan(
            repository,
            scan,
            providers=providers,
            history_start=scan_start,
            closed_through=closed_through,
        )
        return 0
    exit_code = 0
    for provider in providers:
        result = run_historical_backfill(
            repository,
            scan,
            provider=provider,
            account_id=args.account_id,
            environment=args.environment,
            source=SOURCE,
            source_version=args.source_version,
            history_start=scan_start,
            closed_through=closed_through,
            consistency_delay_seconds=STRIPE_CONSISTENCY_DELAY_SECONDS,
            now=now,
        )
        print(_json_line(dataclasses.asdict(result)))
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
