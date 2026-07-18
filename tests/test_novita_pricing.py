from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import novita


def test_novita_manifest_writer_updates_scaled_prices(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "novita.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "novita",
                "price_scale_to_microdollars_per_million_tokens": 100,
                "models": [
                    {
                        "id": "moonshotai/kimi-k3",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "cached_input_token_price_per_m": 1,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(novita, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        novita,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "moonshotai/kimi-k3": {
                "id": "moonshotai/kimi-k3",
                "upstream_id": "moonshotai/kimi-k3",
            }
        },
    )
    result = ProviderPricingResult(
        slug="novita",
        prices={
            "moonshotai/kimi-k3": ModelPrice(
                prompt_micro_per_m=3_000_000,
                completion_micro_per_m=15_000_000,
                prompt_cached_micro_per_m=300_000,
            )
        },
        source="deterministic",
    )

    notes = novita.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = raw["models"][0]
    assert row["input_token_price_per_m"] == 30_000
    assert row["output_token_price_per_m"] == 150_000
    assert row["cached_input_token_price_per_m"] == 3_000
    assert raw["model_count"] == 1
    assert notes == ["novita: refreshed provider_models/novita.json (1 priced rows)"]


def test_novita_manifest_writer_rejects_unrepresentable_price(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "novita.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "novita",
                "price_scale_to_microdollars_per_million_tokens": 100,
                "models": [{"id": "test/model"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(novita, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        novita,
        "_DISCOVERED_MANIFEST_ROWS",
        {"test/model": {"id": "test/model", "upstream_id": "test/model"}},
    )
    result = ProviderPricingResult(
        slug="novita",
        prices={
            "test/model": ModelPrice(
                prompt_micro_per_m=101,
                completion_micro_per_m=200,
            )
        },
        source="deterministic",
    )

    try:
        novita.write_provider_manifest(result)
    except RuntimeError as exc:
        assert "not representable" in str(exc)
    else:
        raise AssertionError("expected non-representable Novita price to fail closed")


def test_novita_live_discovery_preserves_existing_ids_and_normalizes_new_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "novita.json"
    manifest_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "Sao10K/L3-8B-Stheno-v3.2",
                        "title": "Sao10K/L3-8B-Stheno-v3.2",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(novita, "MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("NOVITA_API_KEY", "secret")
    monkeypatch.setattr(
        novita,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": "Sao10K/L3-8B-Stheno-v3.2",
                    "display_name": "Stheno",
                    "context_size": 8192,
                    "endpoints": ["chat/completions"],
                },
                {
                    "id": "MoonshotAI/Kimi-K3",
                    "display_name": "Kimi K3",
                    "context_size": 1_048_576,
                    "features": ["reasoning", "serverless"],
                    "input_modalities": ["text", "image", "video"],
                    "output_modalities": ["text"],
                    "endpoints": ["chat/completions", "anthropic"],
                },
                {
                    "id": "example/disabled-chat",
                    "status": 0,
                    "endpoints": ["chat/completions"],
                },
                {
                    "id": "example/embedding-only",
                    "status": 1,
                    "endpoints": ["embeddings"],
                },
            ]
        },
    )

    discovered = novita._live_model_rows()

    assert "Sao10K/L3-8B-Stheno-v3.2" in discovered
    assert "example/disabled-chat" not in discovered
    assert "example/embedding-only" not in discovered
    kimi = discovered["moonshotai/kimi-k3"]
    assert kimi["upstream_id"] == "MoonshotAI/Kimi-K3"
    assert kimi["context_length"] == 1_048_576
    assert kimi["input_modalities"] == ["text", "image", "video"]


def test_novita_manifest_writer_appends_live_priced_model(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "novita.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "novita",
                "price_scale_to_microdollars_per_million_tokens": 100,
                "models": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(novita, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        novita,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "moonshotai/kimi-k3": {
                "id": "moonshotai/kimi-k3",
                "upstream_id": "moonshotai/kimi-k3",
                "display_name": "Kimi K3",
                "context_length": 1_048_576,
                "endpoints": ["chat/completions"],
            }
        },
    )
    result = ProviderPricingResult(
        slug="novita",
        prices={
            "moonshotai/kimi-k3": ModelPrice(
                prompt_micro_per_m=3_000_000,
                completion_micro_per_m=15_000_000,
            )
        },
        source="api",
    )

    notes = novita.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw["models"] == [
        {
            "id": "moonshotai/kimi-k3",
            "upstream_id": "moonshotai/kimi-k3",
            "display_name": "Kimi K3",
            "context_length": 1_048_576,
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 30_000,
            "output_token_price_per_m": 150_000,
        }
    ]
    assert notes == [
        "novita: refreshed provider_models/novita.json (1 priced rows, appended 1)"
    ]
