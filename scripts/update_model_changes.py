#!/usr/bin/env python3
"""Record catalog changes, or fail CI with the exact regeneration command."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_router.model_changes import (
    HISTORY_PATH,
    STATE_PATH,
    apply_changes,
    canonical,
    change,
    diff_catalogs,
    route_state,
    timestamp,
)

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ".venv/bin/python scripts/update_model_changes.py"


def read_snapshot(at: str, *, source: Path = ROOT, freshness_at: str | None = None) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603 - local reader, no shell or network
        [sys.executable, str(ROOT / "scripts/model_change_snapshot.py"), at, freshness_at or at],
        env={**os.environ, "PYTHONPATH": str(source / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=source, check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def projection(at: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    initial = read_snapshot(at)
    models = route_state(initial["rows"])
    planned = []
    previous = models
    for cutover in initial["cutovers"]:
        # Announced lifecycle projections keep today's discovery evidence.
        # Freshness expiry is conditional on the hourly refresh failing, not
        # an announced retirement. Actual expiry is captured on the next update.
        following = route_state(read_snapshot(cutover, freshness_at=at)["rows"])
        planned.extend(diff_catalogs(previous, following, cutover, source="scheduled"))
        previous = following
    for retirement in initial["retirements"]:
        for model in retirement["models"]:
            planned.append(change(model, "retirement_scheduled", None,
                                  {"provider": retirement["provider"]},
                                  retirement["effective_at"], source="scheduled"))
    return models, planned


def signature(event: dict[str, Any]) -> str:
    return canonical({key: value for key, value in event.items() if key not in {"id", "recorded_at"}})


def record(event: dict[str, Any], at: str, occurrence: int = 0) -> dict[str, Any]:
    row = {**event, "recorded_at": at}
    # Reinstating a previously cancelled schedule must create a fresh Atom id.
    suffix = f":{occurrence}" if occurrence else ""
    row["id"] = hashlib.sha256((signature(event) + at + suffix).encode()).hexdigest()[:32]
    return row


def update(
    state_path: Path, history_path: Path, at: str, *, check: bool = False,
) -> bool:
    previous = json.loads(state_path.read_text()) if state_path.exists() else None
    if check and previous is None:
        return False
    evaluation_at = previous["as_of"] if check else at
    models, planned = projection(evaluation_at)
    history_bytes = history_path.read_bytes() if history_path.exists() else b""
    if check:
        return (
            models == previous["models"]
            and sorted(map(signature, planned)) == sorted(map(signature, previous["scheduled"]))
            and hashlib.sha256(history_bytes).hexdigest() == previous["history_sha256"]
        )
    if previous and at < previous["as_of"]:
        raise ValueError("Cannot record an observation before the recorded baseline")
    history = [json.loads(line) for line in history_bytes.splitlines() if line]
    ids = {event["id"] for event in history}

    def append_record(event: dict[str, Any]) -> dict[str, Any]:
        occurrence = 0
        row = record(event, at)
        # Several local updates can share one second (or an explicit --at).
        # A reinstated forecast must not inherit its cancelled predecessor's id.
        while row["id"] in ids:
            occurrence += 1
            row = record(event, at, occurrence)
        ids.add(row["id"])
        history.append(row)
        return row

    pending = previous["scheduled"] if previous else []
    expected = apply_changes(previous["models"], (e for e in pending if e["effective_at"] <= at)) if previous else models
    additions = diff_catalogs(expected, models, at)
    existing = {signature(e): e for e in pending if e["effective_at"] > at}
    desired = {signature(e): e for e in planned}
    for key, event in existing.items():
        if key not in desired:
            additions.append(change(event["model"], "schedule_cancelled",
                                    {"id": event["id"], "type": event["type"],
                                     "effective_at": event["effective_at"]}, None, at))
    scheduled = []
    for key, event in desired.items():
        if key in existing:
            scheduled.append(existing[key])
        else:
            row = append_record(event)
            scheduled.append(row)
    for event in additions:
        append_record(event)
    history.sort(key=lambda e: (e["recorded_at"], e["id"]))
    content = "".join(canonical(e) + "\n" for e in history).encode()
    state = {"version": 1, "as_of": at, "models": models, "scheduled": scheduled,
             "history_sha256": hashlib.sha256(content).hexdigest()}
    history_path.write_bytes(content)
    state_path.write_text(canonical(state) + "\n")
    print(f"Recorded {len(additions)} catalog changes; {len(scheduled)} scheduled entries; "
          f"history {len(content):,} bytes")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--at", default=None, help="UTC observation timestamp (default: now)")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    args = parser.parse_args()
    at = timestamp(datetime.fromisoformat(args.at.replace("Z", "+00:00"))) if args.at else timestamp(datetime.now(UTC))
    if not update(args.state, args.history, at, check=args.check):
        print(f"Catalog change history is stale. Run: {COMMAND}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
