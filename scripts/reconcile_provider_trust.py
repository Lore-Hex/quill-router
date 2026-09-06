#!/usr/bin/env python3
"""Compatibility wrapper; the installed module also runs in the production image."""
from trusted_router.provider_trust_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
