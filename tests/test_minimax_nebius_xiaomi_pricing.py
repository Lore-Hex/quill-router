from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import minimax as minimax_parser
from scripts.pricing.parsers import xiaomi as xiaomi_parser
from scripts.pricing.providers import minimax, nebius
from scripts.pricing.providers import xiaomi as xiaomi_provider


def test_minimax_parser_reads_official_token_plan_tiers() -> None:
    html = """
    MiniMax-M3 Context ≤ 512K Permanent 50% off $0.6$0.3 $2.4$1.2 $0.12$0.06
    MiniMax-M3 Context 512K ~ 1M Permanent 50% off $1.2$0.6 $4.8$2.4 $0.24$0.12
    MiniMax-M2.7 $0.3/ M tokens $1.2/ M tokens $0.06/ M tokens $0.375/ M tokens
    MiniMax-M2.7-highspeed $0.6/ M tokens $2.4/ M tokens $0.06/ M tokens $0.375/ M tokens
    """

    prices = minimax_parser.parse(html)

    assert prices["minimax/minimax-m3"] == {
        "tiers": [
            {
                "max_prompt_tokens": 512_000,
                "prompt_micro_per_m": 300_000,
                "completion_micro_per_m": 1_200_000,
                "prompt_cached_micro_per_m": 60_000,
            },
            {
                "max_prompt_tokens": None,
                "prompt_micro_per_m": 600_000,
                "completion_micro_per_m": 2_400_000,
                "prompt_cached_micro_per_m": 120_000,
            },
        ]
    }
    assert prices["minimax/minimax-m2.7-highspeed"] == {
        "prompt_micro_per_m": 600_000,
        "completion_micro_per_m": 2_400_000,
        "prompt_cached_micro_per_m": 60_000,
    }


def test_minimax_parser_normalizes_future_flat_price_rows() -> None:
    prices = minimax_parser.parse("MiniMax-M4 $0.4/ M tokens $1.6/ M tokens $0.08/ M tokens")

    assert prices["minimax/minimax-m4"] == {
        "prompt_micro_per_m": 400_000,
        "completion_micro_per_m": 1_600_000,
        "prompt_cached_micro_per_m": 80_000,
    }


