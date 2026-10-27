"""Drain a bounded NYTE refund queue; run with the control plane's configuration.

Examples:
    python -m scripts.retry_nyte_resale_refunds
    python -m scripts.retry_nyte_resale_refunds --full-refunds

No email, card data, secrets or raw SDK failures are printed. Full refunds are
ops requests, not automated payouts. Repeat periodically until the queue clears.
"""
from __future__ import annotations

import argparse

from trusted_router.config import get_settings
from trusted_router.main import create_app
from trusted_router.services.resale import acknowledge_full_refund, command, process_fill


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--full-refunds", action="store_true")
    view.add_argument("--needs-ops", action="store_true", help="Inspect failed refunds; never re-issue them")
    parser.add_argument("--cursor", default=None)
    parser.add_argument("--ack-full-refund-session", default=None)
    parser.add_argument("--refund-id", default=None)
    args = parser.parse_args()
    settings = get_settings()
    create_app(settings)
    if args.ack_full_refund_session:
        if not args.refund_id:
            parser.error("--refund-id is required")
        acknowledge_full_refund(args.ack_full_refund_session, args.refund_id, settings)
        print("Full refund reconciled")
        return
    queue = "full_refunds" if args.full_refunds else "refunds_needs_ops" if args.needs_ops else "refunds"
    result = command("pending", queue=queue, cursor=args.cursor)
    for row in result["rows"]:
        if args.full_refunds:
            print(f"Full refund requested: {row['position']} at {row['requested_at']}")
        elif args.needs_ops:
            print(f"Needs ops: fill {row['id']}, Stripe refund {row['stripe_refund_id']}")
        else:
            process_fill(row["id"], settings)
            print(f"Processed fill {row['id']}")
    if result["cursor"]:
        print(f"Continue with --cursor {result['cursor']}")


if __name__ == "__main__":
    main()
