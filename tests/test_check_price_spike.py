"""Tests for scripts/check_price_spike.py."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.check_price_spike import _summary_line, check


def _make_snapshot(prices: dict[str, tuple[str, str]]) -> dict:
    return {
        "model_count": len(prices),
        "models": [
            {
                "id": model_id,
                "pricing": {"prompt": prompt, "completion": completion},
                "endpoints": [],
            }
            for model_id, (prompt, completion) in prices.items()
        ],
    }


def _write(tmp_path: Path, name: str, snapshot: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _to_prices(snapshot: dict) -> dict[str, dict[str, str]]:
    return {m["id"]: m["pricing"] for m in snapshot["models"]}


def test_no_changes_produces_no_failures_and_no_changes_list() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000002")}))
    after = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000002")}))
    failures, changes, removed = check(before, after)
    assert failures == []
    assert changes == []
    assert removed == []


def test_2x_prompt_increase_fails() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000001")}))
    after = _to_prices(_make_snapshot({"a/b": ("0.000002", "0.000001")}))
    failures, _, _ = check(before, after)
    assert any("a/b prompt" in f for f in failures)


def test_2x_completion_increase_fails() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000001")}))
    after = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000002")}))
    failures, _, _ = check(before, after)
    assert any("a/b completion" in f for f in failures)


def test_just_under_2x_passes() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000001")}))
    after = _to_prices(_make_snapshot({"a/b": ("0.0000019", "0.0000019")}))
    failures, changes, _ = check(before, after)
    assert failures == []
    # Both dimensions changed, so the changes list is populated.
    assert any("a/b" in c for c in changes)


def test_both_prices_to_zero_fails() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000002")}))
    after = _to_prices(_make_snapshot({"a/b": ("0", "0")}))
    failures, _, _ = check(before, after)
    assert any("both prompt and completion went to 0" in f for f in failures)


def test_only_prompt_to_zero_passes() -> None:
    before = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000002")}))
    after = _to_prices(_make_snapshot({"a/b": ("0", "0.000002")}))
    failures, _, _ = check(before, after)
    # Only one dimension to zero is fine — could be a tier change.
    assert all("both prompt" not in f for f in failures)


def test_removed_model_does_not_fail_but_is_listed() -> None:
    before = _to_prices(_make_snapshot({
        "a/b": ("0.000001", "0.000002"),
        "c/d": ("0.000003", "0.000004"),
    }))
    after = _to_prices(_make_snapshot({
        "a/b": ("0.000001", "0.000002"),
    }))
    failures, _, removed = check(before, after)
    assert failures == []
    assert "c/d" in removed


def test_summary_line_zero_changes() -> None:
    assert _summary_line([], []) == "no price changes"


def test_summary_line_with_changes_and_removals() -> None:
    line = _summary_line(["a", "b", "c"], ["x/y"])
    assert "3 prices changed" in line
    assert "1 models removed" in line


def test_decrease_does_not_fail() -> None:
    """Even a 100x decrease (i.e., a free tier kicking in) should not
    fail the spike check — only increases ≥2× fail."""
    before = _to_prices(_make_snapshot({"a/b": ("0.0001", "0.0001")}))
    after = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000001")}))
    failures, _, _ = check(before, after)
    assert failures == []
