"""Decide whether generated prices can be carried onto a newer main.

The CLI reads NUL-delimited paths from ``git diff --name-only --no-renames -z``
on stdin, including both sides of renames. A rebase still requires full CI on
its new exact SHA before publication.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Literal

# Generated files staged by refresh-prices.yml, plus their producing code.
# Directory entries include every descendant (and the directory itself).
REFRESH_PATHS = (
    "src/trusted_router/data/openrouter_snapshot.json",
    "src/trusted_router/data/speech_pricing.json",
    "src/trusted_router/data/provider_models/",
    "src/trusted_router/data/model_changes.jsonl",
    "src/trusted_router/data/model_changes_state.json",
    "src/trusted_router/static/og/providers/",
    "scripts/pricing/",  # Includes the staged self-healed parsers/ output.
    "scripts/ingest_openrouter_catalog.py",
    "scripts/check_price_spike.py",
    "scripts/update_model_changes.py",
    "src/trusted_router/provider_lifecycle.py",
    "src/trusted_router/provider_manifest_policy.py",
    ".github/workflows/refresh-prices.yml",
)


def publish_decision(changed_paths: Iterable[str]) -> Literal["rebase", "recompute"]:
    """Reuse generated data only when no refresh input or output changed."""
    for path in changed_paths:
        for refresh_path in REFRESH_PATHS:
            if path == refresh_path.rstrip("/") or (
                refresh_path.endswith("/") and path.startswith(refresh_path)
            ):
                return "recompute"
    return "rebase"


def main() -> None:
    print(publish_decision(sys.stdin.read().split("\0")))


if __name__ == "__main__":
    main()
