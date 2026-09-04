#!/usr/bin/env python3
"""Run recurring probes; pass --expect-stage-d for durable heartbeat checks."""

# ruff: noqa: I001

import sys

from trusted_router.synthetic.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
