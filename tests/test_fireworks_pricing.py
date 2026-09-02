from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import fireworks as fireworks_parser
from scripts.pricing.providers import fireworks
from trusted_router.catalog import MODEL_ENDPOINTS, effective_endpoint
from trusted_router.provider_lifecycle import (
    FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT,
    ProviderPrice,
    provider_price_microdollars,
    provider_pricing_schedule,
)


def _price() -> ModelPrice:
    return ModelPrice(
        prompt_micro_per_m=1_000_000,
        completion_micro_per_m=2_000_000,
        prompt_cached_micro_per_m=100_000,
    )


def test_fireworks_dsv4_flash_announced_cutover_is_exact() -> None:
    model_id = "deepseek/deepseek-v4-flash-0731"

    assert provider_price_microdollars(
        "fireworks",
        model_id,
        at=FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT - timedelta(seconds=1),
    ) == ProviderPrice(140_000, 280_000, 28_000)
    assert provider_price_microdollars(
        "fireworks",
        model_id,
        at=FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT,
    ) == ProviderPrice(220_000, 660_000, 7_000)


def test_fireworks_dsv4_flash_cutover_applies_markup_and_cache_floor() -> None:
    endpoint = MODEL_ENDPOINTS[
        "deepseek/deepseek-v4-flash-0731@fireworks/prepaid"
    ]

    before = effective_endpoint(
        endpoint,
        at=datetime(2026, 8, 22, 11, 59, 59, tzinfo=UTC),
    )
    after = effective_endpoint(
        endpoint,
        at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )

    assert before.prompt_price_microdollars_per_million_tokens == 147_700
    assert before.completion_price_microdollars_per_million_tokens == 295_400
    assert before.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 29_540
    assert after.prompt_price_microdollars_per_million_tokens == 232_100
    assert after.completion_price_microdollars_per_million_tokens == 696_300
    assert after.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 10_000


def test_fireworks_dsv4_flash_schedule_is_public_and_authorization_locked() -> None:
    assert provider_pricing_schedule(
        "fireworks",
        "deepseek/deepseek-v4-flash-0731",
        at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    ) == {
        "kind": "fixed_cutover",
        "timezone": "UTC",
        "effective_at": "2026-08-22T12:00:00Z",
        "current_period": "new",
        "rate_locked_at": "authorization",
    }


def test_fireworks_fetch_intersects_prices_with_operator_catalog(
    monkeypatch,
) -> None:  # noqa: ANN001
    docs_only_model = "moonshotai/kimi-k2.5"
    priced_ids = {*fireworks.EXPECTED_MODELS, docs_only_model}
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
    assert any(docs_only_model in note for note in result.notes)
    assert fireworks._DISCOVERED_MANIFEST_ROWS["qwen/qwen3.8-max"][
        "input_modalities"
    ] == ["text", "image"]
    assert (
        fireworks._DISCOVERED_MANIFEST_ROWS["minimax/minimax-m3"]["display_name"]
        == "MiniMax M3 on Fireworks"
    )


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


