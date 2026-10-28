"""Merge downloaded CI durations into an explicitly chosen output file.

Usage: python scripts/ci/merge_shard_durations.py ARTIFACT_DIR CURRENT OUTPUT
New observations replace current weights using the maximum across artifacts.
IDs without new observations retain their current weights.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def read_durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        for value in data.values()
    ):
        raise ValueError(f"{path}: expected test IDs mapped to finite, nonnegative durations")
    return data


def merge_shard_durations(artifacts: Path, current: Path, output: Path) -> None:
    files = sorted(artifacts.rglob("durations-*.json"))
    if not files:
        raise ValueError(f"{artifacts}: no durations-*.json files found")
    existing = read_durations(current)
    recorded: dict[str, float] = {}
    for path in files:
        for nodeid, duration in read_durations(path).items():
            recorded[nodeid] = max(recorded.get(nodeid, duration), duration)

    added = len(recorded.keys() - existing.keys())
    updated = sum(
        duration != existing[nodeid]
        for nodeid, duration in recorded.items()
        if nodeid in existing
    )
    kept = len(existing) - updated
    merged = existing | recorded
    output.write_text(json.dumps(merged, sort_keys=True, indent=4) + "\n", encoding="utf-8")
    print(f"Added {added} IDs, updated {updated} IDs, kept {kept} IDs")


def profile_runs(artifacts: Path, current: Path, output: Path, clock: str) -> None:
    """Median of independent successful CI runs, never mix coverage and clocks.

    Download each run under ARTIFACT_DIR/RUN_ID with gh run download. At least
    three runs are required; a new test with fewer samples uses its maximum
    until enough evidence exists. Unobserved tests retain their prior weight.
    Keep xdist group suffixes: loadgroup still serializes database operations.
    """
    samples: dict[str, list[float]] = defaultdict(list)
    runs = []
    for run in sorted(artifacts.iterdir()):
        files = sorted(run.glob(f"durations-{clock}-*/durations-*.json"))
        if not files:
            continue
        observed: dict[str, float] = {}
        for path in files:
            data = read_durations(path)
            if observed.keys() & data.keys():
                raise ValueError(f"{run}: duplicate test observations across shards")
            observed.update(data)
        runs.append(run.name)
        for nodeid, value in observed.items():
            samples[nodeid].append(value)
    if len(runs) < 3:
        raise ValueError("profiling requires at least three independent successful CI runs")
    existing = read_durations(current)
    measured = {
        key: round(statistics.median(values) if len(values) >= 3 else max(values), 6)
        for key, values in samples.items()
    }
    output.write_text(json.dumps(existing | measured, sort_keys=True, indent=4) + "\n", encoding="utf-8")
    print(f"{clock}: runs={','.join(runs)}; measured={len(measured)}; "
          f"new={len(measured.keys() - existing.keys())}; "
          f"repeated={sum(len(values) >= 3 for values in samples.values())}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, help="Directory downloaded by gh run download")
    parser.add_argument("current", type=Path, help="Current .test_durations file")
    parser.add_argument("output", type=Path, help="Explicit destination for the merged JSON")
    parser.add_argument("--profile", choices=("default", "post-cutover"),
                        help="Use separate-clock medians from at least three run directories")
    args = parser.parse_args(argv)
    if args.profile:
        profile_runs(args.artifacts, args.current, args.output, args.profile)
    else:
        merge_shard_durations(args.artifacts, args.current, args.output)


if __name__ == "__main__":
    main()
