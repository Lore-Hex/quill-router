from __future__ import annotations

from scripts.pricing.providers import tinfoil


def test_tinfoil_fetch_ingests_glm_52_cached_input_price(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "glm-5-2",
                "pricing": {
                    "inputTokenPricePer1M": 1.5,
                    "cachedInputTokenPricePer1M": 0.375,
                    "outputTokenPricePer1M": 5.25,
                },
            },
            {
                "id": "gemma4-31b",
                "pricing": {
                    "inputTokenPricePer1M": 0.4,
                    "outputTokenPricePer1M": 1.0,
                },
            },
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)

    result = tinfoil.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    gemma = result.prices["google/gemma-4-31b-it"]

    assert glm.prompt_micro_per_m == 1_500_000
    assert glm.completion_micro_per_m == 5_250_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 375_000
    assert gemma.tiers[0].prompt_cached_micro_per_m is None
