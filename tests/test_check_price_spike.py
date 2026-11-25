"""Tests for scripts/check_price_spike.py."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import check_price_spike
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


@pytest.mark.parametrize(("suffix", "dimension", "old", "new"), [
    ("", "prompt", "0.00000004", "0.0000002"),
    ("", "completion", "0.00000015", "0.00000075"),
    (" cached-input", "prompt", "0.000000004", "0.00000002"),
])
def test_november_1_inception_tariff_approval_is_exact(suffix, dimension, old, new):
    route = "inception/mercury-2.5 [inception:inception:mercury-2.5]" + suffix
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []

    assert check(before, {route: {
        **after[route], dimension: str(Decimal(new) + Decimal("0.000000000001")),
    }})[0]
    assert check({route: {
        **before[route], dimension: str(Decimal(old) / 2),
    }}, after)[0]
    assert check(after, {route: {
        **after[route], dimension: str(Decimal(new) * 2),
    }})[0]
    other_dimension = "prompt" if dimension == "completion" else "completion"
    assert check(
        {route: {"prompt": "1", "completion": "1", other_dimension: old}},
        {route: {"prompt": "1", "completion": "1", other_dimension: new}},
    )[0]
    for other_route in (
        route.replace("inception:inception:", "other:other:"),
        route.replace("inception:inception:", "inception:inception/fp8:"),
        route.replace(":mercury-2.5]", ":mercury-3]"),
        route.replace("inception/mercury-2.5 ", "inception/mercury-3 "),
        route.removesuffix(" cached-input") if suffix else route + " cached-input",
        "inception/mercury-2.5",
    ):
        assert check({other_route: before[route]}, {other_route: after[route]})[0]


def test_november_1_inception_snapshot_and_manifest_spikes_pass(tmp_path, monkeypatch):
    model_id, native = "inception/mercury-2.5", "mercury-2.5"
    paths = []
    for side, prompt, completion, cached in (
        ("before", 40_000, 150_000, 4_000),
        ("after", 200_000, 750_000, 20_000),
    ):
        snapshot = _write(tmp_path, side + ".json", _make_endpoint_snapshot(
            ("1", "1"),
            [("inception", native, str(Decimal(prompt) / 10**12),
              str(Decimal(completion) / 10**12))],
            model_id=model_id,
        ))
        directory = tmp_path / side
        directory.mkdir()
        (directory / "inception.json").write_text(json.dumps({
            "provider": "inception", "models": [{
                "id": model_id, "upstream_id": native,
                "input_token_price_per_m": prompt,
                "output_token_price_per_m": completion,
                "cached_input_token_price_per_m": cached,
            }],
        }))
        paths.append((snapshot, directory))
    args = (paths[0][0], paths[1][0], paths[0][1], paths[1][1])
    assert check_price_spike.spiking_providers(*args) == {}
    for pair in check_price_spike.route_pairs(*args):
        assert pair is not None
        assert check(*pair)[0] == []

    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.spiking_providers(*args) == {
        "inception": [
            f"{model_id} [inception:inception:{native}]",
            f"{model_id} [inception:inception:{native}]",
            f"{model_id} [inception:inception:{native}] cached-input",
        ],
    }

    # Each held route names the exact before and after values, and those are
    # precisely what an APPROVED_ENDPOINT_PRICE_TRANSITIONS entry must match.
    held = check_price_spike.spiking_providers(*args)["inception"]
    assert [route.reasons for route in held] == [
        ("prompt: 4E-8 → 2E-7 (×5.00 ≥ ×2.0)", "completion: 1.5E-7 → 7.5E-7 (×5.00 ≥ ×2.0)"),
        ("prompt: 4E-8 → 2E-7 (×5.00 ≥ ×2.0)", "completion: 1.5E-7 → 7.5E-7 (×5.00 ≥ ×2.0)"),
        ("prompt: 4E-9 → 2E-8 (×5.00 ≥ ×2.0)",),
    ]
    approvals = set()
    for route in held:
        for reason in route.reasons:
            dimension, values = reason.split(": ", 1)
            before_value, after_value = values.split(" (", 1)[0].split(" → ")
            approvals.add((str(route), dimension, Decimal(before_value), Decimal(after_value)))
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset(approvals))
    assert check_price_spike.spiking_providers(*args) == {}


def test_a_zeroed_route_reason_drops_the_route_prefix():
    route = "acme/model [acme:acme:model]"
    reasons = check_price_spike.failing_route_reasons(
        {route: {"prompt": "0.000001", "completion": "0.000002"}},
        {route: {"prompt": "0", "completion": "0"}},
    )
    assert reasons == {route: [f"{route}: both prompt and completion went to 0 (was prompt=0.000001, completion=0.000002)"]}
    spiking = check_price_spike.SpikingRoute(route, ("both prompt and completion went to 0",))
    assert spiking == route and hash(spiking) == hash(route) and sorted([spiking, "a"]) == ["a", route]


@pytest.mark.parametrize("native,suffix,dimension,old,new", [
    (native, suffix, dimension, old, new)
    for native in ("xiaomi/mimo-v2.5", "XiaomiMiMo/MiMo-V2.5")
    for suffix, dimension, old, new in (
        ("", "prompt", "0.000000199", "0.0000004"),
        ("", "completion", "0.000000638", "0.000002"),
        (" cached-input", "prompt", "0.0000000995", "0.0000002"),
    ) if not suffix or native == "XiaomiMiMo/MiMo-V2.5"
])
def test_november_1_io_net_tariff_approval_is_exact(native, suffix, dimension, old, new):
    route = f"xiaomi/mimo-v2.5 [io-net:io-net:{native}]" + suffix
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []
    assert check(before, {route: {
        **after[route], dimension: str(Decimal(new) + Decimal("0.000000000001")),
    }})[0]
    assert check({route: {**before[route], dimension: str(Decimal(old) / 2)}}, after)[0]
    assert check(after, {route: {**after[route], dimension: str(Decimal(new) * 2)}})[0]
    other_dimension = "prompt" if dimension == "completion" else "completion"
    assert check(
        {route: {"prompt": "1", "completion": "1", other_dimension: old}},
        {route: {"prompt": "1", "completion": "1", other_dimension: new}},
    )[0]
    for other in (
        route.replace("io-net:io-net:", "other:other:"),
        route.replace("io-net:io-net:", "io-net:io-net/fp8:"),
        route.replace(native + "]", "XiaomiMiMo/MiMo-V2.6]"),
        route.replace("xiaomi/mimo-v2.5 ", "xiaomi/mimo-v2.6 "),
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2.5 [io-net:io-net:xiaomi/mimo-v2.5] cached-input",
    ):
        assert check({other: before[route]}, {other: after[route]})[0]


def test_november_1_io_net_snapshot_and_manifest_spikes_pass(tmp_path, monkeypatch):
    model_id, native = "xiaomi/mimo-v2.5", "XiaomiMiMo/MiMo-V2.5"
    paths = []
    for side, prompt, completion, cached in (
        ("before", 199_000, 638_000, 99_500),
        ("after", 400_000, 2_000_000, 200_000),
    ):
        snapshot = _write(tmp_path, side + ".json", _make_endpoint_snapshot(
            ("1", "1"),
            [("io-net", model_id, str(Decimal(prompt) / 10**12), str(Decimal(completion) / 10**12))],
            model_id=model_id,
        ))
        directory = tmp_path / side
        directory.mkdir()
        (directory / "io-net.json").write_text(json.dumps({
            "provider": "io-net", "models": [{
                "id": model_id, "upstream_id": native,
                "input_token_price_per_m": prompt,
                "output_token_price_per_m": completion,
                "cached_input_token_price_per_m": cached,
            }],
        }))
        paths.append((snapshot, directory))
    args = (paths[0][0], paths[1][0], paths[0][1], paths[1][1])
    assert check_price_spike.spiking_providers(*args) == {}
    for pair in check_price_spike.route_pairs(*args):
        assert pair is not None
        assert check(*pair)[0] == []
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.spiking_providers(*args) == {
        "io-net": [
            f"{model_id} [io-net:io-net:{model_id}]",
            f"{model_id} [io-net:io-net:{native}]",
            f"{model_id} [io-net:io-net:{native}] cached-input",
        ],
    }


def test_akashml_kimi_cache_price_review_is_exact():
    route = "moonshotai/kimi-k3 [akashml:akashml:moonshotai/Kimi-K3] cached-input"
    before = {route: {"prompt": "0.0000003", "completion": "0"}}
    after = {route: {"prompt": "0.0000012", "completion": "0"}}
    assert check(before, after)[0] == []
    assert check(before, {route: {"prompt": "0.0000013", "completion": "0"}})[0]
    for other in (route.replace("akashml:akashml:", "other:other:"), route.removesuffix(" cached-input")):
        assert check({other: before[route]}, {other: after[route]})[0]


def test_siliconflow_kimi_k25_cache_price_review_is_exact():
    # Exact values from the held hourly refreshes of 2026-11-24.
    route = "moonshotai/kimi-k2.5 [siliconflow:siliconflow:moonshotai/Kimi-K2.5] cached-input"
    before = {route: {"prompt": "7E-8", "completion": "0"}}
    after = {route: {"prompt": "1.5E-7", "completion": "0"}}
    assert check(before, after)[0] == []
    assert check(before, {route: {"prompt": "1.6E-7", "completion": "0"}})[0]
    for other in (route.replace("siliconflow:siliconflow:", "other:other:"), route.removesuffix(" cached-input")):
        assert check({other: before[route]}, {other: after[route]})[0]


def test_tencent_step5_verified_output_correction_is_exact():
    route = "stepfun/step-5-preview [tencent:tencent:step-5-preview]"
    before = {route: {"prompt": "0.000001", "completion": "0.00000027"}}
    after = {route: {"prompt": "0.000001", "completion": "0.0000027"}}
    assert check(before, after)[0] == []
    for bad in ("0.00000271", "0.000027"):
        assert check(before, {route: {**after[route], "completion": bad}})[0]
    for other in (route.replace("tencent:tencent:", "other:other:"), route + " cached-input"):
        assert check({other: before[route]}, {other: after[route]})[0]
    assert check({route: {"prompt": "0.00000027", "completion": "1"}},
                 {route: {"prompt": "0.0000027", "completion": "1"}})[0]


@pytest.mark.parametrize(("route", "dimension", "old", "new"), [
    (
        "deepseek/deepseek-v4.1-flash [wafer:wafer:DeepSeek-V4.1-Flash]",
        "completion", "0.0000006", "0.0000012",
    ),
    (
        "qwen/qwen3.8-27b [wafer:wafer:Qwen3.8-27B]",
        "prompt", "0.00000011", "0.00000042",
    ),
    (
        "deepseek/deepseek-v4-flash-0423 [wafer:wafer:DeepSeek-V4-Flash-0423]",
        "prompt", "0.00000007", "0.00000014",
    ),
])
def test_october_11_wafer_approval_is_exact(route, dimension, old, new):
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []

    # No wildcard for later increases, different baselines, dimensions or routes.
    assert check(before, {route: {
        **after[route], dimension: str(Decimal(new) + Decimal("0.000000000001")),
    }})[0]
    assert check({route: {
        **before[route], dimension: str(Decimal(old) / 2),
    }}, after)[0]
    other_dimension = "prompt" if dimension == "completion" else "completion"
    assert check(
        {route: {"prompt": "1", "completion": "1", other_dimension: old}},
        {route: {"prompt": "1", "completion": "1", other_dimension: new}},
    )[0]
    for other_route in (route.replace("wafer:wafer:", "other:other:"), route + " cached-input"):
        assert check({other_route: before[route]}, {other_route: after[route]})[0]


@pytest.mark.parametrize(("dimension", "old", "new"), [
    ("prompt", "0.0000001078", "0.0000004312"),
    ("completion", "0.00000028", "0.00000112"),
])
def test_october_11_featherless_approval_is_exact(dimension, old, new):
    route = (
        "xiaomi/mimo-v2.6-distill-qwen-9b "
        "[featherless:featherless:XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B]"
    )
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []
    assert check(before, {route: {
        **after[route], dimension: str(Decimal(new) + Decimal("0.000000000001")),
    }})[0]
    assert check({route: {
        **before[route], dimension: str(Decimal(old) / 2),
    }}, after)[0]
    other_dimension = "prompt" if dimension == "completion" else "completion"
    assert check(
        {route: {"prompt": "1", "completion": "1", other_dimension: old}},
        {route: {"prompt": "1", "completion": "1", other_dimension: new}},
    )[0]
    for other_route in (
        route.replace("featherless:featherless:", "other:other:"), route + " cached-input",
    ):
        assert check({other_route: before[route]}, {other_route: after[route]})[0]


def test_october_11_featherless_snapshot_and_manifest_spikes_pass(tmp_path, monkeypatch):
    model_id = "xiaomi/mimo-v2.6-distill-qwen-9b"
    native = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    paths = []
    for side, prompt, completion in (
        ("before", 107_800, 280_000), ("after", 431_200, 1_120_000),
    ):
        snapshot = _write(tmp_path, side + ".json", _make_endpoint_snapshot(
            ("1", "1"),
            [("featherless", native, str(Decimal(prompt) / 10**12),
              str(Decimal(completion) / 10**12))],
            model_id=model_id,
        ))
        directory = tmp_path / side
        directory.mkdir()
        (directory / "featherless.json").write_text(json.dumps({
            "provider": "featherless", "models": [{
                "id": model_id, "upstream_id": native,
                "input_token_price_per_m": prompt,
                "output_token_price_per_m": completion,
            }],
        }))
        paths.append((snapshot, directory))
    args = (paths[0][0], paths[1][0], paths[0][1], paths[1][1])
    assert check_price_spike.spiking_providers(*args) == {}
    for pair in check_price_spike.route_pairs(*args):
        assert pair is not None
        assert check(*pair)[0] == []

    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.spiking_providers(*args) == {
        "featherless": [f"{model_id} [featherless:featherless:{native}]"] * 2,
    }


def test_october_11_wafer_snapshot_and_manifest_spikes_pass(tmp_path, monkeypatch):
    cases = [
        ("deepseek/deepseek-v4.1-flash", "DeepSeek-V4.1-Flash",
         (200_000, 600_000, 10_000), (300_000, 1_200_000, 6_000)),
        ("qwen/qwen3.8-27b", "Qwen3.8-27B",
         (110_000, 2_500_000, 50_000), (420_000, 2_550_000, 85_000)),
        ("deepseek/deepseek-v4-flash-0423", "DeepSeek-V4-Flash-0423",
         (70_000, 250_000, 20_000), (140_000, 280_000, 28_000)),
    ]
    paths = []
    for side in ("before", "after"):
        directory = tmp_path / side
        directory.mkdir()
        models, manifest_rows = [], []
        for model_id, native, old, new in cases:
            prompt, completion, cached = old if side == "before" else new
            models.extend(_make_endpoint_snapshot(
                ("1", "1"),
                [("wafer", native, str(Decimal(prompt) / 10**12),
                  str(Decimal(completion) / 10**12))],
                model_id=model_id,
            )["models"])
            manifest_rows.append({
                "id": model_id, "upstream_id": native,
                "input_token_price_per_m": prompt,
                "output_token_price_per_m": completion,
                "cached_input_token_price_per_m": cached,
            })
        snapshot = _write(tmp_path, side + ".json", {"models": models})
        (directory / "wafer.json").write_text(json.dumps({
            "provider": "wafer", "models": manifest_rows,
        }))
        paths.append((snapshot, directory))
    args = (paths[0][0], paths[1][0], paths[0][1], paths[1][1])
    assert check_price_spike.spiking_providers(*args) == {}
    for pair in check_price_spike.route_pairs(*args):
        assert pair is not None
        assert check(*pair)[0] == []

    # Both comparisons use the same native route labels, hence duplicate holds.
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    held = check_price_spike.spiking_providers(*args)
    assert sorted(held["wafer"]) == sorted(
        [f"{model_id} [wafer:wafer:{native}]" for model_id, native, _, _ in cases] * 2
    )


@pytest.mark.parametrize(("suffix", "dimension", "old", "new"), [
    ("", "prompt", "0.00000015", "0.00000066"),
    ("", "completion", "0.0000006", "0.00000198"),
    (" cached-input", "prompt", "0.000000003", "0.000000022"),
])
def test_deepseek_pro_launch_day_correction_is_exactly_scoped(suffix, dimension, old, new):
    route = "deepseek/deepseek-v4-pro [deepseek:deepseek:deepseek-v4-pro]" + suffix
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []
    after[route][dimension] = str(Decimal(new) + Decimal("0.000000000001"))
    assert check(before, after)[0]
    wrong_provider = route.replace("[deepseek:deepseek:", "[baseten:baseten:")
    assert check(
        {wrong_provider: before[route]},
        {wrong_provider: {**before[route], dimension: new}},
    )[0]


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


def test_provider_manifest_loader_covers_azure_chat_and_decision() -> None:
    manifest = json.loads(AZURE_MANIFEST.read_text(encoding="utf-8"))
    manifest_ids = {row["id"] for row in manifest["models"]}

    assert len(manifest_ids) == 10
    assert AZURE_OPENROUTER_ABSENT_IDS <= manifest_ids

    loaded = _load_provider_manifests(AZURE_MANIFEST.parent)
    expected_base_routes = {
        f"{row['id']} [azure:azure:{row['upstream_id']}]"
        for row in manifest["models"]
    }
    assert len(expected_base_routes) == len(manifest_ids) == 10
    assert expected_base_routes <= loaded.keys()
    decision = loaded["microsoft/microsoft-decision-1 [azure:azure:tr-decision-1]"]
    assert Decimal(decision["prompt"]) == Decimal("0.000000042")
    assert Decimal(decision["completion"]) == 0
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


def test_mass_model_removal_fails_the_gate(tmp_path: Path, capsys) -> None:
    """2026-11-19: an OpenRouter /endpoints outage dropped 95 of 174 models."""
    from scripts.check_price_spike import main

    prices = {f"a/m{i}": ("0.000001", "0.000002") for i in range(174)}
    kept = dict(list(prices.items())[:79])
    before = _write(tmp_path, "b.json", _make_snapshot(prices))
    after = _write(tmp_path, "a.json", _make_snapshot(kept))
    assert main([str(before), str(after), "--summary"]) == 1
    out = capsys.readouterr().out
    assert "95/174 catalog models removed" in out
    assert main([str(before), str(after)]) == 1


def test_ordinary_retirements_stay_nonblocking(tmp_path: Path) -> None:
    from scripts.check_price_spike import main

    prices = {f"a/m{i}": ("0.000001", "0.000002") for i in range(174)}
    kept = dict(list(prices.items())[:140])  # 34 removed, under 20%
    before = _write(tmp_path, "b.json", _make_snapshot(prices))
    after = _write(tmp_path, "a.json", _make_snapshot(kept))
    assert main([str(before), str(after), "--summary"]) == 0


def test_mass_removal_threshold_is_exact() -> None:
    from scripts.check_price_spike import mass_removal_failure

    before = {f"a/m{i}": {"prompt": "1", "completion": "1"} for i in range(100)}
    assert mass_removal_failure(before, [f"a/m{i}" for i in range(20)]) is None
    assert mass_removal_failure(before, [f"a/m{i}" for i in range(21)])
    small = {f"a/m{i}": {"prompt": "1", "completion": "1"} for i in range(10)}
    # Below the absolute floor a large fraction alone is not an outage.
    assert mass_removal_failure(small, [f"a/m{i}" for i in range(9)]) is None


def test_summary_line_zero_changes() -> None:
    assert _summary_line([], []) == "no price changes"


def test_summary_line_with_changes_and_removals() -> None:
    line = _summary_line(["a", "b", "c"], ["x/y"])
    assert "3 prices changed" in line
    assert "1 models removed" in line


def test_decrease_does_not_fail_below_the_drop_ratio() -> None:
    """Ordinary decreases, even 99x, do not fail. Since 2026-11-19 a 100x or
    larger cut does: no real cut on a continuing route reached ÷16 in the 213
    refreshes from 2026-08-21 to 2026-11-19, while a misread unit is ÷1000 or
    more. (A free tier, both prices to 0, has always failed separately.)"""
    before = _to_prices(_make_snapshot({"a/b": ("0.0001", "0.0001")}))
    almost = _to_prices(_make_snapshot({"a/b": ("0.00000102", "0.00000102")}))
    assert check(before, almost)[0] == []
    after = _to_prices(_make_snapshot({"a/b": ("0.000001", "0.000001")}))
    assert check(before, after)[0] == [
        "a/b prompt: 0.0001 → 0.000001 (÷100.00 ≥ ÷100.0)",
        "a/b completion: 0.0001 → 0.000001 (÷100.00 ≥ ÷100.0)",
    ]


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


def test_confirmed_gmi_deepseek_flash_0731_transition_is_allowed(
    tmp_path: Path, capsys
) -> None:
    from scripts.check_price_spike import main

    before_payload = _make_endpoint_snapshot(
        ("0.00000014", "0.00000028"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Flash-0731",
                "0.00000014",
                "0.00000028",
            ),
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    before_payload["models"][0]["endpoints"][0]["tag"] = "gmi"
    before = _write(tmp_path, "before.json", before_payload)

    after_payload = _make_endpoint_snapshot(
        ("0.00000044", "0.00000132"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Flash-0731",
                "0.00000044",
                "0.00000132",
            ),
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    after_payload["models"][0]["endpoints"][0]["tag"] = "gmi"
    after = _write(tmp_path, "after.json", after_payload)

    assert main([str(before), str(after), "--summary"]) == 0
    assert "deepseek/deepseek-v4-flash-0731" in capsys.readouterr().out


def test_confirmed_gmi_snapshot_deepseek_price_transitions_are_allowed(
    tmp_path: Path, capsys
) -> None:
    """Approve only the exact list-price corrections seen in GMI's feed."""

    from scripts.check_price_spike import main

    flash_before = _make_endpoint_snapshot(
        ("0.00000014", "0.00000028"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Flash-0731",
                "0.00000014",
                "0.00000028",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    flash_after = _make_endpoint_snapshot(
        ("0.00000044", "0.00000132"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Flash-0731",
                "0.00000044",
                "0.00000132",
            )
        ],
        model_id="deepseek/deepseek-v4-flash-0731",
    )
    for payload in (flash_before, flash_after):
        payload["models"][0]["endpoints"][0]["tag"] = "gmicloud/fp8"

    pro_before = _make_endpoint_snapshot(
        ("0.00000132", "0.00000396"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Pro",
                "0.00000132",
                "0.00000396",
            )
        ],
        model_id="deepseek/deepseek-v4-pro",
    )
    pro_after = _make_endpoint_snapshot(
        ("0.00000174", "0.00000348"),
        [
            (
                "gmi",
                "deepseek-ai/DeepSeek-V4-Pro",
                "0.00000174",
                "0.00000348",
            )
        ],
        model_id="deepseek/deepseek-v4-pro",
    )
    for payload, cached in ((pro_before, "0.000000044"), (pro_after, "0.000000145")):
        endpoint = payload["models"][0]["endpoints"][0]
        endpoint["tag"] = "gmicloud/fp8"
        endpoint["pricing"]["input_cache_read"] = cached

    before = _write(
        tmp_path,
        "before.json",
        {
            "model_count": 2,
            "models": flash_before["models"] + pro_before["models"],
        },
    )
    after = _write(
        tmp_path,
        "after.json",
        {
            "model_count": 2,
            "models": flash_after["models"] + pro_after["models"],
        },
    )

    assert main([str(before), str(after), "--summary"]) == 0
    output = capsys.readouterr().out
    assert "deepseek/deepseek-v4-flash-0731" in output
    assert "deepseek/deepseek-v4-pro" in output


