from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import fireworks as fireworks_parser
from scripts.pricing.providers import fireworks


def _price() -> ModelPrice:
    return ModelPrice(
        prompt_micro_per_m=1_000_000,
        completion_micro_per_m=2_000_000,
        prompt_cached_micro_per_m=100_000,
    )


def test_fireworks_fetch_intersects_prices_with_operator_catalog(
    monkeypatch,
) -> None:  # noqa: ANN001
    priced_ids = {*fireworks.EXPECTED_MODELS, "moonshotai/kimi-k2.7-code"}
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    monkeypatch.setattr(
        fireworks,
        "fetch_provider",
        lambda **_kwargs: ProviderPricingResult(
            slug="fireworks",
            prices={model_id: _price() for model_id in priced_ids},
            source="deterministic",
        ),
    )
    live_rows = [
        {"id": fireworks.UPSTREAM_ID_MAP[model_id]}
        for model_id in fireworks.EXPECTED_MODELS
        if model_id not in fireworks.VERIFIED_PRICED_LAUNCH_MODELS
    ]
    monkeypatch.setattr(
        fireworks,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": live_rows},
    )

    result = fireworks.fetch()

    assert set(result.prices) == set(fireworks.EXPECTED_MODELS)
    assert any("kimi-k2.7-code" in note for note in result.notes)


def test_fireworks_dated_flash_uses_live_native_id_and_family_price(
    monkeypatch,
) -> None:  # noqa: ANN001
    dated_model = "deepseek/deepseek-v4-flash-0731"
    native_id = "accounts/fireworks/models/deepseek-v4-flash-0731"
    captured: dict[str, object] = {}
    priced_ids = {*fireworks.EXPECTED_MODELS, dated_model}
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    monkeypatch.setattr(fireworks, "UPSTREAM_ID_MAP", dict(fireworks.UPSTREAM_ID_MAP))

    def fake_fetch_provider(**kwargs: object) -> ProviderPricingResult:
        captured.update(kwargs)
        return ProviderPricingResult(
            slug="fireworks",
            prices={model_id: _price() for model_id in priced_ids},
            source="deterministic",
        )

    monkeypatch.setattr(fireworks, "fetch_provider", fake_fetch_provider)
    live_rows = [
        {"id": fireworks.UPSTREAM_ID_MAP[model_id]}
        for model_id in fireworks.EXPECTED_MODELS
        if model_id not in fireworks.VERIFIED_PRICED_LAUNCH_MODELS
    ]
    live_rows.append({"id": native_id})
    monkeypatch.setattr(
        fireworks,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": live_rows},
    )

    result = fireworks.fetch()

    aliases = captured["required_model_price_aliases"]
    assert isinstance(aliases, dict)
    assert aliases[dated_model] == "deepseek/deepseek-v4-flash"
    required_models = captured["required_models"]
    assert isinstance(required_models, frozenset)
    assert dated_model in required_models
    assert fireworks.UPSTREAM_ID_MAP[dated_model] == native_id
    assert dated_model in result.prices


def test_fireworks_parser_reads_kimi_k3_standard_pricing() -> None:
    parsed = fireworks_parser.parse(
        "| [Kimi K3](https://app.fireworks.ai/models/fireworks/kimi-k3) "
        "| $3.00 / $0.30 / $15.00 | $3.75 / $0.375 / $18.75 |"
    )

    assert parsed["moonshotai/kimi-k3"] == {
        "prompt_micro_per_m": 3_000_000,
        "prompt_cached_micro_per_m": 300_000,
        "completion_micro_per_m": 15_000_000,
    }


def test_fireworks_fetch_keeps_verified_launch_model_while_catalog_lags(
    monkeypatch,
) -> None:  # noqa: ANN001
    priced_ids = {*fireworks.EXPECTED_MODELS, "moonshotai/kimi-k3"}
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    monkeypatch.setattr(
        fireworks,
        "fetch_provider",
        lambda **_kwargs: ProviderPricingResult(
            slug="fireworks",
            prices={model_id: _price() for model_id in priced_ids},
            source="deterministic",
        ),
    )
    live_rows = [
        {"id": fireworks.UPSTREAM_ID_MAP[model_id]} for model_id in fireworks.EXPECTED_MODELS
    ]
    monkeypatch.setattr(
        fireworks,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": live_rows},
    )

    result = fireworks.fetch()

    assert "moonshotai/kimi-k3" in result.prices
    assert "moonshotai/kimi-k3" in fireworks._DISCOVERED_LIVE_MODEL_IDS


def test_fireworks_manifest_prunes_retired_models_but_keeps_router(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "fireworks.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "fireworks",
                "models": [
                    {
                        "id": "moonshotai/kimi-k2.6",
                        "upstream_id": "accounts/fireworks/models/kimi-k2p6",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "moonshotai/kimi-k2.5",
                        "upstream_id": "accounts/fireworks/models/kimi-k2p5",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "z-ai/glm-5.2-fast",
                        "upstream_id": "accounts/fireworks/routers/glm-5p2-fast",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "moonshotai/kimi-k3",
                        "upstream_id": "accounts/fireworks/models/kimi-k3",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fireworks, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        fireworks,
        "_DISCOVERED_LIVE_MODEL_IDS",
        {"moonshotai/kimi-k2.6", "moonshotai/kimi-k3"},
    )
    result = ProviderPricingResult(
        slug="fireworks",
        prices={
            "moonshotai/kimi-k2.6": _price(),
            "moonshotai/kimi-k3": ModelPrice(
                prompt_micro_per_m=3_000_000,
                completion_micro_per_m=15_000_000,
                prompt_cached_micro_per_m=300_000,
            ),
        },
        source="deterministic",
    )

    notes = fireworks.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert set(by_id) == {
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2-fast",
    }
    assert by_id["moonshotai/kimi-k2.6"]["input_token_price_per_m"] == 1_000_000
    assert by_id["moonshotai/kimi-k3"]["input_token_price_per_m"] == 3_000_000
    assert by_id["moonshotai/kimi-k3"]["cached_input_token_price_per_m"] == 300_000
    assert notes == [
        "fireworks: refreshed provider_models/fireworks.json (2 priced rows, removed 1 unavailable)"
    ]
