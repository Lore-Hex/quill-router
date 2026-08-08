from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts.deploy.verify_operational_parity_history import (
    VerificationError,
    verify_history,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 8, 8, 12, tzinfo=UTC)
STARTED = dt.datetime(2026, 8, 1, 2, 32, 30, tzinfo=UTC)
MINIMUM_SECONDS = 7 * 24 * 60 * 60


def _row(checked_at: dt.datetime, *, ok: bool = True) -> dict[str, object]:
    return {
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "ok": ok,
        "surfaces": {
            surface: {"sampled": 5000, "found": 5000, "missing": 0, "ok": ok}
            for surface in ("benchmark", "activity", "synthetic", "rollup")
        },
    }


def _clean_rows() -> list[dict[str, object]]:
    first = NOW - dt.timedelta(days=7)
    return [_row(first + dt.timedelta(minutes=30 * index)) for index in range(337)]


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _verify(path: Path) -> dict[str, object]:
    return verify_history(
        path,
        started_at=STARTED,
        minimum_seconds=MINIMUM_SECONDS,
        now=NOW,
    )


def test_historical_failure_outside_rolling_clean_window_is_preserved_but_recovered(
    tmp_path: Path,
) -> None:
    history = tmp_path / "parity.jsonl"
    historical_failure = _row(dt.datetime(2026, 8, 1, 7, 20, tzinfo=UTC), ok=False)
    _write(history, [historical_failure, *_clean_rows()])

    summary = _verify(history)

    assert summary["sample_count"] == 337
    assert summary["window_start"] == "2026-08-01T12:00:00Z"


def test_failure_inside_rolling_window_blocks_cutover(tmp_path: Path) -> None:
    history = tmp_path / "parity.jsonl"
    rows = _clean_rows()
    rows[100] = _row(NOW - dt.timedelta(days=5), ok=False)
    _write(history, rows)

    with pytest.raises(VerificationError, match="contains a failed check"):
        _verify(history)


def test_stale_latest_sample_blocks_cutover(tmp_path: Path) -> None:
    history = tmp_path / "parity.jsonl"
    _write(history, _clean_rows()[:-3])

    with pytest.raises(VerificationError, match="latest operational parity sample is stale"):
        _verify(history)


def test_sampling_gap_blocks_cutover(tmp_path: Path) -> None:
    history = tmp_path / "parity.jsonl"
    rows = _clean_rows()
    del rows[100:103]
    _write(history, rows)

    with pytest.raises(VerificationError, match="contains a sampling gap"):
        _verify(history)


def test_each_surface_requires_positive_parity_evidence(tmp_path: Path) -> None:
    history = tmp_path / "parity.jsonl"
    rows = _clean_rows()
    for row in rows:
        row["surfaces"]["rollup"]["sampled"] = 0  # type: ignore[index]
    _write(history, rows)

    with pytest.raises(VerificationError, match="no positive parity evidence for rollup"):
        _verify(history)


def test_malformed_history_blocks_cutover(tmp_path: Path) -> None:
    history = tmp_path / "parity.jsonl"
    history.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(VerificationError, match="invalid parity sample on line 1"):
        _verify(history)
