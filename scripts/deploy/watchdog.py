#!/usr/bin/env python3
"""Per-region post-deploy watchdog.

Polls https://trustedrouter.com/status.json every minute and decides,
per `target_region`, whether to roll that region back. Writes the list
of regions-to-rollback to `$GITHUB_OUTPUT` (key `rollback_regions`,
comma-separated) so the workflow's rollback step can target only the
failing regions.

Per-region status: take the WORST `effective_status` among
`data.current.checks[]` whose `target_region` matches. Same logic the
status page uses for overall_status, applied per region.

Logic:
  - Poll once per minute for `--duration-min` minutes (default 10).
  - For each region, track a consecutive-down counter.
  - When ANY region reads "down" for `--rollback-after` consecutive
    minutes (default 3), that region is added to the rollback set,
    its counter resets, and the watchdog continues monitoring the
    other regions.
  - Exit 1 if at least one region is in the rollback set, else 0.

Why "down" only (not "degraded"): synthetics naturally flap during a
rolling update — only sustained-down means the deploy actually broke
something for that region. degraded alone is normal churn.

Why per-region: a deploy can break us-central1 but leave
europe-west4 healthy (e.g., bad regional secret rotation). Rolling
back globally over a single-region failure is overkill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections.abc import Iterable

# Worst-of: the per-region effective_status is the worst over its checks.
# `unknown` is treated as severity 0 for ranking but reported separately
# so the operator can tell "no signal" apart from "all healthy."
SEVERITY = {"up": 0, "degraded": 1, "down": 2}
INVERSE_SEVERITY = {0: "up", 1: "degraded", 2: "down"}


def fetch_per_region(url: str, regions: Iterable[str], timeout: int = 10) -> dict[str, str]:
    """Return {region: 'up'|'degraded'|'down'|'unknown'} for each requested region.

    A region maps to `unknown` when status.json had no checks targeting
    it. That distinguishes "monitor blackout" (status fetch ok, region
    has no recent probes) from a real "up" reading.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - deploy script uses operator-provided HTTPS status URL.
            payload = json.load(response)
        checks = payload.get("data", {}).get("current", {}).get("checks", []) or []
    except Exception as exc:
        print(f"  watchdog: status fetch error: {exc}", flush=True)
        # Fetch error -> unknown for every region (doesn't increment
        # the down counter; deploys aren't the suspect for an LB blip).
        return {region: "unknown" for region in regions}

    worst: dict[str, int] = {}
    for check in checks:
        target = (check or {}).get("target_region")
        status = (check or {}).get("effective_status") or (check or {}).get("status")
        if not target or not status:
            continue
        if target not in regions:
            continue
        sev = SEVERITY.get(str(status).lower())
        if sev is None:
            continue
        if sev > worst.get(target, -1):
            worst[target] = sev
    out: dict[str, str] = {}
    for region in regions:
        if region in worst:
            out[region] = INVERSE_SEVERITY[worst[region]]
        else:
            out[region] = "unknown"
    return out


def write_output(key: str, value: str) -> None:
    """Append a key=value line to $GITHUB_OUTPUT if defined.

    Falls back to stdout marker line for local runs.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    else:
        print(f"::output::{key}={value}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-min", type=int, default=10)
    parser.add_argument("--rollback-after", type=int, default=3)
    parser.add_argument(
        "--regions",
        default="us-central1,europe-west4",
        help="Comma-separated list of target regions to monitor.",
    )
    parser.add_argument(
        "--status-url",
        default="https://trustedrouter.com/status.json",
    )
    parser.add_argument(
        "--baseline-grace-sec",
        type=int,
        default=30,
        help=(
            "Wait this long before the first poll, then capture a "
            "baseline of regions that are ALREADY 'down' before the "
            "deploy is the suspect. Pre-existing flap (e.g. a flaky "
            "upstream provider that's broken globally) is held "
            "constant: a region whose status was 'down' in the "
            "baseline doesn't accrue 'consecutive_down' counts. "
            "Only NEW downs (regions that flipped from up/degraded "
            "to down after the canary) count toward rollback. This "
            "lets us keep an aggressive --rollback-after of 1 (block "
            "any deploy-introduced regression) without false-positive "
            "rollbacks when the underlying probe is already failing."
        ),
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help=(
            "Hotfix flag: skip baseline and use raw 'down' counting. "
            "Used by the emergency-fix workflow path that must ship "
            "regardless of pre-existing badness."
        ),
    )
    args = parser.parse_args()

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    consecutive_down = {region: 0 for region in regions}
    rollback_set: set[str] = set()

    # Baseline: the set of regions that were ALREADY 'down' before the
    # deploy. The canary watchdog is interested in whether the new
    # revision INTRODUCED a regression — not whether some unrelated
    # upstream is currently broken. Regions in the baseline-down set
    # are excluded from rollback accounting unless their status
    # transitions further (today: still treat as down — but we won't
    # blame the deploy for them).
    baseline_down: set[str] = set()
    if not args.skip_baseline:
        if args.baseline_grace_sec > 0:
            time.sleep(args.baseline_grace_sec)
        baseline_snapshot = fetch_per_region(args.status_url, regions)
        baseline_down = {r for r, s in baseline_snapshot.items() if s == "down"}
        if baseline_down:
            print(
                f"watchdog: baseline 'down' regions (not blamed on deploy): "
                f"{sorted(baseline_down)}",
                flush=True,
            )

    print(
        f"watchdog: polling {args.status_url} every 60s for {args.duration_min} min; "
        f"per-region rollback if 'down' for {args.rollback_after} consecutive minutes "
        f"AND not already 'down' before the deploy; regions={regions}",
        flush=True,
    )
    for minute in range(1, args.duration_min + 1):
        time.sleep(60)
        per_region = fetch_per_region(args.status_url, regions)
        line = f"  minute {minute}:"
        for region in regions:
            status = per_region.get(region, "unknown")
            if region in rollback_set:
                line += f"  {region}=ROLLED_BACK"
                continue
            # Only count "down" as deploy-caused if the region was
            # healthy in the baseline. If it was already down, the
            # deploy isn't the suspect; leave the counter at 0 so
            # we don't roll back over a pre-existing condition.
            if status == "down" and region not in baseline_down:
                consecutive_down[region] += 1
            else:
                consecutive_down[region] = 0
            tag = f"{status}({consecutive_down[region]})"
            if region in baseline_down:
                tag += "[baseline]"
            line += f"  {region}={tag}"
            if consecutive_down[region] >= args.rollback_after:
                rollback_set.add(region)
                print(
                    f"  watchdog: ROLLBACK {region} — 'down' for {consecutive_down[region]} consecutive minutes "
                    f"(was {'down' if region in baseline_down else 'healthy'} pre-deploy)",
                    flush=True,
                )
        print(line, flush=True)
        # If every region is rolled back, no point continuing the watch.
        if rollback_set == set(regions):
            break

    rollback_list = ",".join(sorted(rollback_set))
    write_output("rollback_regions", rollback_list)
    if rollback_set:
        print(f"watchdog: rolling back regions: {rollback_list}", flush=True)
        return 1
    print("watchdog: deploy healthy across all regions", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