def test_confirmed_gmi_deepseek_pro_cached_transition_is_allowed() -> None:
    route = (
        "deepseek/deepseek-v4-pro "
        "[gmi:gmicloud/fp8:deepseek-ai/DeepSeek-V4-Pro] cached-input"
    )

    failures, changes, removed = check(
        {route: {"prompt": "0.000000044", "completion": "0"}},
        {route: {"prompt": "0.000000145", "completion": "0"}},
    )

    assert failures == []
    assert len(changes) == 1
    assert removed == []


def test_confirmed_gmi_canonical_deepseek_pro_cached_transition_is_allowed() -> None:
    route = (
        "deepseek/deepseek-v4-pro "
        "[gmi:gmi:deepseek-ai/DeepSeek-V4-Pro] cached-input"
    )

    failures, changes, removed = check(
        {route: {"prompt": "0.000000044", "completion": "0"}},
        {route: {"prompt": "0.000000145", "completion": "0"}},
    )

    assert failures == []
    assert len(changes) == 1
    assert removed == []


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
        glm_cached: {"prompt": "0.000000075", "completion": "0"},
    }

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == 3
    assert removed == []


@pytest.mark.parametrize("native", ["deepseek/deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash"])
def test_verified_io_net_v41_price_transition_is_exact(native: str) -> None:
    route = f"deepseek/deepseek-v4.1-flash [io-net:io-net:{native}]"
    before = {route: {"prompt": "0.00000015", "completion": "0.0000012"}}
    after = {route: {"prompt": "0.000000315", "completion": "0.00000126"}}
    assert check(before, after)[0] == []
    after[route]["prompt"] = "0.000000316"
    assert check(before, after)[0]


