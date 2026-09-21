from __future__ import annotations

import json

import pytest

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult
from scripts.pricing.providers import grok


@pytest.mark.parametrize("context_length", [None, 600_000])
def test_grok_47_discovery_preserves_pricing_and_documented_capabilities(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    context_length: int | None,
) -> None:
    manifest = tmp_path / "grok.json"
    monkeypatch.setattr(grok, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(grok, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(grok, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    source = {
        "id": "grok-4.7",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "context_length": context_length,
        "prompt_text_token_price": 20_000,
        "cached_prompt_text_token_price": 5_000,
        "completion_text_token_price": 60_000,
        "long_context_threshold": 200_000,
        "prompt_text_token_price_long_context": 40_000,
        "cached_prompt_text_token_price_long_context": 10_000,
        "completion_text_token_price_long_context": 120_000,
    }
    monkeypatch.setattr(
        grok, "fetch_json", lambda *_args, **_kwargs: {
            "models": [source, {**source, "id": "grok-4.6"}]
        },
    )
    probed: list[str] = []
    monkeypatch.setattr(
        grok, "probe_openai_chat",
        lambda **kwargs: probed.append(kwargs["model"]) or True,
    )

    result = grok.fetch()
    grok.write_provider_manifest(result)
    rows = json.loads(manifest.read_text())["models"]
    row = next(row for row in rows if row["id"] == "x-ai/grok-4.7")

    assert "grok-4.7" in probed
    assert row["routable"] is True
    assert row["upstream_id"] == "grok-4.7"
    assert row["context_length"] == (context_length or 500_000)
    assert row["input_modalities"] == ["text", "image"]
    assert {"function-calling", "tool-choice", "reasoning-effort"} <= set(row["features"])
    assert row["price_tiers"] == [
        {
            "max_prompt_tokens": 199_999,
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
    assert not any("fast" in row["id"] for row in rows)


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
