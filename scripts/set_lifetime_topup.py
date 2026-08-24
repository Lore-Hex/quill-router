"""Apply an idempotent lifetime-top-up support override without granting credits.

Example:
  uv run python scripts/set_lifetime_topup.py \
    --user-id USER_ID --amount-dollars 25 --event-id support_override_2026_08_16
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars, format_money_precise
from trusted_router.storage import create_store


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--amount-dollars", required=True)
    parser.add_argument("--event-id", required=True)
    args = parser.parse_args(argv)

    try:
        amount = dollars_to_microdollars(args.amount_dollars)
        if amount <= 0:
            raise ValueError("amount must be positive")
        active_store = create_store(Settings()) if store is None else store
        if active_store.get_user(args.user_id) is None:
            raise ValueError(f"no user found for {args.user_id}")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    applied = active_store.add_lifetime_topup(args.user_id, amount, args.event_id)
    print(
        f"result: {'applied' if applied else 'no-op (event already applied)'}; "
        f"user={args.user_id} amount={format_money_precise(amount)} event_id={args.event_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