def test_verified_io_net_v41_cached_price_does_not_approve_other_routes() -> None:
    route = "deepseek/deepseek-v4.1-flash [io-net:io-net:deepseek-ai/DeepSeek-V4.1-Flash] cached-input"
    before = {route: {"prompt": "0.000000003", "completion": "0"}}
    after = {route: {"prompt": "0.0000001575", "completion": "0"}}
    assert check(before, after)[0] == []
    after[route]["prompt"] = "0.000000158"
    assert check(before, after)[0]
    other = route.replace("io-net:io-net", "other:other")
    assert check(
        {other: {"prompt": "0.000000003", "completion": "0"}},
        {other: {"prompt": "0.0000001575", "completion": "0"}},
    )[0]


def test_verified_io_net_pro_0813_cache_transition_is_exact() -> None:
    route = "deepseek/deepseek-v4-pro-0813 [io-net:io-net:deepseek-ai/DeepSeek-V4-Pro-0813] cached-input"
    before = {route: {"prompt": "0.00000014", "completion": "0"}}
    after = {route: {"prompt": "0.000000462", "completion": "0"}}
    assert check(before, after)[0] == []
    after[route]["prompt"] = "0.000000463"
    assert check(before, after)[0]
    other = route.replace("io-net:io-net", "other:other")
    assert check(
        {other: {"prompt": "0.00000014", "completion": "0"}},
        {other: {"prompt": "0.000000462", "completion": "0"}},
    )[0]


