from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import telnyx


def _live_model(
    native_id: str,
    *,
    context_length: int = 100_000,
    vision: bool = False,
    pricing: dict[str, str] | None = None,
    owned_by: str = "Telnyx",
) -> dict:
    return {
        "id": native_id,
        "owned_by": owned_by,
        "task": "text-generation",
        "context_length": context_length,
        "max_completion_tokens": 64_000 if native_id.endswith("Kimi-K3") else None,
        "is_vision_supported": vision,
        "regions": ["us-east-1"],
        "pricing": pricing
        or {
            "input": "0.000000",
            "output": "0.000000",
            "cached_prompt": "0.000000",
            "unit": "1M_tokens",
        },
    }


def _live_payload() -> dict:
    rows = [
        _live_model("google/gemma-2b-it", context_length=8192),
        _live_model("meta-llama/Llama-3.3-70B-Instruct", context_length=99_000),
        _live_model("meta-llama/Meta-Llama-3.1-70B-Instruct", context_length=99_000),
        _live_model("meta-llama/Meta-Llama-3.1-8B-Instruct", context_length=131_072),
        _live_model("MiniMaxAI/MiniMax-M2.7", context_length=200_000),
        _live_model("MiniMaxAI/MiniMax-M3-MXFP8", context_length=1_000_000),
        _live_model("moonshotai/Kimi-K2.5", context_length=256_000, vision=True),
        _live_model("moonshotai/Kimi-K2.6", context_length=262_144, vision=True),
        _live_model(
            "moonshotai/Kimi-K3",
            context_length=1_000_000,
            vision=True,
            pricing={
                "input": "2.700000",
                "output": "13.500000",
                "cached_prompt": "0.270000",
                "unit": "1M_tokens",
            },
        ),
        _live_model("Qwen/Qwen3-235B-A22B", context_length=32_768),
        _live_model("zai-org/GLM-5.1-FP8", context_length=202_752),
        _live_model("zai-org/GLM-5.2", context_length=1_000_000),
        _live_model("openai/gpt-5.5", owned_by="openai"),
    ]
    return {"object": "list", "data": rows}


def _x402_payload() -> dict:
    rows = []
    for native_id in telnyx._NATIVE_TO_OR_ID:  # noqa: SLF001
        if native_id == "moonshotai/Kimi-K3":
            continue
        rates = {"input": "0.20", "cached": "0.10", "output": "0.40"}
        if native_id == "MiniMaxAI/MiniMax-M2.7":
            rates = {"input": "0.21", "cached": "0.03", "output": "1.20"}
        elif native_id == "MiniMaxAI/MiniMax-M3-MXFP8":
            rates = {"input": "0.51", "cached": "0.102", "output": "2.04"}
        elif native_id == "moonshotai/Kimi-K2.6":
            rates = {"input": "0.70", "cached": "0.10", "output": "4.40"}
        elif native_id == "zai-org/GLM-5.2":
            rates = {"input": "1.40", "cached": "0.26", "output": "4.40"}
        rows.append(
            {
                "id": native_id,
                "owned_by": "Telnyx",
                "pricing": {"rates": rates},
            }
        )
    rows.append(
        {
            "id": "moonshotai/Kimi-K2.6-long",
            "owned_by": "Telnyx",
            "pricing": {
                "rates": {"input": "0.665", "cached": "0.08", "output": "4.00"}
            },
        }
    )
    return {"object": "list", "data": rows}


def _page_prices() -> ProviderPricingResult:
    return ProviderPricingResult(
        slug="telnyx",
        source="deterministic",
        fetched_url=telnyx.PRICING_URL,
        prices={
            "moonshotai/kimi-k2.6": ModelPrice(
                665_000,
                4_000_000,
                prompt_cached_micro_per_m=80_000,
            ),
            "z-ai/glm-5.2": ModelPrice(
                1_000_000,
                4_000_000,
                prompt_cached_micro_per_m=200_000,
            ),
            "minimax/minimax-m3": ModelPrice(
                270_000,
                1_100_000,
                prompt_cached_micro_per_m=80_000,
            ),
        },
    )


