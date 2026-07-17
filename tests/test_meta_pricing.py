from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import meta


def _payload() -> dict[str, Any]:
    return {
        "data": {
            "endpoints": [
                {
                    "provider_name": "Meta",
                    "context_length": 1_048_576,
                    "pricing": {
                        "prompt": "0.00000125",
                        "completion": "0.00000425",
                        "input_cache_read": "0.00000015",
                    },
                }
            ]
        }
    }


def test_meta_pricing_reads_exact_openrouter_endpoint_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meta, "fetch_json", lambda _url: _payload())

    result = meta.fetch()

    price = result.prices[meta.MODEL_ID]
    assert result.source == "api"
    assert price.prompt_micro_per_m == 1_250_000
    assert price.completion_micro_per_m == 4_250_000
    assert price.tiers[0].prompt_cached_micro_per_m == 150_000


def test_meta_pricing_rejects_missing_meta_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meta, "fetch_json", lambda _url: {"data": {"endpoints": []}})

    with pytest.raises(RuntimeError, match="did not contain the Meta route"):
        meta.fetch()


def test_meta_pricing_refresh_updates_manifest_without_changing_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "meta.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "meta",
                "models": [
                    {
                        "id": meta.MODEL_ID,
                        "upstream_id": meta.MODEL_ID,
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(meta, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(meta, "fetch_json", lambda _url: _payload())

    notes = meta.write_provider_manifest(meta.fetch())
    refreshed = json.loads(manifest.read_text(encoding="utf-8"))

    assert notes
    assert refreshed["models"][0]["id"] == meta.MODEL_ID
    assert refreshed["models"][0]["upstream_id"] == meta.MODEL_ID
    assert refreshed["models"][0]["input_token_price_per_m"] == 1_250_000
    assert refreshed["models"][0]["output_token_price_per_m"] == 4_250_000
    assert refreshed["models"][0]["cached_input_token_price_per_m"] == 150_000


def test_snapshot_merge_marks_meta_as_openrouter_backed() -> None:
    endpoint = {
        "provider_name": "Meta",
        "tr_provider_slug": "meta",
        "model_id": meta.MODEL_ID,
        "pricing": {
            "prompt": "0.00000125",
            "completion": "0.00000425",
        },
    }
    openrouter_snapshot = {
        "tr_keyed_providers": ["meta"],
        "models": [
            {
                "id": meta.MODEL_ID,
                "name": "Meta: Muse Spark 1.1",
                "context_length": 1_048_576,
                "pricing": endpoint["pricing"],
                "endpoints": [endpoint],
            }
        ],
    }
    price = ModelPrice(
        prompt_micro_per_m=1_250_000,
        completion_micro_per_m=4_250_000,
        prompt_cached_micro_per_m=150_000,
    )

    merged = refresh._merge_snapshot(
        openrouter_snapshot,
        {meta.MODEL_ID: {"meta": price}},
        set(),
    )

    assert merged["models"][0]["pricing_source"] == "openrouter_provider"
    assert merged["models"][0]["endpoints"][0]["pricing_source"] == (
        "openrouter_provider"
    )
