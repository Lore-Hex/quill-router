"""Controlled manifest inputs, independent of hourly provider refreshes."""

from __future__ import annotations

from typing import Any

MANIFEST_STATES = ("without-rerank", "post-refresh", "changed-prices")
RERANK_PRICES = {
    "voyage": {
        "voyage/rerank-3": 50_000,
        "voyage/rerank-3-lite": 20_000,
        "voyage/rerank-2.5": 50_000,
        "voyage/rerank-2.5-lite": 20_000,
        "voyage/rerank-2": 50_000,
        "voyage/rerank-2-lite": 20_000,
        "voyage/rerank-1": 50_000,
        "voyage/rerank-lite-1": 20_000,
    },
    "cohere": {"cohere/rerank-5-fast": 20_000, "cohere/rerank-5-pro": 50_000},
}


def rerank_rows(slug: str, state: str) -> list[dict[str, Any]]:
    assert state in MANIFEST_STATES
    if state == "without-rerank":
        return []
    # Deliberately different stored prices prove that page prices replace them.
    multiplier = 2 if state == "changed-prices" else 1
    return [
        {
            "id": model_id,
            "upstream_id": model_id.split("/", 1)[1],
            "display_name": model_id.split("/", 1)[1],
            "model_type": "rerank",
            "endpoints": ["rerank"],
            "input_modalities": ["text"],
            "output_modalities": ["scores"],
            "input_token_price_per_m": price * multiplier,
            "output_token_price_per_m": 0,
        }
        for model_id, price in RERANK_PRICES[slug].items()
    ]


def provider_manifest(slug: str, state: str = "without-rerank") -> dict[str, Any]:
    model_id, price = {
        "voyage": ("voyage/voyage-3-large", 180_000),
        "cohere": ("cohere/embed-v4.0", 120_000),
    }[slug]
    return {
        "provider": slug,
        "price_scale": "microdollars_per_million",
        "generated_at": "2026-01-01T00:00:00Z",
        "models": [
            {
                "id": model_id,
                "model_type": "embedding",
                "endpoints": ["embeddings"],
                "input_token_price_per_m": price,
                "output_token_price_per_m": 0,
            },
            *rerank_rows(slug, state),
        ],
    }