def test_telnyx_fetch_reconciles_all_live_models_with_safe_precedence(
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")

    def fake_fetch_json(url: str, **_kwargs) -> dict:  # noqa: ANN003
        if url == telnyx.MODELS_URL:
            return _live_payload()
        if url == telnyx.X402_MODELS_URL:
            return _x402_payload()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(telnyx, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(telnyx, "fetch_provider", lambda **_kwargs: _page_prices())

    result = telnyx.fetch()

    assert set(result.prices) == set(telnyx.EXPECTED_MODELS)
    assert result.prices["moonshotai/kimi-k3"] == ModelPrice(
        2_700_000,
        13_500_000,
        prompt_cached_micro_per_m=270_000,
    )
    assert result.prices["minimax/minimax-m2.7"] == ModelPrice(
        210_000,
        1_200_000,
        prompt_cached_micro_per_m=30_000,
    )
    # The current pricing page supersedes stale x402 rates.
    assert result.prices["minimax/minimax-m3"] == ModelPrice(
        270_000,
        1_100_000,
        prompt_cached_micro_per_m=80_000,
    )
    assert result.prices["z-ai/glm-5.2"] == ModelPrice(
        1_000_000,
        4_000_000,
        prompt_cached_micro_per_m=200_000,
    )
    assert "moonshotai/kimi-k2.6-long" not in result.prices
    assert "openai/gpt-5.5" not in telnyx._DISCOVERED_MANIFEST_ROWS  # noqa: SLF001


def test_telnyx_zero_catalog_prices_are_not_interpreted_as_free() -> None:
    discovered, direct_prices = telnyx._live_catalog(_live_payload())  # noqa: SLF001

    assert set(discovered) == set(telnyx.EXPECTED_MODELS)
    assert set(direct_prices) == {"moonshotai/kimi-k3"}
    assert direct_prices["moonshotai/kimi-k3"].prompt_micro_per_m == 2_700_000


def test_telnyx_future_owned_priced_model_is_discovered_without_a_hand_map() -> None:
    payload = {
        "data": [
            _live_model(
                "Qwen/Qwen4-Next",
                pricing={
                    "input": "0.123456",
                    "output": "1.234567",
                    "cached_prompt": "0.012345",
                    "unit": "1M_tokens",
                },
            )
        ]
    }

    discovered, prices = telnyx._live_catalog(payload)  # noqa: SLF001

    assert discovered["qwen/qwen4-next"]["upstream_id"] == "Qwen/Qwen4-Next"
    assert prices["qwen/qwen4-next"] == ModelPrice(
        123_456,
        1_234_567,
        prompt_cached_micro_per_m=12_345,
    )
    assert telnyx.UPSTREAM_ID_MAP["qwen/qwen4-next"] == "Qwen/Qwen4-Next"


def test_telnyx_manifest_keeps_context_vision_and_exact_native_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "telnyx.json"
    manifest_path.write_text(
        json.dumps({"provider": "telnyx", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(telnyx, "MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setattr(
        telnyx,
        "fetch_json",
        lambda url, **_kwargs: _live_payload()
        if url == telnyx.MODELS_URL
        else _x402_payload(),
    )
    monkeypatch.setattr(telnyx, "fetch_provider", lambda **_kwargs: _page_prices())

    result = telnyx.fetch()
    notes = telnyx.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in raw["models"]}
    assert raw["model_count"] == 12
    assert "12 priced rows" in notes[0]
    assert rows["moonshotai/kimi-k3"]["upstream_id"] == "moonshotai/Kimi-K3"
    assert rows["moonshotai/kimi-k3"]["context_length"] == 1_000_000
    assert rows["moonshotai/kimi-k3"]["max_output_tokens"] == 64_000
    assert rows["moonshotai/kimi-k3"]["input_modalities"] == ["text", "image"]
    assert rows["z-ai/glm-5.2"]["input_modalities"] == ["text"]
    assert rows["z-ai/glm-5.2"]["provider_regions"] == ["us-east-1"]


def test_telnyx_manifest_is_loaded_as_prepaid_and_byok_catalog_routes() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS

    endpoints = [
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "telnyx"
    ]
    assert len(endpoints) == len(telnyx.EXPECTED_MODELS) * 2
    assert {endpoint.model_id for endpoint in endpoints} == set(telnyx.EXPECTED_MODELS)
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits", "BYOK"}
    assert (
        MODEL_ENDPOINTS["moonshotai/kimi-k3@telnyx/prepaid"].upstream_id
        == "moonshotai/Kimi-K3"
    )
