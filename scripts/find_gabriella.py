"""Find Gabriella's workspace given the Stripe data we have.

The two recent checkouts were:
  evt_1TaKM6QuWGscJyRc — $5, customer cus_UZTIkVI6DpsBBM, metadata.workspace_id=50842583-...
  evt_1TaKNXQuWGscJyRc — $2, customer cus_UZTKBOx7Nhr18v, metadata.workspace_id=50842583-...

But workspace 50842583-... doesn't exist in our credit ledger. So either:
  A. The workspace was never persisted (signup never finished)
  B. The Stripe metadata captured a UUID that wasn't the live workspace
  C. The signup created a workspace with a different ID, then created a
     SECOND workspace where Stripe linked
  D. Different Spanner instance

This script enumerates workspaces created in the last 24h and matches on:
  - stripe_customer_id == cus_UZTIkVI6DpsBBM or cus_UZTKBOx7Nhr18v
  - email containing "gabriella" or similar
  - created in the last 90 minutes (charge windows)
"""

from __future__ import annotations

import json
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


def main() -> int:
    store = create_store(Settings())
    db = store._database  # noqa: SLF001
    with db.snapshot(multi_use=True) as snap:  # 3 reads on one snapshot
        print("=== workspaces created in last 6h ===")
        rows = snap.execute_sql(
            "SELECT id, body, updated_at FROM tr_entities "
            "WHERE kind = 'workspace' "
            "AND updated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR) "
            "ORDER BY updated_at DESC LIMIT 30"
        )
        any_rows = False
        for row in rows:
            any_rows = True
            wid, body, created = row[0], row[1], row[2]
            email = None
            owner = None
            customer = None
            try:
                d = json.loads(body) if isinstance(body, str) else body
            except (TypeError, ValueError):
                d = {}
            email = (d or {}).get("billing_email")
            owner = (d or {}).get("owner_user_id")
            customer = (d or {}).get("stripe_customer_id")
            print(
                f"  {wid}  created={created}  email={email!r}  "
                f"customer={customer!r}  owner={owner!r}"
            )
        if not any_rows:
            print("  (no workspaces in last 6h)")

        print()
        print("=== workspaces with stripe_customer cus_UZTIkVI6DpsBBM or cus_UZTKBOx7Nhr18v ===")
        rows = snap.execute_sql(
            "SELECT id, body FROM tr_entities WHERE kind = 'workspace' "
            "AND (REGEXP_CONTAINS(body, r'cus_UZTIkVI6DpsBBM') "
            "OR REGEXP_CONTAINS(body, r'cus_UZTKBOx7Nhr18v'))"
        )
        any_rows = False
        for row in rows:
            any_rows = True
            wid, body = row[0], row[1]
            print(f"  {wid}  body={body[:300]}")
        if not any_rows:
            print("  (no matching stripe_customer)")

        print()
        print("=== users created in last 6h ===")
        rows = snap.execute_sql(
            "SELECT id, body, updated_at FROM tr_entities "
            "WHERE kind = 'user' "
            "AND updated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR) "
            "ORDER BY updated_at DESC LIMIT 30"
        )
        any_rows = False
        for row in rows:
            any_rows = True
            uid, body, created = row[0], row[1], row[2]
            email = None
            try:
                d = json.loads(body) if isinstance(body, str) else body
            except (TypeError, ValueError):
                d = {}
            email = (d or {}).get("email")
            print(f"  user={uid}  created={created}  email={email!r}")
        if not any_rows:
            print("  (no users in last 6h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