@pytest.mark.parametrize("mutate", [False, True])
def test_verified_nextbit_transition_does_not_allow_further_increases(mutate: bool) -> None:
    route = "deepseek/deepseek-v4-flash-0731 [nextbit:nextbit:deepseek:v4-flash-0731]"
    before = {route: {"prompt": "0.00000016", "completion": "0.0000003"}}
    after = {route: {"prompt": "0.000000352", "completion": "0.000001056"}}
    if mutate:
        after[route]["completion"] = "0.000001057"
    assert bool(check(before, after)[0]) is mutate


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
            "0.000000076",
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


def test_confirmed_digitalocean_official_price_transitions_are_allowed() -> None:
    v32 = "deepseek/deepseek-v3.2 [digitalocean:digitalocean:deepseek-3.2]"
    flash = (
        "deepseek/deepseek-v4-flash "
        "[digitalocean:digitalocean:deepseek-4-flash]"
    )
    pro = (
        "deepseek/deepseek-v4-pro "
        "[digitalocean:digitalocean:deepseek-v4-pro]"
    )
    mimo = "xiaomi/mimo-v2.5-pro [digitalocean:digitalocean:mimo-v2.5-pro]"
    glm = "z-ai/glm-5.2 [digitalocean:digitalocean:glm-5.2]"
    before = {
        v32: {"prompt": "0.00000025", "completion": "0.0000008"},
        f"{v32} cached-input": {"prompt": "0.000000075", "completion": "0"},
        flash: {"prompt": "0.000000068", "completion": "0.000000168"},
        pro: {"prompt": "0.00000087", "completion": "0.00000174"},
        f"{pro} cached-input": {"prompt": "0.000000174", "completion": "0"},
        mimo: {"prompt": "0.0000004", "completion": "0.0000015"},
        f"{mimo} cached-input": {"prompt": "0.00000008", "completion": "0"},
        glm: {"prompt": "0.0000007", "completion": "0.0000022"},
        f"{glm} cached-input": {"prompt": "0.000000105", "completion": "0"},
    }
    after = {
        v32: {"prompt": "0.0000005", "completion": "0.0000016"},
        f"{v32} cached-input": {"prompt": "0.00000015", "completion": "0"},
        flash: {"prompt": "0.00000014", "completion": "0.00000028"},
        pro: {"prompt": "0.00000174", "completion": "0.00000348"},
        f"{pro} cached-input": {"prompt": "0.000000348", "completion": "0"},
        mimo: {"prompt": "0.0000008", "completion": "0.000003"},
        f"{mimo} cached-input": {"prompt": "0.00000016", "completion": "0"},
        glm: {"prompt": "0.0000014", "completion": "0.0000044"},
        f"{glm} cached-input": {"prompt": "0.00000021", "completion": "0"},
    }

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == len(before)
    assert removed == []


