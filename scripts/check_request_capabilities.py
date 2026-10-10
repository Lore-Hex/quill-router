#!/usr/bin/env python3
"""Report reviewed provider/model pairs without an endpoint in the repo catalog.

Run manually during contract review, not as a refresh/CI gate: routine catalog
refreshes and scheduled retirements can legitimately leave stale contracts.
This check uses local data only and does not probe providers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trusted_router import catalog, request_capabilities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contracts", type=Path, default=request_capabilities._CONTRACT_PATH,
        help="Reviewed contracts JSON (defaults to the repo's request_capabilities.json)",
    )
    args = parser.parse_args(argv)
    data = json.loads(args.contracts.read_text())
    reviewed = {
        (provider, model_id)
        for row in data["contracts"]
        for provider in row["providers"]
        for model_id in row["models"]
    }
    present = {
        (endpoint.provider, endpoint.model_id)
        for endpoint in catalog.MODEL_ENDPOINTS.values()
    }
    missing = sorted(reviewed - present)
    if missing:
        print("Reviewed provider/model pairs with no catalog endpoint:")
        for provider, model_id in missing:
            print(f"  ({provider}, {model_id})")
        return 1
    print("All reviewed provider/model pairs have catalog endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
