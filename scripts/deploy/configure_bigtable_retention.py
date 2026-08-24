#!/usr/bin/env python3
"""Configure bounded Bigtable column families without touching legacy data."""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

from trusted_router.storage_gcp_synthetic_index import (
    configure_retention_families,
    open_generation_table,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create/update the bounded families; default is a dry run",
    )
    args = parser.parse_args()
    settings = SimpleNamespace(
        gcp_project_id=os.environ.get(
            "TR_GCP_PROJECT_ID",
            os.environ.get("GCP_PROJECT_ID", "quill-cloud-proxy"),
        ),
        bigtable_instance_id=os.environ.get(
            "TR_BIGTABLE_INSTANCE_ID",
            "trusted-router-logs",
        ),
        bigtable_generation_table=os.environ.get(
            "TR_BIGTABLE_GENERATION_TABLE",
            "trustedrouter-generations",
        ),
    )
    table = open_generation_table(settings)
    actions = configure_retention_families(table, apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    for action in actions:
        print(
            f"{mode}: {action['action']} family={action['family']} "
            f"max_age_days={action['max_age_days']} max_versions=1"
        )
    print(f"{mode}: legacy family=m unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
