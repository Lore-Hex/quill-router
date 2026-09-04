#!/usr/bin/env python3
"""Provision the dedicated workspace/key for the recurring Stage D probe.

The key must be heartbeat-capable and use local typed authorization. After the
dry-run/apply, put the emitted workspace id in TR_STAGE_D_PILOT_WORKSPACE_IDS,
keep it out of TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS, and store the raw
key as Secret Manager secret ``trustedrouter-stage-d-probe-api-key``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

try:
    from scripts.provision_synthetic_monitor import provision
except ModuleNotFoundError:  # direct ``scripts/foo.py`` execution
    from provision_synthetic_monitor import provision
from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.storage import create_store

DEFAULT_EMAIL = "stage-d-probe@trustedrouter.internal"
DEFAULT_WORKSPACE_NAME = "TrustedRouter Stage D Probe"
DEFAULT_KEY_NAME = "Stage D streaming probe"
DEFAULT_FUNDING_EVENT = "stage_d_probe_workspace_funding_v1"
DEFAULT_TARGET_SHARDS = 16


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-name", default=DEFAULT_WORKSPACE_NAME)
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--fund-usd", default="1000")
    parser.add_argument("--funding-event-id", default=DEFAULT_FUNDING_EVENT)
    parser.add_argument("--target-shards", type=int, default=DEFAULT_TARGET_SHARDS)
    parser.add_argument("--key-output-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2
    if args.target_shards < 1 or args.target_shards > 64:
        print("ERROR: --target-shards must be between 1 and 64", file=sys.stderr)
        return 2
    try:
        active_store = create_store(Settings()) if store is None else store
        result = provision(
            active_store,
            email=args.email.strip().lower(),
            workspace_name=args.workspace_name.strip(),
            key_name=args.key_name.strip(),
            funding_microdollars=dollars_to_microdollars(args.fund_usd),
            funding_event_id=args.funding_event_id.strip(),
            target_shards=args.target_shards,
            apply=args.apply,
            key_output_file=args.key_output_file,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    workspace_id = result.get("workspace_id")
    result.update(
        {
            "heartbeat_capable_local_typed_key": True,
            "stage_d_pilot_workspace_id": workspace_id,
            "regional_quota_pilot_membership_required": False,
            "secret_name": "trustedrouter-stage-d-probe-api-key",
        }
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.apply:
        print("DRY-RUN: no production state changed; pass --apply after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
