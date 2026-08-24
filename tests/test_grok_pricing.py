from __future__ import annotations

import json

import pytest

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult
from scripts.pricing.providers import grok


def test_write_provider_manifest_preserves_grok_46_price_tiers(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "grok.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "grok",
                "models": [
                    {"id": "x-ai/grok-4.6"},
                    {"id": "x-ai/grok-4.5"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(grok, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        grok,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "x-ai/grok-4.6": {
                "id": "x-ai/grok-4.6",
                "upstream_id": "grok-4.6",
            },
            "x-ai/grok-4.5": {
                "id": "x-ai/grok-4.5",
                "upstream_id": "grok-4.5",
            },
        },
    )

    result = ProviderPricingResult(
        slug="grok",
        prices={
            "x-ai/grok-4.6": ModelPrice(
                tiers=[
                    PriceTier(200_000, 2_000_000, 6_000_000, 500_000),
                    PriceTier(None, 4_000_000, 12_000_000, 1_000_000),
                ]
            ),
            "x-ai/grok-4.5": ModelPrice(2_000_000, 6_000_000),
        },
        source="deterministic",
        fetched_url=grok.URL,
    )

    notes = grok.write_provider_manifest(result)

    rows = {
        row["id"]: row
        for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]
    }
    assert rows["x-ai/grok-4.6"]["price_tiers"] == [
        {
            "max_prompt_tokens": 200_000,
            "input_token_price_per_m": 2_000_000,
            "output_token_price_per_m": 6_000_000,
            "cached_input_token_price_per_m": 500_000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 4_000_000,
            "output_token_price_per_m": 12_000_000,
            "cached_input_token_price_per_m": 1_000_000,
        },
    ]
    assert "price_tiers" not in rows["x-ai/grok-4.5"]
    assert notes == ["grok: refreshed provider_models/grok.json (2 priced rows)"]
