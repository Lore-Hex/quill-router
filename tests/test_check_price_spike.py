"""Tests for scripts/check_price_spike.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_price_spike import _load_provider_manifests, _summary_line, check

ROOT = Path(__file__).parents[1]
AZURE_MANIFEST = ROOT / "src" / "trusted_router" / "data" / "provider_models" / "azure.json"
AZURE_OPENROUTER_ABSENT_IDS = frozenset(
    {
        "cohere/command-a",
        "x-ai/grok-4.1-fast-non-reasoning",
        "x-ai/grok-4.1-fast-reasoning",
        "x-ai/grok-4.20-non-reasoning",
        "x-ai/grok-4.20-reasoning",
    }
)
AZURE_SPIKE_TARGET = "x-ai/grok-4.20-reasoning"


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


def _write_provider_manifest_dir(
    tmp_path: Path,
    name: str,
    manifest: dict,
) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True)
    (path / "azure.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _azure_manifest_case(
    tmp_path: Path,
    *,
    target_completion_price: int | None,
) -> tuple[list[str], int]:
    before_manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    after_manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    target_before = next(
        row for row in before_manifest["models"] if row["id"] == AZURE_SPIKE_TARGET
    )
    target_after = next(
        row for row in after_manifest["models"] if row["id"] == AZURE_SPIKE_TARGET
    )
    original_completion_price = target_before["output_token_price_per_m"]
    if target_completion_price is not None:
        target_after["output_token_price_per_m"] = target_completion_price

    before_provider_dir = _write_provider_manifest_dir(
        tmp_path,
        "before-provider-models",
        before_manifest,
    )
    after_provider_dir = _write_provider_manifest_dir(
        tmp_path,
        "after-provider-models",
        after_manifest,
    )

    # The OpenRouter fixture deliberately contains only Azure's four models
    # that are also present in the real snapshot. The target is one of the five
    # supplemental-only routes, so this regression cannot pass via the legacy
    # OpenRouter comparison.
    openrouter_prices = {
        row["id"]: ("0.000001", "0.000002")
        for row in before_manifest["models"]
        if row["id"] not in AZURE_OPENROUTER_ABSENT_IDS
    }
    before_snapshot = _write(
        tmp_path,
        "before.json",
        _make_snapshot(openrouter_prices),
    )
    after_snapshot = _write(
        tmp_path,
        "after.json",
        _make_snapshot(openrouter_prices),
    )
    return (
        [
            str(before_snapshot),
            str(after_snapshot),
            "--before-provider-manifests",
            str(before_provider_dir),
            "--after-provider-manifests",
            str(after_provider_dir),
            "--summary",
        ],
        original_completion_price,
    )


def _to_prices(snapshot: dict) -> dict[str, dict[str, str]]:
    return {m["id"]: m["pricing"] for m in snapshot["models"]}


def test_provider_manifest_loader_covers_all_azure_exact9() -> None:
    manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    manifest_ids = {row["id"] for row in manifest["models"]}

    assert len(manifest_ids) == 9
    assert AZURE_OPENROUTER_ABSENT_IDS <= manifest_ids

    loaded = _load_provider_manifests(AZURE_MANIFEST.parent)
    expected_base_routes = {
        f"{row['id']} [azure:azure:{row['upstream_id']}]"
        for row in manifest["models"]
    }
    assert len(expected_base_routes) == len(manifest_ids) == 9
    assert expected_base_routes <= loaded.keys()
    expected_cached_routes = {
        f"{row['id']} [azure:azure:{row['upstream_id']}] cached-input"
        for row in manifest["models"]
        if "cached_input_token_price_per_m" in row
    }
    assert len(expected_cached_routes) == 4
    assert expected_cached_routes <= loaded.keys()


def test_supplemental_only_azure_2x_rate_change_fails(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, original = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    after = json.loads((after_dir / "azure.json").read_text(encoding="utf-8"))
    target = next(row for row in after["models"] if row["id"] == AZURE_SPIKE_TARGET)
    target["output_token_price_per_m"] = original * 2
    (after_dir / "azure.json").write_text(json.dumps(after), encoding="utf-8")

    assert main(args) == 1
    out = capsys.readouterr().out
    assert "PRICE SPIKE FAILURES" in out
    assert (
        "x-ai/grok-4.20-reasoning "
        "[azure:azure:grok-4-20-reasoning] completion"
    ) in out


def test_supplemental_only_azure_ordinary_rates_pass(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    unchanged_args, original = _azure_manifest_case(
        tmp_path / "unchanged",
        target_completion_price=None,
    )
    assert main(unchanged_args) == 0
    assert "PRICE SPIKE FAILURES" not in capsys.readouterr().out

    below_threshold_args, _ = _azure_manifest_case(
        tmp_path / "below-threshold",
        target_completion_price=original * 2 - 1,
    )
    assert main(below_threshold_args) == 0
    below_output = capsys.readouterr().out
    assert AZURE_SPIKE_TARGET in below_output
    assert "PRICE SPIKE FAILURES" not in below_output


def test_azure_cached_input_2x_rate_change_fails(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    after = json.loads((after_dir / "azure.json").read_text(encoding="utf-8"))
    target = next(row for row in after["models"] if row["id"] == "moonshotai/kimi-k2.5")
    target["cached_input_token_price_per_m"] *= 2
    (after_dir / "azure.json").write_text(json.dumps(after), encoding="utf-8")

    assert main(args) == 1
    out = capsys.readouterr().out
    assert (
        "moonshotai/kimi-k2.5 [azure:azure:kimi-k2-5] "
        "cached-input prompt"
    ) in out


def test_azure_tier_prompt_completion_and_cached_2x_rates_fail(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    for option in ("--before-provider-manifests", "--after-provider-manifests"):
        manifest_dir = Path(args[args.index(option) + 1])
        raw = json.loads((manifest_dir / "azure.json").read_text(encoding="utf-8"))
        target = next(row for row in raw["models"] if row["id"] == AZURE_SPIKE_TARGET)
        target["price_tiers"] = [
            {
                "max_prompt_tokens": 200_000,
                "input_token_price_per_m": 1_250_000,
                "output_token_price_per_m": 2_500_000,
                "cached_input_token_price_per_m": 250_000,
            },
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 2_500_000,
                "output_token_price_per_m": 5_000_000,
                "cached_input_token_price_per_m": 500_000,
            },
        ]
        (manifest_dir / "azure.json").write_text(json.dumps(raw), encoding="utf-8")

    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    after = json.loads((after_dir / "azure.json").read_text(encoding="utf-8"))
    target = next(row for row in after["models"] if row["id"] == AZURE_SPIKE_TARGET)
    target["price_tiers"][0]["input_token_price_per_m"] *= 2
    target["price_tiers"][1]["output_token_price_per_m"] *= 2
    target["price_tiers"][1]["cached_input_token_price_per_m"] *= 2
    (after_dir / "azure.json").write_text(json.dumps(after), encoding="utf-8")

    assert main(args) == 1
    out = capsys.readouterr().out
    route = (
        "x-ai/grok-4.20-reasoning "
        "[azure:azure:grok-4-20-reasoning]"
    )
    assert f"{route} tier[max=200000] prompt" in out
    assert f"{route} tier[uncapped] completion" in out
    assert f"{route} tier[uncapped] cached-input prompt" in out


def test_provider_manifest_comparison_keeps_openrouter_endpoint_gate(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    before = _make_endpoint_snapshot(
        ("0.0000001", "0.0000002"),
        [("provider", "Native/Model", "0.0000001", "0.0000002")],
    )
    after = _make_endpoint_snapshot(
        ("0.0000001", "0.0000004"),
        [("provider", "Native/Model", "0.0000001", "0.0000004")],
    )
    _write(tmp_path, "before.json", before)
    _write(tmp_path, "after.json", after)

    assert main(args) == 1
    out = capsys.readouterr().out
    assert "a/b [provider:provider:Native/Model] completion" in out


def test_provider_manifest_input_failure_fails_closed(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    (after_dir / "azure.json").write_text("not json", encoding="utf-8")

    assert main(args) == 2
    assert "PRICE SPIKE INPUT ERROR" in capsys.readouterr().err


def test_provider_manifest_disappearance_fails_closed(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    (after_dir / "azure.json").unlink()
    (after_dir / "replacement.json").write_text(
        json.dumps({"provider": "replacement", "models": []}),
        encoding="utf-8",
    )

    assert main(args) == 2
    assert (
        "provider manifests disappeared after refresh: azure.json"
        in capsys.readouterr().err
    )


def test_individual_depriced_provider_row_remains_nonblocking(
    tmp_path: Path,
    capsys,
) -> None:
    from scripts.check_price_spike import main

    args, _ = _azure_manifest_case(
        tmp_path,
        target_completion_price=None,
    )
    after_dir = Path(args[args.index("--after-provider-manifests") + 1])
    after = json.loads((after_dir / "azure.json").read_text(encoding="utf-8"))
    target = next(row for row in after["models"] if row["id"] == AZURE_SPIKE_TARGET)
    target.pop("input_token_price_per_m")
    target.pop("output_token_price_per_m")
    target["routable"] = False
    target["routable_reason"] = "price-unavailable"
    (after_dir / "azure.json").write_text(json.dumps(after), encoding="utf-8")

    assert main(args) == 0
    out = capsys.readouterr().out
    assert "1 models removed" in out
    assert AZURE_SPIKE_TARGET in out


def test_provider_manifest_loader_rejects_partial_price_pair(tmp_path: Path) -> None:
    manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    manifest["models"][0].pop("output_token_price_per_m")
    manifest_dir = _write_provider_manifest_dir(tmp_path, "partial", manifest)

    with pytest.raises(ValueError, match="both input and output prices"):
        _load_provider_manifests(manifest_dir)


def test_provider_manifest_loader_rejects_duplicate_stable_rate(tmp_path: Path) -> None:
    manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    manifest["models"].append(dict(manifest["models"][0]))
    manifest_dir = _write_provider_manifest_dir(tmp_path, "duplicate", manifest)

    with pytest.raises(ValueError, match="duplicate provider-manifest rate"):
        _load_provider_manifests(manifest_dir)


def test_hourly_workflow_gates_supplemental_provider_manifests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "refresh-prices.yml").read_text(
        encoding="utf-8"
    )

    assert "cp src/trusted_router/data/provider_models/*.json" in workflow
    assert "--before-provider-manifests /tmp/before-provider-models" in workflow
    assert (
        "--after-provider-manifests src/trusted_router/data/provider_models"
        in workflow
    )


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


def test_confirmed_atlas_v4_flash_0731_transition_is_allowed(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000008", "0.00000018"),
            [
                (
                    "atlas-cloud",
                    "deepseek/deepseek-v4-flash-0731",
                    "0.00000014",
                    "0.00000028",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000008", "0.00000018"),
            [
                (
                    "atlas-cloud",
                    "deepseek/deepseek-v4-flash-0731",
                    "0.00000044",
                    "0.00000132",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 0
    assert "PRICE SPIKE FAILURES" not in capsys.readouterr().out


def test_different_atlas_v4_flash_0731_transition_still_blocks(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000008", "0.00000018"),
            [
                (
                    "atlas-cloud",
                    "deepseek/deepseek-v4-flash-0731",
                    "0.00000014",
                    "0.00000028",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000008", "0.00000018"),
            [
                (
                    "atlas-cloud",
                    "deepseek/deepseek-v4-flash-0731",
                    "0.00000045",
                    "0.00000132",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 1
    assert "PRICE SPIKE FAILURES" in capsys.readouterr().out


@pytest.mark.parametrize("endpoint_tag", ["novita", "novita/fp8"])
def test_confirmed_novita_v4_flash_0731_transition_is_allowed(
    tmp_path: Path, capsys, endpoint_tag: str
) -> None:
    from scripts.check_price_spike import main

    before_payload = _make_endpoint_snapshot(
        ("0.00000008", "0.00000018"),
        [
            (
                "novita",
                "deepseek/deepseek-v4-flash-0731",
                "0.00000014",
                "0.00000028",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    before_payload["models"][0]["endpoints"][0]["tag"] = endpoint_tag
    before = _write(tmp_path, "before.json", before_payload)
    after_payload = _make_endpoint_snapshot(
        ("0.00000008", "0.00000018"),
        [
            (
                "novita",
                "deepseek/deepseek-v4-flash-0731",
                "0.00000044",
                "0.00000132",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    after_payload["models"][0]["endpoints"][0]["tag"] = endpoint_tag
    after = _write(tmp_path, "after.json", after_payload)

    assert main([str(before), str(after), "--summary"]) == 0
    assert "PRICE SPIKE FAILURES" not in capsys.readouterr().out


@pytest.mark.parametrize("endpoint_tag", ["novita", "novita/fp8"])
def test_different_novita_v4_flash_0731_transition_still_blocks(
    tmp_path: Path, capsys, endpoint_tag: str
) -> None:
    from scripts.check_price_spike import main

    before_payload = _make_endpoint_snapshot(
        ("0.00000008", "0.00000018"),
        [
            (
                "novita",
                "deepseek/deepseek-v4-flash-0731",
                "0.00000014",
                "0.00000028",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    before_payload["models"][0]["endpoints"][0]["tag"] = endpoint_tag
    before = _write(tmp_path, "before.json", before_payload)
    after_payload = _make_endpoint_snapshot(
        ("0.00000008", "0.00000018"),
        [
            (
                "novita",
                "deepseek/deepseek-v4-flash-0731",
                "0.00000045",
                "0.00000132",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    after_payload["models"][0]["endpoints"][0]["tag"] = endpoint_tag
    after = _write(tmp_path, "after.json", after_payload)

    assert main([str(before), str(after), "--summary"]) == 1
    assert "PRICE SPIKE FAILURES" in capsys.readouterr().out


def test_confirmed_fireworks_v4_flash_0731_transition_is_allowed(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000014", "0.00000028"),
            [
                (
                    "fireworks",
                    "accounts/fireworks/models/deepseek-v4-flash-0731",
                    "0.00000014",
                    "0.00000028",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000022", "0.00000066"),
            [
                (
                    "fireworks",
                    "accounts/fireworks/models/deepseek-v4-flash-0731",
                    "0.00000022",
                    "0.00000066",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 0
    assert "PRICE SPIKE FAILURES" not in capsys.readouterr().out


def test_different_fireworks_v4_flash_0731_transition_still_blocks(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before = _write(
        tmp_path,
        "before.json",
        _make_endpoint_snapshot(
            ("0.00000014", "0.00000028"),
            [
                (
                    "fireworks",
                    "accounts/fireworks/models/deepseek-v4-flash-0731",
                    "0.00000014",
                    "0.00000028",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )
    after = _write(
        tmp_path,
        "after.json",
        _make_endpoint_snapshot(
            ("0.00000022", "0.00000067"),
            [
                (
                    "fireworks",
                    "accounts/fireworks/models/deepseek-v4-flash-0731",
                    "0.00000022",
                    "0.00000067",
                )
            ],
            model_id="deepseek/deepseek-v4-flash-0731",
        ),
    )

    assert main([str(before), str(after), "--summary"]) == 1
    assert "PRICE SPIKE FAILURES" in capsys.readouterr().out


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


def test_confirmed_io_net_price_transitions_are_allowed() -> None:
    mistral = (
        "mistralai/mistral-nemo-instruct-2407 "
        "[io-net:io-net:mistralai/Mistral-Nemo-Instruct-2407]"
    )
    mistral_cached = f"{mistral} cached-input"
    glm_cached = (
        "z-ai/glm-5.3-flash "
        "[io-net:io-net:zai-org/GLM-5.3-Flash] cached-input"
    )
    before = {
        mistral: {"prompt": "0.000000029667", "completion": "0.000000076667"},
        mistral_cached: {"prompt": "0.000000014834", "completion": "0"},
        glm_cached: {"prompt": "0.00000003", "completion": "0"},
    }
    after = {
        mistral: {"prompt": "0.0000000635", "completion": "0.00000009875"},
        mistral_cached: {"prompt": "0.00000003175", "completion": "0"},
        glm_cached: {"prompt": "0.00000006", "completion": "0"},
    }

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == 3
    assert removed == []


@pytest.mark.parametrize(
    ("route", "old_price", "unapproved_price"),
    [
        (
            "mistralai/mistral-nemo-instruct-2407 "
            "[io-net:io-net:mistralai/Mistral-Nemo-Instruct-2407]",
            "0.000000029667",
            "0.0000000636",
        ),
        (
            "mistralai/mistral-nemo-instruct-2407 "
            "[io-net:io-net:mistralai/Mistral-Nemo-Instruct-2407] cached-input",
            "0.000000014834",
            "0.00000003176",
        ),
        (
            "z-ai/glm-5.3-flash "
            "[io-net:io-net:zai-org/GLM-5.3-Flash] cached-input",
            "0.00000003",
            "0.000000061",
        ),
    ],
)
def test_different_io_net_price_transitions_still_block(
    route: str,
    old_price: str,
    unapproved_price: str,
) -> None:
    before = {route: {"prompt": old_price, "completion": "0"}}
    after = {route: {"prompt": unapproved_price, "completion": "0"}}

    failures, _changes, _removed = check(before, after)

    assert len(failures) == 1
    assert route in failures[0]
