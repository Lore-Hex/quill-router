"""Merge downloaded CI durations into an explicitly chosen output file.

Usage: python scripts/ci/merge_shard_durations.py ARTIFACT_DIR CURRENT OUTPUT
New observations replace current weights using the maximum across artifacts.
IDs without new observations retain their current weights.
"""

from __future__ import annotations

import argparse
import json
import math
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, help="Directory downloaded by gh run download")
    parser.add_argument("current", type=Path, help="Current .test_durations file")
    parser.add_argument("output", type=Path, help="Explicit destination for the merged JSON")
    args = parser.parse_args(argv)
    merge_shard_durations(args.artifacts, args.current, args.output)


if __name__ == "__main__":
    main()
