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


def _make_endpoint_snapshot(
    headline: tuple[str, str],
    endpoints: list[tuple[str, str, str, str]],
    *,
    model_id: str = "a/b",
) -> dict:
    return {
        "model_count": 1,
        "models": [
            {
                "id": model_id,
                "pricing": {"prompt": headline[0], "completion": headline[1]},
                "endpoints": [
                    {
                        "tr_provider_slug": provider,
                        "model_id": upstream,
                        "tag": provider,
                        "pricing": {"prompt": prompt, "completion": completion},
                    }
                    for provider, upstream, prompt, completion in endpoints
                ],
            }
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


def test_summary_surfaces_per_model_deltas(tmp_path: Path, capsys) -> None:
    """The drift-visibility fix: --summary must print the actual old->new
    delta per model, not just the count (so sub-2x changes are visible)."""
    from scripts.check_price_spike import main

    before = _write(tmp_path, "b.json", _make_snapshot({"a/b": ("0.00000004", "0.00000013")}))
    after = _write(tmp_path, "a.json", _make_snapshot({"a/b": ("0.00000005", "0.00000015")}))
    rc = main([str(before), str(after), "--summary"])
    out = capsys.readouterr().out
    assert rc == 0  # +25% is under the 2x spike gate
    assert "1 prices changed" in out
    assert "a/b:" in out  # the delta line is surfaced, not just the count


def test_route_removal_can_raise_headline_without_blocking_refresh(
    tmp_path: Path, capsys
) -> None:
    """A cheap route disappearing must not freeze every provider refresh."""
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000006", "0.00000006"),
            [
                ("cheap", "author/model", "0.00000006", "0.00000006"),
                ("steady", "author/model", "0.0000002", "0.0000006"),
            ],
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.0000002", "0.0000006"),
            [("steady", "author/model", "0.0000002", "0.0000006")],
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 0
    assert "a/b:" in capsys.readouterr().out


def test_same_provider_endpoint_spike_blocks_and_is_visible(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.0000001", "0.0000002"),
            [("provider", "Native/Model", "0.0000001", "0.0000002")],
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.0000001", "0.0000004"),
            [("provider", "Native/Model", "0.0000001", "0.0000004")],
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 1
    out = capsys.readouterr().out
    assert "PRICE SPIKE FAILURES" in out
    assert "provider" in out


def test_confirmed_tinfoil_kimi_k3_price_transition_is_allowed(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.000002", "0.000006"),
            [("tinfoil", "kimi-k3", "0.000002", "0.000006")],
            model_id="moonshotai/kimi-k3",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000255", "0.00001275"),
            [("tinfoil", "kimi-k3", "0.000004", "0.00002")],
            model_id="moonshotai/kimi-k3",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 0
    assert "moonshotai/kimi-k3" in capsys.readouterr().out


def test_confirmed_deepseek_and_gmi_price_transitions_are_allowed(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before_payload = _make_endpoint_snapshot(
        ("0.000000347999", "0.000000695999"),
        [
            ("deepseek", "deepseek-v4-pro", "0.000000435", "0.00000087"),
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Pro",
                "0.000000347999",
                "0.000000695999",
            ),
        ],
        model_id="deepseek/deepseek-v4-pro",
    )
    before_payload["models"][0]["endpoints"][1]["tag"] = "gmicloud/fp8"
    before = _write(tmp_path, "before.json", before_payload)

    after_payload = _make_endpoint_snapshot(
        ("0.00000066", "0.000001392"),
        [
            ("deepseek", "deepseek-v4-pro", "0.00000066", "0.00000198"),
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Pro",
                "0.000000696",
                "0.000001392",
            ),
        ],
        model_id="deepseek/deepseek-v4-pro",
    )
    after_payload["models"][0]["endpoints"][1]["tag"] = "gmicloud/fp8"
    after = _write(tmp_path, "after.json", after_payload)

    assert main([str(before), str(after), "--summary"]) == 0
    assert "deepseek/deepseek-v4-pro" in capsys.readouterr().out


def test_unapproved_deepseek_price_transition_still_blocks(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000014", "0.00000028"),
            [("deepseek", "deepseek-v4-flash", "0.00000014", "0.00000028")],
            model_id="deepseek/deepseek-v4-flash",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000022", "0.00000067"),
            [("deepseek", "deepseek-v4-flash", "0.00000022", "0.00000067")],
            model_id="deepseek/deepseek-v4-flash",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 1
    assert "PRICE SPIKE FAILURES" in capsys.readouterr().out


def test_unapproved_tinfoil_kimi_k3_price_transition_still_blocks(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.000002", "0.000006"),
            [("tinfoil", "kimi-k3", "0.000002", "0.000006")],
            model_id="moonshotai/kimi-k3",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.000005", "0.000021"),
            [("tinfoil", "kimi-k3", "0.000005", "0.000021")],
            model_id="moonshotai/kimi-k3",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 1
    assert "PRICE SPIKE FAILURES" in capsys.readouterr().out
