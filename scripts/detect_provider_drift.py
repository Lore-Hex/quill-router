#!/usr/bin/env python3
"""Detect day-over-day provider/model API drift from benchmark samples.

Aggregates a recent window of ProviderBenchmarkSamples (organic + rotation
probe) and compares it against a committed baseline, flagging undocumented
upstream changes: a model that started erroring/404ing, a brand-new error
shape, a TTFT regression, or a newly-appeared model.

Usage:
    python scripts/detect_provider_drift.py                  # report drift
    python scripts/detect_provider_drift.py --check          # exit 1 if drift found (for CI / alerts)
    python scripts/detect_provider_drift.py --update-baseline  # rewrite the committed baseline

Reads the live store (prod GCP env) for the recent window. The committed
baseline lives at src/trusted_router/data/provider_drift_baseline.json; refresh
it deliberately via --update-baseline and review the diff in a PR.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from trusted_router.synthetic.drift import aggregate, baseline_from_stats, detect_drift

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BASELINE = REPO_ROOT / "src" / "trusted_router" / "data" / "provider_drift_baseline.json"
BASELINE_PATH = Path(os.environ.get("TR_DRIFT_BASELINE_PATH", str(_DEFAULT_BASELINE)))


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _recent_samples(limit: int) -> list:
    from trusted_router.storage import STORE

    return STORE.provider_benchmark_samples(date=None, limit=limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect provider/model API drift.")
    parser.add_argument("--limit", type=int, default=5000, help="recent samples to scan")
    parser.add_argument("--check", action="store_true", help="exit 1 if any drift is found")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the committed baseline from the current window",
    )
    parser.add_argument("--min-samples", type=int, default=5)
    args = parser.parse_args(argv)

    current = aggregate(_recent_samples(args.limit))
    total = sum(stats.sample_count for stats in current.values())

    if args.update_baseline:
        baseline = baseline_from_stats(current)
        BASELINE_PATH.write_text(
            json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote baseline for {len(baseline)} models ({total} samples) -> {BASELINE_PATH}")
        return 0

    findings = detect_drift(current, _load_baseline(), min_samples=args.min_samples)
    if not findings:
        print(f"no drift across {len(current)} models ({total} samples)")
        return 0
    print(f"DRIFT: {len(findings)} finding(s):")
    for finding in findings:
        print(f"  [{finding.kind}] {finding.provider}/{finding.model}: {finding.detail}")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
