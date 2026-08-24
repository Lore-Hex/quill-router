#!/usr/bin/env python3
"""Provision an explicit provider-portal grant.

Dry-run is the default. The user receives no trial credit when created.
"""

from __future__ import annotations

import argparse

from trusted_router.config import Settings
from trusted_router.storage import create_store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--role", choices=("viewer", "admin"), default="viewer")
    parser.add_argument("--project", default="quill-cloud-proxy")
    parser.add_argument("--spanner-instance", default="trusted-router-nam6")
    parser.add_argument("--spanner-database", default="trusted-router")
    parser.add_argument("--bigtable-instance", default="trusted-router-logs")
    parser.add_argument("--bigtable-table", default="trustedrouter-generations")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = Settings(
        environment="local",
        storage_backend="spanner-bigtable",
        gcp_project_id=args.project,
        spanner_instance_id=args.spanner_instance,
        spanner_database_id=args.spanner_database,
        bigtable_instance_id=args.bigtable_instance,
        bigtable_generation_table=args.bigtable_table,
        signup_trial_credit_microdollars=0,
    )
    store = create_store(settings)
    existing = store.find_user_by_email(args.email)
    if not args.apply:
        print(
            f"DRY RUN: {'existing' if existing else 'new'} user {args.email}; "
            f"grant {args.role} access to {args.provider}; trial credit $0"
        )
        return 0

    user = existing or store.ensure_user(
        args.email,
        email=args.email,
        trial_credit_microdollars=0,
    )
    grant = store.grant_provider_access(
        user.id,
        args.provider,
        role=args.role,
    )
    print(
        f"granted user={user.email} user_id={user.id} "
        f"provider={grant.provider} role={grant.role}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
