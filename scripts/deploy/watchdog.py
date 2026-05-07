#!/usr/bin/env python3
"""Post-deploy watchdog: polls https://trustedrouter.com/status.json
and decides whether to rollback.

Logic:
  - Poll once per minute for `--duration-min` minutes (default 10).
  - Track `overall_status`. If it's "down" for `--rollback-after`
    consecutive checks (default 3), exit non-zero so the GHA workflow
    triggers its rollback step.
  - "degraded" alone doesn't trigger rollback — synthetics flap during
    rolling updates and we don't want to rollback over normal churn.
  - Exit 0 if the watchdog window completes without 3 consecutive
    "down" reads; the deploy is declared healthy.

Synthetics are external probes (Cloud Scheduler hits the prod LB every
minute, results aggregated into status.json). Polling that already-
public endpoint means the watchdog has zero new infrastructure and is
re-using the same signal customers see at /status.

Usage:
  python3 scripts/deploy/watchdog.py \
    [--duration-min 10] [--rollback-after 3] [--status-url https://trustedrouter.com/status.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def fetch_status(url: str, timeout: int = 10) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
        status = payload.get("data", {}).get("overall_status")
        if isinstance(status, str):
            return status.lower()
    except Exception as exc:
        print(f"  watchdog: status fetch error: {exc}", flush=True)
        # A fetch error counts as "degraded" — not a deploy regression
        # signal on its own (could be an LB/DNS blip). Returning
        # "unknown" prevents the consecutive-down counter from rising.
        return "unknown"
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-min", type=int, default=10)
    parser.add_argument("--rollback-after", type=int, default=3)
    parser.add_argument(
        "--status-url",
        default="https://trustedrouter.com/status.json",
    )
    args = parser.parse_args()

    consecutive_down = 0
    print(
        f"watchdog: polling {args.status_url} every 60s for {args.duration_min} min; "
        f"rollback if 'down' for {args.rollback_after} consecutive minutes",
        flush=True,
    )
    for minute in range(1, args.duration_min + 1):
        time.sleep(60)
        status = fetch_status(args.status_url)
        if status == "down":
            consecutive_down += 1
        else:
            consecutive_down = 0
        print(
            f"  minute {minute}: status={status}  consecutive_down={consecutive_down}",
            flush=True,
        )
        if consecutive_down >= args.rollback_after:
            print(
                f"watchdog: ROLLBACK — 'down' for {consecutive_down} consecutive minutes",
                flush=True,
            )
            return 1
    print("watchdog: deploy healthy", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