# Exact failing-run 34384146856 values; E-notation independently pins the
# positional Decimals in the production approval set.
CONFIRMED_REFRESH_TRANSITIONS = [
    (
        "deepseek/deepseek-v4-flash-0731 "
        "[siliconflow:siliconflow/fp8:deepseek-ai/DeepSeek-V4-Flash-0731]",
        "completion", "2.8E-7", "6.6E-7",
    ),
    (
        "deepseek/deepseek-v4-flash-0731 "
        "[siliconflow:siliconflow:deepseek-ai/DeepSeek-V4-Flash-0731]",
        "completion", "2.8E-7", "6.6E-7",
    ),
    (
        "deepseek/deepseek-v4-flash-vision-exp "
        "[siliconflow:siliconflow/fp8:deepseek-ai/DeepSeek-V4-Flash-Vision-Exp]",
        "prompt", "1.3E-7", "4.4E-7",
    ),
    (
        "deepseek/deepseek-v4-flash-vision-exp "
        "[siliconflow:siliconflow/fp8:deepseek-ai/DeepSeek-V4-Flash-Vision-Exp]",
        "completion", "2.8E-7", "1.32E-6",
    ),
    (
        "deepseek/deepseek-v4-flash-vision-exp "
        "[siliconflow:siliconflow:deepseek-ai/DeepSeek-V4-Flash-Vision-Exp]",
        "prompt", "1.3E-7", "4.4E-7",
    ),
    (
        "deepseek/deepseek-v4-flash-vision-exp "
        "[siliconflow:siliconflow:deepseek-ai/DeepSeek-V4-Flash-Vision-Exp]",
        "completion", "2.8E-7", "1.32E-6",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[novita:novita/fp8:z-ai/glm-5.3-flash]",
        "prompt", "7.5E-8", "1.5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[novita:novita/fp8:z-ai/glm-5.3-flash]",
        "completion", "2.5E-7", "5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[novita:novita:zai-org/glm-5.3-flash]",
        "prompt", "7.5E-8", "1.5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[novita:novita:zai-org/glm-5.3-flash]",
        "completion", "2.5E-7", "5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[novita:novita:zai-org/glm-5.3-flash] cached-input",
        "prompt", "1.5E-8", "3E-8",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[telnyx:telnyx:zai-org/GLM-5.3-Flash]",
        "prompt", "7.5E-8", "1.5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[telnyx:telnyx:zai-org/GLM-5.3-Flash]",
        "completion", "2.5E-7", "5E-7",
    ),
    (
        "z-ai/glm-5.3-flash "
        "[telnyx:telnyx:zai-org/GLM-5.3-Flash] cached-input",
        "prompt", "1.5E-8", "3E-8",
    ),
]