def test_minimax_live_discovery_requires_new_model_before_manifest_write(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest = tmp_path / "minimax.json"
    manifest.write_text(
        json.dumps({"models": [{"id": "minimax/minimax-m3", "upstream_id": "MiniMax-M3"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(minimax, "MANIFEST_PATH", manifest)
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setattr(
        minimax,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"id": "MiniMax-M3", "status": 1},
                {"id": "MiniMax-M4", "status": 1},
            ]
        },
    )
    captured: dict[str, object] = {}

    def fake_fetch_provider(**kwargs: object) -> ProviderPricingResult:
        captured.update(kwargs)
        return ProviderPricingResult(
            slug="minimax",
            prices={
                "minimax/minimax-m3": ModelPrice(300_000, 1_200_000),
                "minimax/minimax-m4": ModelPrice(400_000, 1_600_000),
            },
            source="deterministic",
        )

    monkeypatch.setattr(minimax, "fetch_provider", fake_fetch_provider)

    result = minimax.fetch()

    assert captured["required_models"] == frozenset({"minimax/minimax-m4"})
    assert set(result.prices) == {"minimax/minimax-m3", "minimax/minimax-m4"}


def test_xiaomi_parser_reads_official_mimo_payg_prices() -> None:
    html = """
    #### MiMo-V2.5-Pro
    Input (cache hit)$0.0036 / MTok Input (cache miss)$0.435 / MTok Output$0.87 / MTok
    #### MiMo-V2.5-Pro-UltraSpeed
    Input (cache hit)$0.0108 / MTok Input (cache miss)$1.305 / MTok Output$2.61 / MTok
    #### MiMo-V2.5
    Input (cache hit)$0.0028 / MTok Input (cache miss)$0.14 / MTok Output$0.28 / MTok
    """

    prices = xiaomi_parser.parse(html)

    assert prices["xiaomi/mimo-v2.5-pro"] == {
        "prompt_micro_per_m": 435_000,
        "completion_micro_per_m": 870_000,
        "prompt_cached_micro_per_m": 3_600,
    }
    assert prices["xiaomi/mimo-v2.5-pro-ultraspeed"] == {
        "prompt_micro_per_m": 1_305_000,
        "completion_micro_per_m": 2_610_000,
        "prompt_cached_micro_per_m": 10_800,
    }
    assert prices["xiaomi/mimo-v2.5"] == {
        "prompt_micro_per_m": 140_000,
        "completion_micro_per_m": 280_000,
        "prompt_cached_micro_per_m": 2_800,
    }


def test_xiaomi_parser_reads_current_overseas_markdown_table() -> None:
    html = """
    ### Domestic Pricing of the Model
    | `mimo-v2.5-pro` | ¥0.025 | ¥3.00 | ¥6.00 |
    ### Overseas Pricing of the Model
    |  | **Input (Cache Hit)** | **Input (Cache Miss)** | **Output** |
    | --- | --- | --- | --- |
    | `mimo-v2.5-pro` | $0.0036 | $0.435 | $0.87 |
    | `mimo-v2.5` | $0.0028 | $0.14 | $0.28 |
    ### Pricing for Web Search Plugins
    """

    assert xiaomi_parser.parse(html) == {
        "xiaomi/mimo-v2.5-pro": {
            "prompt_micro_per_m": 435_000,
            "completion_micro_per_m": 870_000,
            "prompt_cached_micro_per_m": 3_600,
        },
        "xiaomi/mimo-v2.5": {
            "prompt_micro_per_m": 140_000,
            "completion_micro_per_m": 280_000,
            "prompt_cached_micro_per_m": 2_800,
        },
    }


def test_xiaomi_parser_reads_flattened_overseas_table_without_using_domestic_prices() -> None:
    html = """
    ### Domestic Pricing of the Model
    MiMo-V2.5 Series
    Input (Cache Hit) Input (Cache Miss) Output
    `mimo-v2.5-pro` ¥0.025 ¥3.00 ¥6.00
    `mimo-v2.5` ¥0.02 ¥1.00 ¥2.00
    ### Overseas Pricing of the Model
    MiMo-V2.5 Series
    Input (Cache Hit) Input (Cache Miss) Output
    `mimo-v2.5-pro` $0.0036 $0.435 $0.87
    `mimo-v2.5` $0.0028 $0.14 $0.28
    ### Pricing for Web Search Plugins
    """

    assert xiaomi_parser.parse(html) == {
        "xiaomi/mimo-v2.5-pro": {
            "prompt_micro_per_m": 435_000,
            "completion_micro_per_m": 870_000,
            "prompt_cached_micro_per_m": 3_600,
        },
        "xiaomi/mimo-v2.5": {
            "prompt_micro_per_m": 140_000,
            "completion_micro_per_m": 280_000,
            "prompt_cached_micro_per_m": 2_800,
        },
    }


def test_xiaomi_parser_rejects_domestic_only_pricing_as_usd() -> None:
    html = """
    ### Domestic Pricing of the Model
    `mimo-v2.5-pro` ¥0.025 ¥3.00 ¥6.00
    `mimo-v2.5` ¥0.02 ¥1.00 ¥2.00
    """

    assert xiaomi_parser.parse(html) == {}


def test_xiaomi_provider_uses_current_official_payg_page() -> None:
    assert xiaomi_provider.PUBLIC_PRICING_URL == (
        "https://mimo.mi.com/docs/en-US/price/pay-as-you-go"
    )
    assert xiaomi_provider.URL == xiaomi_provider.PUBLIC_PRICING_URL


def test_xiaomi_parser_normalizes_future_payg_model_rows() -> None:
    html = """
    ### Overseas Pricing of the Model
    |  | **Input (Cache Hit)** | **Input (Cache Miss)** | **Output** |
    | --- | --- | --- | --- |
    | `mimo-v2.6-pro` | $0.01 | $0.50 | $1.00 |
    ### Pricing for Web Search Plugins
    """

    assert xiaomi_parser.parse(html) == {
        "xiaomi/mimo-v2.6-pro": {
            "prompt_micro_per_m": 500_000,
            "completion_micro_per_m": 1_000_000,
            "prompt_cached_micro_per_m": 10_000,
        }
    }


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_nebius_fetch_uses_verbose_pricing_and_skips_embeddings(
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    payload = {
        "data": [
            {
                "id": "openai/gpt-oss-120b",
                "name": "gpt-oss-120b",
                "created": 1,
                "context_length": 131072,
                "architecture": {"modality": "text->text"},
                "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            },
            {
                "id": "deepseek-ai/DeepSeek-V4-Pro",
                "name": "DeepSeek-V4-Pro",
                "created": 1,
                "context_length": 1048576,
                "architecture": {"modality": "text->text"},
                "pricing": {"prompt": "0.00000175", "completion": "0.0000035"},
            },
            {
                "id": "zai-org/GLM-5.1",
                "name": "GLM-5.1",
                "created": 1,
                "context_length": 202752,
                "architecture": {"modality": "text->text"},
                "pricing": {"prompt": "0.0000014", "completion": "0.0000044"},
            },
            {
                "id": "nvidia/Nemotron-3-Ultra-550b-a55b",
                "name": "NVIDIA Nemotron 3 Ultra 550B A55B",
                "created": 1,
                "context_length": 262144,
                "architecture": {"modality": "text->text"},
                "pricing": {"prompt": "0.0000006", "completion": "0.0000024"},
            },
            {
                "id": "Qwen/Qwen3-Embedding-8B",
                "name": "Qwen3-Embedding-8B",
                "created": 1,
                "context_length": 40960,
                "architecture": {"modality": "text->embedding"},
                "pricing": {"prompt": "0.00000001", "completion": "0"},
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> _FakeResponse:  # noqa: ANN002, ANN003
            return _FakeResponse(payload)

    monkeypatch.setattr(nebius.httpx, "Client", FakeClient)

    result = nebius.fetch()

    assert result.prices["openai/gpt-oss-120b"].prompt_micro_per_m == 150_000
    assert result.prices["deepseek-ai/DeepSeek-V4-Pro"].completion_micro_per_m == 3_500_000
    canonical = "nvidia/nemotron-3-ultra-550b-a55b"
    assert result.prices[canonical].prompt_micro_per_m == 600_000
    assert "nvidia/Nemotron-3-Ultra-550b-a55b" not in result.prices
    assert nebius._DISCOVERED_ROWS[canonical]["id"] == canonical
    assert nebius._DISCOVERED_ROWS[canonical]["upstream_id"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert "Qwen/Qwen3-Embedding-8B" not in result.prices

    manifest = tmp_path / "nebius.json"
    manifest.write_text('{"models": []}\n', encoding="utf-8")
    monkeypatch.setattr(nebius, "MANIFEST_PATH", manifest)

    nebius.write_provider_manifest(result)

    rows = json.loads(manifest.read_text(encoding="utf-8"))["models"]
    ids = {row["id"] for row in rows}
    assert canonical in ids
    assert "nvidia/Nemotron-3-Ultra-550b-a55b" not in ids
