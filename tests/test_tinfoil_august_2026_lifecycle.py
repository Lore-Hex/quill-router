from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import tinfoil
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_CUTOFF = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
_K26 = "moonshotai/kimi-k2.6"
_K26_UPSTREAM = "kimi-k2-6"
_K3 = "moonshotai/kimi-k3"
_K3_UPSTREAM = "kimi-k3"


def test_tinfoil_kimi_k26_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "tinfoil",
        _K26,
        _K26_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "tinfoil",
        _K26,
        _K26_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "tinfoil",
        _K3,
        _K3_UPSTREAM,
        at=_CUTOFF,
    )


def test_tinfoil_kimi_k26_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    providers = {endpoint.provider for endpoint in endpoints_for_model(_K26)}

    assert "tinfoil" not in providers
    assert providers


def test_hourly_refresh_cannot_restore_retired_tinfoil_kimi_k26(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="tinfoil",
        prices={
            _K26: ModelPrice(1_500_000, 5_250_000),
            _K3: ModelPrice(3_000_000, 15_000_000),
        },
        source="api",
        fetched_url=tinfoil.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"tinfoil": result})
    assert "tinfoil" in before[_K26]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"tinfoil": result})
    assert _K26 not in after
    assert "tinfoil" in after[_K3]


def test_tinfoil_feed_discovers_kimi_k3_only_when_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tinfoil, "UPSTREAM_ID_MAP", dict(tinfoil.UPSTREAM_ID_MAP))
    payload = {
        "data": [
            {
                "id": "glm-5-2",
                "pricing": {
                    "inputTokenPricePer1M": 1.5,
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
            {
                "id": _K3_UPSTREAM,
                "name": "Kimi K3",
                "type": "chat",
                "context_window": 262_144,
                "endpoints": ["/v1/chat/completions", "/v1/responses"],
                "reasoning": True,
                "tool_calling": True,
                "multimodal": True,
                "pricing": {
                    "inputTokenPricePer1M": 4.0,
                    "cachedInputTokenPricePer1M": 0.8,
                    "outputTokenPricePer1M": 20.0,
                },
            },
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)

    result = tinfoil.fetch()

    assert result.prices[_K3].prompt_micro_per_m == 4_000_000
    assert result.prices[_K3].completion_micro_per_m == 20_000_000
    assert result.prices[_K3].tiers[0].prompt_cached_micro_per_m == 800_000
    assert tinfoil.UPSTREAM_ID_MAP[_K3] == _K3_UPSTREAM
    assert tinfoil._DISCOVERED_MANIFEST_ROWS[_K3] == {
        "id": _K3,
        "upstream_id": _K3_UPSTREAM,
        "display_name": "Kimi K3",
        "title": _K3,
        "model_type": "chat",
        "features": ["reasoning", "function-calling", "multimodal"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions", "responses"],
        "status": 1,
        "context_length": 262_144,
    }


def test_tinfoil_feed_marks_launched_kimi_k3_as_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {
                "id": "glm-5-2",
                "pricing": {
                    "inputTokenPricePer1M": 1.5,
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

    assert _K3 not in result.prices
    assert _K3 in tinfoil.EXPECTED_MODELS
