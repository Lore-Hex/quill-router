from __future__ import annotations

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import zai


def test_zai_glm_53_flash_is_required_and_preserves_launch_capabilities(
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("ZAI_API_KEY", "secret")
    monkeypatch.setattr(
        zai,
        "fetch_provider",
        lambda **_kwargs: ProviderPricingResult(
            slug="zai",
            prices={
                model_id: ModelPrice(
                    prompt_micro_per_m=150_000,
                    completion_micro_per_m=500_000,
                    prompt_cached_micro_per_m=30_000,
                )
                for model_id in zai.EXPECTED_MODELS
            },
            source="deterministic",
            fetched_url=zai.URL,
        ),
    )
    monkeypatch.setattr(
        zai,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [{"id": model_id.removeprefix("z-ai/")} for model_id in zai.EXPECTED_MODELS]
        },
    )

    result = zai.fetch()

    assert "z-ai/glm-5.3-flash" in result.prices
    flash = zai._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.3-flash"]
    assert flash["context_length"] == 1_048_576
    assert flash["max_output_tokens"] == 131_072
    assert flash["input_modalities"] == ["text", "image", "video"]
    assert flash["output_modalities"] == ["text"]
    assert set(flash["supported_features"]) == {
        "reasoning",
        "structured_outputs",
        "tools",
        "prompt_cache",
    }
