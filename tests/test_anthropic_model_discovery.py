from __future__ import annotations

import json

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import anthropic as anthropic_parser
from scripts.pricing.providers import anthropic
from trusted_router.catalog import (
    MODELS,
    endpoint_zero_data_retention,
    endpoints_for_model,
)


def _opus_5_api_row() -> dict[str, object]:
    return {
        "type": "model",
        "id": "claude-opus-5",
        "display_name": "Claude Opus 5",
        "created_at": "2026-07-24T00:00:00Z",
        "max_input_tokens": 1_000_000,
        "max_tokens": 128_000,
        "capabilities": {
            "image_input": {"supported": True},
            "structured_outputs": {"supported": True},
            "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
        },
    }


def test_anthropic_parser_discovers_future_claude_names_and_cache_price() -> None:
    html = """
    <div class="card">
      <div><h3 class="card_pricing_title_text">Opus 6</h3></div>
      <span class="tokens_main_val_number" data-value="5"></span>
      <span class="tokens_main_val_number" data-value="25"></span>
      <span class="tokens_main_val_number" data-value="6.25"></span>
      <span class="tokens_main_val_number" data-value="0.50"></span>
    </div>
    """

    assert anthropic_parser.parse(html)["anthropic/claude-opus-6"] == {
        "prompt_micro_per_m": 5_000_000,
        "completion_micro_per_m": 25_000_000,
        "prompt_cached_micro_per_m": 500_000,
    }


def test_anthropic_models_api_row_preserves_native_id_and_capabilities(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        anthropic,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [_opus_5_api_row()],
            "has_more": False,
        },
    )

    rows = anthropic._live_model_rows()

    assert rows == {
        "anthropic/claude-opus-5": {
            "id": "anthropic/claude-opus-5",
            "display_name": "Claude Opus 5",
            "title": "claude-opus-5",
            "context_length": 1_000_000,
            "max_output_tokens": 128_000,
            "model_type": "chat",
            "features": ["function-calling", "structured-outputs", "reasoning"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "upstream_id": "claude-opus-5",
            "created_at": "2026-07-24T00:00:00Z",
            "status": 1,
        }
    }


def test_anthropic_fetch_requires_price_for_newly_discovered_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        anthropic,
        "_live_model_rows",
        lambda: {
            "anthropic/claude-opus-5": {
                "id": "anthropic/claude-opus-5",
                "upstream_id": "claude-opus-5",
            }
        },
    )
    monkeypatch.setattr(anthropic, "_known_manifest_model_ids", frozenset)

    def fake_fetch_provider(**kwargs):
        captured.update(kwargs)
        return ProviderPricingResult(
            slug="anthropic",
            prices={
                "anthropic/claude-opus-5": ModelPrice(
                    5_000_000,
                    25_000_000,
                    prompt_cached_micro_per_m=500_000,
                )
            },
            source="deterministic",
        )

    monkeypatch.setattr(anthropic, "fetch_provider", fake_fetch_provider)

    result = anthropic.fetch()

    assert captured["required_models"] == frozenset({"anthropic/claude-opus-5"})
    assert result.notes == ["discovered 1 Anthropic account models"]


def test_anthropic_fetch_discovers_new_model_without_secret_access(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "_manifest_rows_by_id", dict)

    def fake_fetch_provider(**_kwargs):
        return ProviderPricingResult(
            slug="anthropic",
            prices={"anthropic/claude-opus-6": ModelPrice(5_000_000, 25_000_000)},
            source="deterministic",
        )

    monkeypatch.setattr(anthropic, "fetch_provider", fake_fetch_provider)

    result = anthropic.fetch()
    discovered = anthropic._DISCOVERED_MANIFEST_ROWS["anthropic/claude-opus-6"]

    assert result.notes == ["discovered 1 Anthropic public pricing models"]
    assert discovered["upstream_id"] == "claude-opus-6"
    assert discovered["context_length"] == 1_000_000
    assert discovered["max_output_tokens"] == 128_000


def test_anthropic_public_discovery_does_not_resurrect_legacy_models(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        anthropic,
        "_manifest_rows_by_id",
        lambda: {
            "anthropic/claude-opus-5": {
                "id": "anthropic/claude-opus-5",
                "upstream_id": "claude-opus-5",
            }
        },
    )

    rows = anthropic._public_pricing_model_rows(
        {
            "anthropic/claude-opus-4.1": ModelPrice(15_000_000, 75_000_000),
            "anthropic/claude-opus-5": ModelPrice(5_000_000, 25_000_000),
            "anthropic/claude-opus-6": ModelPrice(5_000_000, 25_000_000),
        }
    )

    assert set(rows) == {
        "anthropic/claude-opus-5",
        "anthropic/claude-opus-6",
    }


def test_anthropic_manifest_writer_publishes_discovered_opus_5(
    monkeypatch,
    tmp_path,
) -> None:
    manifest = tmp_path / "anthropic.json"
    manifest.write_text('{"models":[]}\n', encoding="utf-8")
    monkeypatch.setattr(anthropic, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        anthropic,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "anthropic/claude-opus-5": {
                "id": "anthropic/claude-opus-5",
                "display_name": "Claude Opus 5",
                "title": "claude-opus-5",
                "context_length": 1_000_000,
                "max_output_tokens": 128_000,
                "model_type": "chat",
                "features": ["function-calling", "structured-outputs", "reasoning"],
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "upstream_id": "claude-opus-5",
                "status": 1,
            }
        },
    )
    result = ProviderPricingResult(
        slug="anthropic",
        prices={
            "anthropic/claude-opus-5": ModelPrice(
                5_000_000,
                25_000_000,
                prompt_cached_micro_per_m=500_000,
            )
        },
        source="deterministic",
    )

    anthropic.write_provider_manifest(result)
    row = json.loads(manifest.read_text(encoding="utf-8"))["models"][0]

    assert row["id"] == "anthropic/claude-opus-5"
    assert row["upstream_id"] == "claude-opus-5"
    assert row["input_token_price_per_m"] == 5_000_000
    assert row["output_token_price_per_m"] == 25_000_000
    assert row["cached_input_token_price_per_m"] == 500_000


def test_opus_5_catalog_is_routable_for_chat_and_messages_but_not_zdr() -> None:
    model = MODELS["anthropic/claude-opus-5"]
    endpoints = endpoints_for_model(model.id)
    anthropic_endpoints = [
        endpoint for endpoint in endpoints if endpoint.provider == "anthropic"
    ]

    assert model.context_length == 1_000_000
    assert model.supports_chat is True
    assert model.supports_messages is True
    assert {endpoint.usage_type for endpoint in anthropic_endpoints} == {
        "Credits",
        "BYOK",
    }
    assert {endpoint.upstream_id for endpoint in anthropic_endpoints} == {
        "claude-opus-5"
    }
    assert {
        (
            endpoint.prompt_price_microdollars_per_million_tokens,
            endpoint.completion_price_microdollars_per_million_tokens,
        )
        for endpoint in anthropic_endpoints
    } == {(5_250_000, 26_250_000)}
    assert all(
        endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
        == 525_000
        for endpoint in anthropic_endpoints
    )
    assert not any(endpoint_zero_data_retention(endpoint) for endpoint in endpoints)
