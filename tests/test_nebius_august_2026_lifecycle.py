from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import nebius
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_CUTOFF = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
_RETIRING = {
    "nvidia/nemotron-3-ultra-550b-a55b": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "Qwen/Qwen3-32B": "Qwen/Qwen3-32B",
    "NousResearch/Hermes-4-70B": "NousResearch/Hermes-4-70B",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/Llama-3.3-70B-Instruct",
    "Qwen/Qwen2.5-VL-72B-Instruct": "Qwen/Qwen2.5-VL-72B-Instruct",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "MiniMaxAI/MiniMax-M2.5": "MiniMaxAI/MiniMax-M2.5",
    "nvidia/Nemotron-3-Nano-Omni": "nvidia/Nemotron-3-Nano-Omni",
    "deepseek/deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
    "nvidia/Cosmos3-Super-Reasoner": "nvidia/Cosmos3-Super-Reasoner",
    "Qwen/Qwen3-Next-80B-A3B-Thinking": "Qwen/Qwen3-Next-80B-A3B-Thinking",
    "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1": "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1",
}
_SURVIVING = {
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek-ai/DeepSeek-V4-Pro",
    "zai-org/GLM-5.1": "zai-org/GLM-5.1",
    "moonshotai/kimi-k3": "moonshotai/Kimi-K3",
}


def test_nebius_routes_retire_at_announced_date() -> None:
    for model_id, upstream_id in _RETIRING.items():
        assert not provider_lifecycle.provider_model_retired(
            "nebius",
            model_id,
            upstream_id,
            at=_CUTOFF - timedelta(microseconds=1),
        )
        assert provider_lifecycle.provider_model_retired(
            "nebius",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )

    for model_id, upstream_id in _SURVIVING.items():
        assert not provider_lifecycle.provider_model_retired(
            "nebius",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )


def test_nebius_retirements_are_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    for model_id, upstream_id in _RETIRING.items():
        providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
        assert "nebius" not in providers
        assert not provider_lifecycle.provider_model_retired(
            "another-provider",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )

    # This test owns only Nebius's lifecycle. Other providers may add or
    # remove the same checkpoints independently without invalidating it.


def test_hourly_refresh_cannot_restore_retired_nebius_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = {model_id: ModelPrice(1_000_000, 3_000_000) for model_id in {*_RETIRING, *_SURVIVING}}
    result = ProviderPricingResult(
        slug="nebius",
        prices=prices,
        source="api",
        fetched_url=nebius.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"nebius": result})
    for model_id in _RETIRING:
        assert "nebius" in before[model_id]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"nebius": result})
    assert set(_RETIRING).isdisjoint(after)
    for model_id in _SURVIVING:
        assert "nebius" in after[model_id]


def test_nebius_parser_filters_retired_rows_from_stale_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(native_id: str) -> dict[str, object]:
        return {
            "id": native_id,
            "name": native_id,
            "created": 1,
            "context_length": 131_072,
            "architecture": {"modality": "text->text"},
            "pricing": {"prompt": "0.000001", "completion": "0.000003"},
        }

    payload = {
        "data": [
            *(row(upstream_id) for upstream_id in _RETIRING.values()),
            *(row(upstream_id) for upstream_id in _SURVIVING.values()),
        ]
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(nebius.httpx, "Client", FakeClient)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = nebius.fetch()
    assert set(_RETIRING).issubset(before.prices)

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = nebius.fetch()
    assert set(_RETIRING).isdisjoint(after.prices)
    assert set(_RETIRING).isdisjoint(nebius._DISCOVERED_ROWS)
    assert set(_SURVIVING).issubset(after.prices)
