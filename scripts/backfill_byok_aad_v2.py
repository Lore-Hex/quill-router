#!/usr/bin/env python3
"""Refuse use of the retired mutating BYOK AAD migration command."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "RETIRED: the AAD v1 backfill was removed after Step 4. "
        "Use scripts/check_no_v1_envelopes.py for the fail-closed read-only audit."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