def test_fireworks_parser_distinguishes_fast_routes_and_replacements() -> None:
    parsed = fireworks_parser.parse(
        """
        | Kimi K2.6 | $0.95 / $0.16 / $4.00 |
        | Kimi K2.6 Fast | $2.00 / $0.30 / $8.00 |
        | Kimi K2.7 Code | $0.95 / $0.19 / $4.00 |
        | Kimi K2.7 Code Fast | $1.90 / $0.38 / $8.00 |
        | MiniMax M3 | $0.30 / $0.06 / $1.20 |
        | Muse Glimmer 30B | $0.35 / $0.04 / $1.50 |
        | NVIDIA Nemotron 3.5 Lightning 30B A3B | $0.05 / $0.01 / $0.20 |
        | NVIDIA Nemotron 3 Ultra (Preview) | $0.60 / $0.12 / $2.40 |
        | Qwen 3.8 Max | $2.00 / $0.25 / $6.00 |
        """
    )

    assert parsed["moonshotai/kimi-k2.6"]["prompt_micro_per_m"] == 950_000
    assert parsed["moonshotai/kimi-k2.6-fast"]["prompt_micro_per_m"] == 2_000_000
    assert parsed["moonshotai/kimi-k2.7-code"]["completion_micro_per_m"] == 4_000_000
    assert parsed["moonshotai/kimi-k2.7-code-fast"]["completion_micro_per_m"] == 8_000_000
    assert parsed["minimax/minimax-m3"]["completion_micro_per_m"] == 1_200_000
    assert parsed["meta-models/muse-glimmer-30b"]["completion_micro_per_m"] == 1_500_000
    assert parsed["nvidia/nemotron-3.5-lightning"]["prompt_micro_per_m"] == 50_000
    assert (
        parsed["nvidia/nemotron-3-ultra-550b-a55b"]["completion_micro_per_m"]
        == 2_400_000
    )
    assert parsed["qwen/qwen3.8-max"]["completion_micro_per_m"] == 6_000_000


def test_fireworks_parser_auto_discovers_linked_standard_family_rows() -> None:
    parsed = fireworks_parser.parse(
        """
        | [GLM 5.3](https://app.fireworks.ai/models/fireworks/glm-5p3) |
          $0.40 / $0.04 / $1.20 |
        | [Qwen 3.9 Max](https://app.fireworks.ai/models/fireworks/qwen3p9-max) |
          $0.50 / $0.05 / $1.50 |
        | [Kimi K4 Fast](https://app.fireworks.ai/models/fireworks/kimi-k4) |
          $2.00 / $0.20 / $8.00 |
        """
    )

    assert parsed["z-ai/glm-5.3"]["completion_micro_per_m"] == 1_200_000
    assert parsed["qwen/qwen3.9-max"]["prompt_micro_per_m"] == 500_000
    assert "moonshotai/kimi-k4" not in parsed


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

    assert "moonshotai/kimi-k3" in result.prices
    assert "moonshotai/kimi-k3" in fireworks._DISCOVERED_MANIFEST_ROWS


def test_fireworks_fetch_preserves_live_unpriced_model_as_dark_metadata(
    monkeypatch,
) -> None:  # noqa: ANN001
    native_id = "accounts/fireworks/models/glm-5p3-flash"
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    monkeypatch.setattr(fireworks, "UPSTREAM_ID_MAP", dict(fireworks.UPSTREAM_ID_MAP))
    monkeypatch.setattr(
        fireworks,
        "fetch_provider",
        lambda **_kwargs: ProviderPricingResult(
            slug="fireworks",
            prices={model_id: _price() for model_id in fireworks.EXPECTED_MODELS},
            source="deterministic",
        ),
    )
    live_rows = [
        {"id": fireworks.UPSTREAM_ID_MAP[model_id]}
        for model_id in fireworks.EXPECTED_MODELS
        if model_id not in fireworks.VERIFIED_PRICED_LAUNCH_MODELS
    ]
    live_rows.append(
        {
            "id": native_id,
            "name": "GLM 5.3 Flash",
            "context_length": 1_048_576,
        }
    )
    live_rows.extend(
        [
            {
                "id": "accounts/fireworks/models/qwen3-embedding-8b",
                "kind": "EMBEDDING_MODEL",
                "supports_chat": True,
            },
            {
                "id": "accounts/fireworks/models/qwen3p8-2p4t-a95b",
                "kind": "HF_BASE_MODEL",
                "supports_chat": True,
            },
        ]
    )
    monkeypatch.setattr(
        fireworks,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": live_rows},
    )

    result = fireworks.fetch()

    assert "z-ai/glm-5.3-flash" not in result.prices
    assert fireworks._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.3-flash"] == {
        "id": "z-ai/glm-5.3-flash",
        "upstream_id": native_id,
        "display_name": "GLM 5.3 Flash on Fireworks",
        "endpoints": ["chat/completions"],
        "context_length": 1_048_576,
    }
    assert fireworks.UPSTREAM_ID_MAP["z-ai/glm-5.3-flash"] == native_id
    assert "qwen/qwen3-embedding-8b" not in fireworks._DISCOVERED_MANIFEST_ROWS
    assert "qwen/qwen3p8-2p4t-a95b" not in fireworks._DISCOVERED_MANIFEST_ROWS


