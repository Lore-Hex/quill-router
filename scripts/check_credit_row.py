"""Probe whether the credit row exists for Gabriella's workspace and
diagnose why get_credit_account returned None.

Possibilities:
  - The credit row was never written (batch partial-failure on create_workspace)
  - The credit row exists but kind/id key differs
  - The credit row exists but body parses to something get_credit_account
    treats as missing
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from google.cloud.spanner_v1 import param_types

from trusted_router.config import Settings
from trusted_router.storage import create_store

WID = "50842583-5803-4966-a190-1c09d331ebaa"


def main() -> int:
    store = create_store(Settings())
    db = store._database  # noqa: SLF001
    with db.snapshot() as snap:
        rows = snap.execute_sql(
            "SELECT kind, id, body, updated_at FROM tr_entities WHERE id = @id",
            params={"id": WID},
            param_types={"id": param_types.STRING},
        )
        any_rows = False
        for row in rows:
            any_rows = True
            kind, ent_id, body, updated = row[0], row[1], row[2], row[3]
            print(f"  {kind:20} id={ent_id} updated={updated}")
            print(f"    body[:200]={body[:200]}")
        if not any_rows:
            print(f"  no rows at all for id={WID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
