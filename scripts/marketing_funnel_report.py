#!/usr/bin/env python3
"""Print TrustedRouter's first-party acquisition funnel by creative."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trusted_router.marketing_funnel import (  # noqa: E402
    aggregate_funnel_rows,
    build_axiom_funnel_query,
    parse_axiom_json_lines,
    render_markdown,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query metadata-only acquisition events and report engaged visits, "
            "signups, activated API users, purchases, and retention by UTM creative."
        )
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dataset", default="trusted-router-logs")
    parser.add_argument("--source")
    parser.add_argument("--campaign")
    parser.add_argument("--creative")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.days <= 365:
        raise SystemExit("--days must be between 1 and 365")
    axiom = shutil.which("axiom")
    if axiom is None:
        raise SystemExit("Axiom CLI is required. Install it and run `axiom auth login`.")
    query = build_axiom_funnel_query(args.dataset)
    completed = subprocess.run(  # noqa: S603 - executable is resolved by shutil.which.
        [
            axiom,
            "query",
            query,
            "--start-time",
            f"-{args.days}d",
            "--format",
            "json",
            "--no-spinner",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Axiom query failed"
        raise SystemExit(detail)
    rows = aggregate_funnel_rows(
        parse_axiom_json_lines(completed.stdout),
        source=args.source,
        campaign=args.campaign,
        creative=args.creative,
    )
    if args.format == "json":
        print(json.dumps([row.as_dict() for row in rows], indent=2, sort_keys=True))
    else:
        print(render_markdown(rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