def _confirmed_refresh_prices(
    transitions: list[tuple[str, str, str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    before: dict[str, dict[str, str]] = {}
    after: dict[str, dict[str, str]] = {}
    for route, dimension, old, new in transitions:
        before.setdefault(route, {"prompt": "0", "completion": "0"})[dimension] = old
        after.setdefault(route, {"prompt": "0", "completion": "0"})[dimension] = new
    return before, after


SEPTEMBER_22_REFRESH_TRANSITIONS = [
    (
        "google/gemma-4-31b-it [siliconflow:siliconflow/fp8:google/gemma-4-31B-it]",
        "prompt", "1.3E-7", "7.5E-7",
    ),
    (
        "google/gemma-4-31b-it [siliconflow:siliconflow/fp8:google/gemma-4-31B-it]",
        "completion", "4E-7", "1E-6",
    ),
    (
        "google/gemma-4-31b-it [siliconflow:siliconflow:google/gemma-4-31B-it]",
        "prompt", "1.3E-7", "7.5E-7",
    ),
    (
        "google/gemma-4-31b-it [siliconflow:siliconflow:google/gemma-4-31B-it]",
        "completion", "4E-7", "1E-6",
    ),
    (
        "deepseek/deepseek-v4-flash-0731 "
        "[inceptron:inceptron:deepseek-ai/DeepSeek-V4-Flash-0731] cached-input",
        "prompt", "2E-8", "4E-8",
    ),
]

# Exact failing-run 36313186338 values (2026-09-27).
SEPTEMBER_27_WAFER_REFRESH_TRANSITIONS = [
    ("z-ai/glm-5.3-flash [wafer:wafer:GLM-5.3-Flash]", "prompt", "1E-7", "4.3E-7"),
    (
        "deepseek/deepseek-v4.1-flash [wafer:wafer:DeepSeek-V4.1-Flash] cached-input",
        "prompt", "1E-8", "5E-8",
    ),
    (
        "deepseek/deepseek-v4-pro [wafer:wafer:DeepSeek-V4-Pro] cached-input",
        "prompt", "3E-8", "2.4E-7",
    ),
    (
        "deepseek/deepseek-v4-pro [wafer:wafer:DeepSeek-V4-Pro] cached-input",
        "prompt", "3E-8", "1.7E-7",
    ),
]

# Exact held-run 44627483513 values (2026-11-19) for the routes verified
# against each provider's own price source. Routes printed twice with the
# same text (snapshot and manifest) appear once.
_ATLAS_SOL_CODEX = "openai/gpt-6-sol-codex [atlas-cloud:atlas-cloud:openai/gpt-6-sol-codex]"
_GMI_ARGON = "google/gemini-4-argon [gmi:gmi:google/gemini-4-argon]"
NOVEMBER_20_INCEPTRON_TRANSITIONS = [
    (
        "deepseek/deepseek-v4-flash-0731 [inceptron:inceptron:deepseek-ai/DeepSeek-V4-Flash-0731]",
        "prompt", "6.2E-8", "1.3E-7",
    ),
    ("z-ai/glm-5.3 [inceptron:inceptron:zai-org/GLM-5.3]", "prompt", "2.445E-7", "1.05E-6"),
]
NOVEMBER_19_REFRESH_TRANSITIONS = [
    (_ATLAS_SOL_CODEX, "prompt", "1E-7", "0.000002"),
    (_ATLAS_SOL_CODEX, "completion", "5E-7", "0.00001"),
    (_ATLAS_SOL_CODEX + " cached-input", "prompt", "1E-8", "2E-7"),
    (_GMI_ARGON, "prompt", "0.000002", "0.000004"),
    (_GMI_ARGON, "completion", "0.00001", "0.00002"),
    (_GMI_ARGON + " cached-input", "prompt", "1E-7", "2E-7"),
    *(
        (route, dimension, old, new)
        for route in (
            "google/gemma-3-12b-it [nextbit:nextbit:gemma3:12b-it]",
            "google/gemma-3-12b-it [nextbit-eu:nextbit-eu:google/gemma-3-12b-it]",
            "google/gemma-3-12b-it [nextbit-eu:nextbit-eu:gemma3:12b-it]",
        )
        for dimension, old, new in (
            ("prompt", "7.5E-8", "3.5E-7"),
            ("completion", "1.5E-7", "4.5E-7"),
        )
    ),
    (
        "deepseek/deepseek-v4.1-flash [wafer:wafer:DeepSeek-V4.1-Flash] cached-input",
        "prompt", "6E-9", "1.2E-8",
    ),
    (
        "deepseek/deepseek-v4.1-flash-1017 [wafer:wafer:DeepSeek-V4.1-Flash-1017] cached-input",
        "prompt", "3E-9", "8E-9",
    ),
]


@pytest.mark.parametrize(
    ("route", "dimension", "old", "new"),
    CONFIRMED_REFRESH_TRANSITIONS
    + SEPTEMBER_22_REFRESH_TRANSITIONS
    + SEPTEMBER_27_WAFER_REFRESH_TRANSITIONS
    + NOVEMBER_19_REFRESH_TRANSITIONS
    + NOVEMBER_20_INCEPTRON_TRANSITIONS,
)
def test_confirmed_refresh_transition_is_allowed(
    route: str, dimension: str, old: str, new: str,
) -> None:
    before, after = _confirmed_refresh_prices([(route, dimension, old, new)])

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == 1
    assert removed == []
    # Decimal equality confirms both positional literals equal the run's E-notation.
    assert (route, dimension, Decimal(old), Decimal(new)) in (
        check_price_spike.APPROVED_ENDPOINT_PRICE_TRANSITIONS
    )


def test_all_fourteen_confirmed_refresh_transitions_are_allowed() -> None:
    assert len(CONFIRMED_REFRESH_TRANSITIONS) == 14
    before, after = _confirmed_refresh_prices(CONFIRMED_REFRESH_TRANSITIONS)

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == len(before) == 9
    assert removed == []


def test_november_19_verified_transitions_pass_only_with_their_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, after = _confirmed_refresh_prices(NOVEMBER_19_REFRESH_TRANSITIONS)

    failures, changes, removed = check(before, after)

    assert failures == []
    assert len(changes) == len(before) == 9
    assert removed == []
    # Positive control: without the approvals the gate holds every route the
    # 2026-11-19 refresh held for these providers.
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.failing_routes(before, after) == set(before)


@pytest.mark.parametrize(
    ("route", "dimension", "old", "new"),
    CONFIRMED_REFRESH_TRANSITIONS
    + SEPTEMBER_22_REFRESH_TRANSITIONS
    + SEPTEMBER_27_WAFER_REFRESH_TRANSITIONS
    + NOVEMBER_19_REFRESH_TRANSITIONS
    + NOVEMBER_20_INCEPTRON_TRANSITIONS,
)
@pytest.mark.parametrize("mutation", ["remove-approval", "after-price", "endpoint-tag"])
def test_confirmed_refresh_mutations_still_block(
    monkeypatch: pytest.MonkeyPatch,
    route: str, dimension: str, old: str, new: str, mutation: str,
) -> None:
    if mutation == "remove-approval":
        approval = (route, dimension, Decimal(old), Decimal(new))
        monkeypatch.setattr(
            check_price_spike,
            "APPROVED_ENDPOINT_PRICE_TRANSITIONS",
            check_price_spike.APPROVED_ENDPOINT_PRICE_TRANSITIONS - {approval},
        )
    elif mutation == "after-price":
        # Increase only the final digit, keeping every mutation above the 2x gate.
        new = format(Decimal(new), "f")
        new = new[:-1] + str(int(new[-1]) + 1)
    else:
        # Change the tag in BOTH snapshots so this remains a continuing endpoint.
        model_provider, tag, upstream = route.split(":", 2)
        tag = tag.replace("/fp8", "/fp16") if "/fp8" in tag else f"{tag}/fp16"
        route = f"{model_provider}:{tag}:{upstream}"
    before, after = _confirmed_refresh_prices([(route, dimension, old, new)])

    failures, _changes, _removed = check(before, after)

    assert len(failures) == 1
    assert failures[0].startswith(f"{route} {dimension}:")


def test_fifteenth_unapproved_refresh_transition_still_blocks() -> None:
    route = "other/model [siliconflow:siliconflow/fp8:other/model]"
    before, after = _confirmed_refresh_prices([
        *CONFIRMED_REFRESH_TRANSITIONS,
        (route, "prompt", "1E-7", "2E-7"),
    ])

    failures, changes, removed = check(before, after)

    assert len(failures) == 1
    assert failures[0].startswith(f"{route} prompt:")
    assert len(changes) == 10
    assert removed == []


@pytest.mark.parametrize("native, old_output", [
    ("gemini-3.8-flash-tts", 9_000_000),
    ("gemini-3.8-flash-lite-tts", 6_000_000),
])
@pytest.mark.parametrize("change", [
    "approved", "early", "input_only", "output_only", "wrong_old", "wrong_new",
    "next_doubling", "chat", "provider", "tag", "upstream", "headline", "tier", "cached",
])
def test_dated_speech_approval_requires_exact_endpoint_and_complete_pairs(
    monkeypatch: pytest.MonkeyPatch, native: str, old_output: int, change: str,
) -> None:
    from datetime import UTC, datetime

    from trusted_router import pricing_approvals

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 12, 31, tzinfo=UTC) if change == "early" else cls(2027, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(pricing_approvals, "datetime", Clock)
    route = f"google/{native} [google-ai-studio:google-ai-studio:{native}]"
    old = {"prompt": Decimal(500_000), "completion": Decimal(old_output)}
    new = {key: value * 2 for key, value in old.items()}
    if change == "input_only":
        new["completion"] = old["completion"]
    elif change == "output_only":
        new["prompt"] = old["prompt"]
    elif change == "wrong_old":
        old["completion"] -= 1
    elif change == "wrong_new":
        new["completion"] += 1
    elif change == "next_doubling":
        old, new = new, {key: value * 2 for key, value in new.items()}
    elif change == "chat":
        route = route.replace("-tts", "")
    elif change == "provider":
        route = route.replace("[google-ai-studio:", "[google-vertex:")
    elif change == "tag":
        route = route.replace(":google-ai-studio:", ":other:")
    elif change == "upstream":
        route = route.replace(f":{native}]", ":other]")
    elif change == "headline":
        route = f"google/{native}"
    elif change == "tier":
        route += " tier[max=1000]"
    elif change == "cached":
        route += " cached-input"
    failures, changes, _ = check(
        {route: {key: str(value / 10**12) for key, value in old.items()}},
        {route: {key: str(value / 10**12) for key, value in new.items()}},
    )
    assert bool(failures) == (change != "approved")
    assert changes  # An approval never hides the repricing from the report.


def _regolo_apertus_manifest_rates(tmp_path: Path, rate: str) -> tuple[dict, dict]:
    """Committed vs refreshed Regolo manifests for the published EUR tariff."""
    from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m
    from scripts.pricing.providers import regolo

    committed = json.loads(regolo.MANIFEST_PATH.read_text(encoding="utf-8"))
    # Pin the pre-approval EUR 0.40 / 2.10 tariff (2026-11-02 publish, ECB
    # 1.1172) on the "before" side. The refresh publishes the approved price,
    # after which the committed manifest no longer shows the transition.
    (apertus,) = [row for row in committed["models"] if row["id"] == "swiss-ai/apertus-70b-instruct"]
    apertus["input_token_price_per_m"] = 446_880
    apertus["output_token_price_per_m"] = 2_346_120
    (row,) = regolo.normalize_catalog({"data": [{
        "model_group": "apertus-70b", "mode": "chat",
        # https://regolo.ai/pricing (2026-11-14): EUR 1.00 / EUR 3.40 per M.
        "input_cost_per_token": 0.000001, "output_cost_per_token": 0.0000034,
        "max_input_tokens": 30000, "max_output_tokens": 30000,
    }]}, Decimal(rate))
    refreshed = json.loads(json.dumps(committed))
    for manifest_row in refreshed["models"]:
        if manifest_row["id"] == "swiss-ai/apertus-70b-instruct":
            manifest_row["input_token_price_per_m"] = dollars_per_token_to_micro_per_m(
                Decimal(row["pricing"]["prompt"])
            )
            manifest_row["output_token_price_per_m"] = dollars_per_token_to_micro_per_m(
                Decimal(row["pricing"]["completion"])
            )
    out = []
    for name, raw in (("before", committed), ("after", refreshed)):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / "regolo.json").write_text(json.dumps(raw), encoding="utf-8")
        out.append(_load_provider_manifests(directory))
    return out[0], out[1]


def test_regolo_apertus_eur_tariff_approval_is_exact_to_the_ecb_fixing(tmp_path):
    route = "swiss-ai/apertus-70b-instruct [regolo:regolo:apertus-70b]"
    before, after = _regolo_apertus_manifest_rates(tmp_path / "fixing", "1.1052")
    # The pinned "before" side carries the old EUR 0.40 / 2.10 tariff.
    assert {axis: Decimal(value) for axis, value in before[route].items()} == {
        "prompt": Decimal("0.00000044688"), "completion": Decimal("0.00000234612"),
    }
    assert Decimal(after[route]["prompt"]) == Decimal("0.0000011052")
    assert check(before, after)[0] == []
    # A different ECB fixing, route or axis is not approved by this entry.
    _, other_rate = _regolo_apertus_manifest_rates(tmp_path / "other", "1.1053")
    assert check(before, other_rate)[0]
    other_route = route.replace("regolo:regolo:", "other:other:")
    assert check({other_route: before[route]}, {other_route: after[route]})[0]
    assert check(
        {route: {"prompt": "1", "completion": before[route]["prompt"]}},
        {route: {"prompt": "1", "completion": after[route]["prompt"]}},
    )[0]


# Routes held by the hourly refresh on 2026-11-16 (run 43862438969), with the
# exact old and live rates in USD/token.
_ZRO_ABYSS1 = "zro/abyss1 [zro:zro:abyss1]"
_IO_V41 = "deepseek/deepseek-v4.1-flash [io-net:io-net:{}]"
_NOVEMBER_16_TRANSITIONS = [
    (_ZRO_ABYSS1, "prompt", "0.00000015", "0.0000003"),
    (_ZRO_ABYSS1, "completion", "0.00000055", "0.0000011"),
    (_ZRO_ABYSS1 + " cached-input", "prompt", "0.000000005", "0.00000001"),
    *(
        (_IO_V41.format(native), dimension, old, new)
        for native in ("deepseek/deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash")
        for dimension, old, new in (
            ("prompt", "0.00000036", "0.0000008202"),
            ("completion", "0.00000144", "0.0000033534"),
        )
    ),
    (_IO_V41.format("deepseek-ai/DeepSeek-V4.1-Flash") + " cached-input",
     "prompt", "0.00000018", "0.0000004101"),
]


@pytest.mark.parametrize(("route", "dimension", "old", "new"), _NOVEMBER_16_TRANSITIONS)
def test_november_16_zro_and_io_net_approvals_are_exact(route, dimension, old, new):
    before = {route: {"prompt": "1", "completion": "1", dimension: old}}
    after = {route: {**before[route], dimension: new}}
    assert check(before, after)[0] == []
    # Another new value, another old value, a further doubling, the other
    # axis, or another route still holds.
    assert check(before, {route: {
        **after[route], dimension: str(Decimal(new) + Decimal("0.000000000001")),
    }})[0]
    assert check({route: {**before[route], dimension: str(Decimal(old) / 2)}}, after)[0]
    assert check(after, {route: {**after[route], dimension: str(Decimal(new) * 2)}})[0]
    other_dimension = "prompt" if dimension == "completion" else "completion"
    assert check(
        {route: {"prompt": "1", "completion": "1", other_dimension: old}},
        {route: {"prompt": "1", "completion": "1", other_dimension: new}},
    )[0]
    provider = check_price_spike.route_provider(route)
    for other in (
        route.replace(f"[{provider}:{provider}:", "[other:other:"),
        route.replace(f"[{provider}:{provider}:", f"[{provider}:{provider}/fp8:"),
        route.removesuffix(" cached-input") if route.endswith(" cached-input")
        else route + " cached-input",
        route.split(" [", 1)[0],
    ):
        assert check({other: before[route]}, {other: after[route]})[0]


def _manifest_rates(tmp_path: Path, slug: str, raw: dict) -> dict[str, dict[str, str]]:
    directory = tmp_path / slug
    directory.mkdir(parents=True)
    (directory / f"{slug}.json").write_text(json.dumps(raw), encoding="utf-8")
    return _load_provider_manifests(directory)


def test_zro_abyss1_live_table_refresh_passes_only_with_the_approval(tmp_path, monkeypatch):
    """The 2026-11-16 Zro page, through the adapter and manifest writer."""
    from scripts.pricing.providers import zro

    html = (ROOT / "tests/fixtures/pricing/zro_models_2026-11-16.html").read_text(encoding="utf-8")
    committed = json.loads(zro.MANIFEST_PATH.read_text(encoding="utf-8"))
    (abyss1,) = [row for row in committed["models"] if row["id"] == "zro/abyss1"]
    # Pin the launch tariff the refresh compared against.
    before_raw = {"provider": "zro", "models": [{
        **abyss1, "routable": True, "input_token_price_per_m": 150_000,
        "output_token_price_per_m": 550_000, "cached_input_token_price_per_m": 5_000,
    }]}
    manifest = tmp_path / "zro.json"
    manifest.write_text(json.dumps(before_raw), encoding="utf-8")
    monkeypatch.setattr(zro, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(zro, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(zro, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("ZRO_API_KEY", "test-only-key")
    native_ids = ("deepseek-v4.1-flash", "glm-5.3", "dolly2-security", "abyss1", "zro/abyss1")
    monkeypatch.setattr(zro, "fetch_json", lambda *_a, **_kw: {"data": [{"id": n} for n in native_ids]})
    monkeypatch.setattr(zro, "fetch_html", lambda *_a, **_kw: html)
    probed: list[str] = []
    monkeypatch.setattr(zro, "probe_openai_chat", lambda **kw: probed.append(kw["model"]) or True)

    zro.write_provider_manifest(zro.fetch())

    assert "abyss1" not in probed  # A published route is not canaried again.
    before = _manifest_rates(tmp_path / "before", "zro", before_raw)
    after = _manifest_rates(
        tmp_path / "after", "zro", json.loads(manifest.read_text(encoding="utf-8"))
    )
    assert {axis: Decimal(value) for axis, value in after[_ZRO_ABYSS1].items()} == {
        "prompt": Decimal("0.0000003"), "completion": Decimal("0.0000011"),
    }
    assert Decimal(after[_ZRO_ABYSS1 + " cached-input"]["prompt"]) == Decimal("0.00000001")
    assert check(before, after)[0] == []
    # Positive control: without the approval the gate holds what the refresh held.
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.failing_routes(before, after) == {
        _ZRO_ABYSS1, _ZRO_ABYSS1 + " cached-input",
    }


def test_io_net_v41_flash_live_rates_pass_snapshot_and_manifest_only_when_approved(
    tmp_path, monkeypatch,
):
    """IO's 2026-11-16 /models row, through the adapter, writer and both gates."""
    from dataclasses import replace

    from scripts.pricing import refresh
    from scripts.pricing.providers import _direct_openai, io_net
    from scripts.pricing.providers._direct_openai import DirectOpenAIProvider

    model_id, native = "deepseek/deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash"
    # GET https://api.intelligence.io.solutions/api/v1/models, 2026-11-16.
    live_row = {
        "id": native, "object": "model", "context_window": 1048320, "max_tokens": 131072,
        "input_token_price": 8.202e-07, "output_token_price": 3.3534e-06,
        "cache_read_token_price": 4.101e-07, "supports_tools": True,
    }
    committed = json.loads(io_net.MANIFEST_PATH.read_text(encoding="utf-8"))
    (row,) = [r for r in committed["models"] if r.get("upstream_id") == native]
    before_raw = {"provider": "io-net", "models": [{
        **row, "routable": True, "input_token_price_per_m": 360_000,
        "output_token_price_per_m": 1_440_000, "cached_input_token_price_per_m": 180_000,
    }]}
    manifest = tmp_path / "io-net.json"
    manifest.write_text(json.dumps(before_raw), encoding="utf-8")
    monkeypatch.setenv("IONET_API_KEY", "test-only-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: {"data": [live_row]})

    def no_paid_canary(**_kwargs):
        raise AssertionError("a published route is not canaried again")

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", no_paid_canary)
    catalog = DirectOpenAIProvider(replace(io_net.CATALOG.spec, expected_models=()), manifest_path=manifest)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)

    block = refresh._price_to_pricing_block(result.prices[model_id])
    paths = []
    for side, prompt, completion in (
        ("before", "0.00000036", "0.00000144"),
        ("after", block["prompt"], block["completion"]),
    ):
        snapshot = _write(tmp_path, side + ".json", _make_endpoint_snapshot(
            ("1", "1"), [("io-net", model_id, prompt, completion)], model_id=model_id,
        ))
        raw = before_raw if side == "before" else json.loads(manifest.read_text(encoding="utf-8"))
        directory = tmp_path / side
        directory.mkdir()
        (directory / "io-net.json").write_text(json.dumps(raw), encoding="utf-8")
        paths.append((snapshot, directory))
    args = (paths[0][0], paths[1][0], paths[0][1], paths[1][1])
    assert (Decimal(block["prompt"]), Decimal(block["completion"])) == (
        Decimal("0.0000008202"), Decimal("0.0000033534"),
    )
    assert check_price_spike.spiking_providers(*args) == {}
    # Positive control: without the approval the gate holds the same three
    # routes the 2026-11-16 refresh held.
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset())
    assert check_price_spike.spiking_providers(*args) == {
        "io-net": [
            f"{model_id} [io-net:io-net:{model_id}]",
            f"{model_id} [io-net:io-net:{native}]",
            f"{model_id} [io-net:io-net:{native}] cached-input",
        ],
    }


# Large price drops: the signature of a misread unit (a per-1K or per-token
# price read as per-1M), which would otherwise publish and under-bill silently.
DROP_ROUTE = "acme/model [acme:acme:model]"


def _drop(prompt, completion, *, before=("0.000001", "0.000002"), route=DROP_ROUTE, **kwargs):
    return check(
        {route: {"prompt": before[0], "completion": before[1]}},
        {route: {"prompt": prompt, "completion": completion}},
        **kwargs,
    )[0]


@pytest.mark.parametrize(("prompt", "completion", "failure"), [
    # Per 1K read as per 1M: 1,000x too low.
    ("0.000000001", "0.000002", f"{DROP_ROUTE} prompt: 0.000001 → 1E-9 (÷1000.00 ≥ ÷100.0)"),
    ("0.000001", "0.000000002", f"{DROP_ROUTE} completion: 0.000002 → 2E-9 (÷1000.00 ≥ ÷100.0)"),
    # Per token read as per 1M: 1,000,000x too low.
    ("1E-12", "0.000002", f"{DROP_ROUTE} prompt: 0.000001 → 1E-12 (÷1000000.00 ≥ ÷100.0)"),
    # One dimension falling to zero.
    ("0", "0.000002", f"{DROP_ROUTE} prompt: 0.000001 → 0 (to 0 ≥ ÷100.0)"),
])
def test_a_large_drop_fails_with_its_exact_values(prompt, completion, failure):
    assert _drop(prompt, completion) == [failure]


def test_the_drop_boundary_is_inclusive_at_100x():
    assert _drop("0.00000001", "0.000002") == [
        f"{DROP_ROUTE} prompt: 0.000001 → 1E-8 (÷100.00 ≥ ÷100.0)"
    ]
    assert _drop("0.0000000100001", "0.000002") == []


@pytest.mark.parametrize(("before", "after"), [
    (("0.000001", "0.000002"), ("0.0000005", "0.000001")),  # a 50% cut
    (("0.00000003", "0.000002"), ("0.000000002", "0.000002")),  # ÷15, the largest real cut 2026-08-21..11-19
    (("0.000001", "0.000002"), ("0.000000011", "0.000002")),  # ÷90.9
])
def test_ordinary_and_historical_cuts_pass(before, after):
    assert _drop(*after, before=before) == []


def test_both_prices_zeroed_reports_once_with_the_dedicated_message():
    assert _drop("0", "0") == [
        f"{DROP_ROUTE}: both prompt and completion went to 0 (was prompt=0.000001, completion=0.000002)"
    ]


def test_a_route_that_was_already_free_is_not_a_drop():
    assert _drop("0", "0.000002", before=("0", "0.000002")) == []


def test_an_exact_approval_clears_a_drop_and_nothing_nearby(monkeypatch):
    approval = (DROP_ROUTE, "prompt", Decimal("0.000001"), Decimal("1E-9"))
    monkeypatch.setattr(check_price_spike, "APPROVED_ENDPOINT_PRICE_TRANSITIONS", frozenset({approval}))
    assert _drop("0.000000001", "0.000002") == []
    # A different new value, old value, dimension or route still fails.
    assert _drop("0.000000002", "0.000002")
    assert _drop("0.000000001", "0.000002", before=("0.0000011", "0.000002"))
    assert _drop("0.000001", "0.000000002", before=("0.000001", "0.000002"))
    assert _drop("0.000000001", "0.000002", route="acme/model [acme:acme:other]")


def test_a_custom_drop_ratio_is_honored():
    assert _drop("0.0000001", "0.000002", drop_ratio=10.0) == [
        f"{DROP_ROUTE} prompt: 0.000001 → 1E-7 (÷10.00 ≥ ÷10.0)"
    ]
    assert _drop("0.0000001", "0.000002") == []


def test_drops_hold_the_provider_through_spiking_providers(tmp_path):
    before = _write(tmp_path, "before.json", _make_endpoint_snapshot(
        ("1", "1"), [("acme", "acme/model", "0.000001", "0.000002"),
                     ("other", "acme/model", "0.000001", "0.000002")],
    ))
    after = _write(tmp_path, "after.json", _make_endpoint_snapshot(
        ("1", "1"), [("acme", "acme/model", "0.000000001", "0.000002"),
                     ("other", "acme/model", "0.0000009", "0.000002")],
    ))
    held = check_price_spike.spiking_providers(before, after)
    assert list(held) == ["acme"]
    assert [route.reasons for route in held["acme"]] == [("prompt: 0.000001 → 1E-9 (÷1000.00 ≥ ÷100.0)",)]


def test_the_cli_fails_a_large_drop_and_rejects_a_bad_drop_ratio(tmp_path, capsys):
    before = _write(tmp_path, "before.json", _make_endpoint_snapshot(
        ("1", "1"), [("acme", "acme/model", "0.000001", "0.000002")],
    ))
    after = _write(tmp_path, "after.json", _make_endpoint_snapshot(
        ("1", "1"), [("acme", "acme/model", "0.000000001", "0.000002")],
    ))
    assert check_price_spike.main([str(before), str(after)]) == 1
    assert "÷1000.00 ≥ ÷100.0" in capsys.readouterr().err
    assert check_price_spike.main([str(before), str(after), "--drop-ratio", "1"]) == 2
    for ratio in ("inf", "1e309", "nan"):
        assert check_price_spike.main([str(before), str(after), "--drop-ratio", ratio]) == 2
