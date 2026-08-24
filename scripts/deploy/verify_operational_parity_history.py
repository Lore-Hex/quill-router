#!/usr/bin/env python3
"""Verify a continuous rolling window of operational analytics parity."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

SURFACES = ("benchmark", "activity", "synthetic", "rollup")
DEFAULT_MAX_SAMPLE_GAP_SECONDS = 3600


class VerificationError(RuntimeError):
    """The parity history does not prove a clean cutover window."""


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(dt.UTC)


def _valid_rows(path: Path, *, started_at: dt.datetime) -> list[tuple[dt.datetime, dict[str, Any]]]:
    rows: list[tuple[dt.datetime, dict[str, Any]]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
            checked_at = _timestamp(row["checked_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationError(f"invalid parity sample on line {line_number}") from exc
        if not isinstance(row, dict):
            raise VerificationError(f"invalid parity sample on line {line_number}")
        if checked_at >= started_at:
            rows.append((checked_at, row))
    rows.sort(key=lambda item: item[0])
    return rows


def verify_history(
    path: Path,
    *,
    started_at: dt.datetime,
    minimum_seconds: int,
    now: dt.datetime | None = None,
    max_sample_gap_seconds: int = DEFAULT_MAX_SAMPLE_GAP_SECONDS,
) -> dict[str, object]:
    if minimum_seconds < 1:
        raise ValueError("minimum_seconds must be positive")
    if max_sample_gap_seconds < 1:
        raise ValueError("max_sample_gap_seconds must be positive")
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    started = started_at.astimezone(dt.UTC)
    minimum = dt.timedelta(seconds=minimum_seconds)
    max_gap = dt.timedelta(seconds=max_sample_gap_seconds)
    if current - started < minimum:
        raise VerificationError("dual-read soak has not reached the minimum duration")

    window_start = max(started, current - minimum)
    rows = [item for item in _valid_rows(path, started_at=started) if item[0] >= window_start]
    required = max(1, math.floor(minimum_seconds / 3600))
    if len(rows) < required:
        raise VerificationError(f"only {len(rows)} parity samples; require at least {required}")

    first_checked = rows[0][0]
    latest_checked = rows[-1][0]
    if first_checked - window_start > max_gap:
        raise VerificationError("operational parity evidence starts too late in the clean window")
    if current - latest_checked > max_gap:
        raise VerificationError("latest operational parity sample is stale")
    if latest_checked - current > dt.timedelta(minutes=5):
        raise VerificationError("latest operational parity sample is in the future")

    for (previous, _), (checked_at, _) in zip(rows, rows[1:], strict=False):
        if checked_at - previous > max_gap:
            raise VerificationError(
                "operational parity history contains a sampling gap "
                f"from {previous.isoformat()} to {checked_at.isoformat()}"
            )

    failures = [checked_at for checked_at, row in rows if row.get("ok") is not True]
    if failures:
        raise VerificationError(
            "operational parity history contains a failed check at "
            f"{failures[-1].isoformat()}"
        )
    for surface in SURFACES:
        if not any(
            isinstance(row.get("surfaces"), dict)
            and isinstance(row["surfaces"].get(surface), dict)
            and row["surfaces"][surface].get("sampled", 0) > 0
            for _, row in rows
        ):
            raise VerificationError(f"no positive parity evidence for {surface}")

    max_observed_gap = max(
        (
            (checked_at - previous).total_seconds()
            for (previous, _), (checked_at, _) in zip(rows, rows[1:], strict=False)
        ),
        default=0.0,
    )
    return {
        "window_start": window_start.isoformat().replace("+00:00", "Z"),
        "first_checked_at": first_checked.isoformat().replace("+00:00", "Z"),
        "latest_checked_at": latest_checked.isoformat().replace("+00:00", "Z"),
        "sample_count": len(rows),
        "max_gap_seconds": int(max_observed_gap),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--minimum-seconds", required=True, type=int)
    parser.add_argument("--now")
    parser.add_argument(
        "--max-sample-gap-seconds",
        type=int,
        default=DEFAULT_MAX_SAMPLE_GAP_SECONDS,
    )
    args = parser.parse_args()
    try:
        summary = verify_history(
            args.history,
            started_at=_timestamp(args.started_at),
            minimum_seconds=args.minimum_seconds,
            now=_timestamp(args.now) if args.now else None,
            max_sample_gap_seconds=args.max_sample_gap_seconds,
        )
    except (OSError, ValueError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
