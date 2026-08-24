#!/usr/bin/env python3
"""Validate a synthetic monitor key read from stdin against production state.

The raw key is never accepted on the command line and is never printed.
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

from scripts.provision_synthetic_monitor import validate_synthetic_monitor_key
from trusted_router.config import Settings
from trusted_router.storage import create_store


def main() -> int:
    raw_key = sys.stdin.read()
    try:
        result = validate_synthetic_monitor_key(create_store(Settings()), raw_key)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