def test_fireworks_manifest_keeps_unpriced_model_dark(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "fireworks.json"
    manifest_path.write_text(
        json.dumps({"provider": "fireworks", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fireworks, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        fireworks,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "z-ai/glm-5.3-flash": {
                "id": "z-ai/glm-5.3-flash",
                "upstream_id": "accounts/fireworks/models/glm-5p3-flash",
                "display_name": "GLM 5.3 Flash on Fireworks",
                "endpoints": ["chat/completions"],
                "context_length": 1_048_576,
            }
        },
    )
    result = ProviderPricingResult(
        slug="fireworks",
        prices={},
        source="api",
        fetched_url=fireworks.URL,
    )

    fireworks.write_provider_manifest(result)

    row = json.loads(manifest_path.read_text(encoding="utf-8"))["models"][0]
    assert row["id"] == "z-ai/glm-5.3-flash"
    assert row["routable"] is False
    assert row["routable_reason"] == "awaiting-price"
    assert row["unresolved_since"]
    assert "input_token_price_per_m" not in row
    assert "output_token_price_per_m" not in row


def test_fireworks_manifest_appends_discovered_models_and_tombstones_delisted(
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
                        "missing_since": "2026-08-24",
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
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "moonshotai/kimi-k2.6": {
                "id": "moonshotai/kimi-k2.6",
                "upstream_id": "accounts/fireworks/models/kimi-k2p6",
            },
            "z-ai/glm-5.2-fast": {
                "id": "z-ai/glm-5.2-fast",
                "upstream_id": "accounts/fireworks/routers/glm-5p2-fast",
            },
            "moonshotai/kimi-k3": {
                "id": "moonshotai/kimi-k3",
                "upstream_id": "accounts/fireworks/models/kimi-k3",
            },
            "minimax/minimax-m3": {
                "id": "minimax/minimax-m3",
                "upstream_id": "accounts/fireworks/models/minimax-m3",
                "display_name": "MiniMax M3 on Fireworks",
                "context_length": 512_000,
            },
        },
    )
    result = ProviderPricingResult(
        slug="fireworks",
        prices={
            "moonshotai/kimi-k2.6": _price(),
            "z-ai/glm-5.2-fast": _price(),
            "moonshotai/kimi-k3": ModelPrice(
                prompt_micro_per_m=3_000_000,
                completion_micro_per_m=15_000_000,
                prompt_cached_micro_per_m=300_000,
            ),
            "minimax/minimax-m3": _price(),
        },
        source="api",
        fetched_url=fireworks.URL,
    )

    notes = fireworks.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert set(by_id) == {
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k2.5",
        "moonshotai/kimi-k3",
        "minimax/minimax-m3",
        "z-ai/glm-5.2-fast",
    }
    assert by_id["moonshotai/kimi-k2.6"]["input_token_price_per_m"] == 1_000_000
    assert by_id["moonshotai/kimi-k3"]["input_token_price_per_m"] == 3_000_000
    assert by_id["moonshotai/kimi-k3"]["cached_input_token_price_per_m"] == 300_000
    assert by_id["minimax/minimax-m3"]["context_length"] == 512_000
    assert by_id["minimax/minimax-m3"]["display_name"] == "MiniMax M3 on Fireworks"
    assert by_id["moonshotai/kimi-k2.5"]["routable"] is False
    assert by_id["moonshotai/kimi-k2.5"]["routable_reason"] == "delisted-upstream"
    assert notes == [
        "fireworks: refreshed provider_models/fireworks.json "
        "(4 priced rows, appended 1, tombstoned 1 unavailable)"
    ]
