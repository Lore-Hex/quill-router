from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_AUGUST_CUTOFF = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_QWEN_CUTOFF = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
_AUGUST_RETIRING = {
    "minimax/minimax-m2.7": "accounts/fireworks/models/minimax-m2p7",
    "openai/gpt-oss-20b": "accounts/fireworks/models/gpt-oss-20b",
    "moonshotai/kimi-k2.6-fast": "accounts/fireworks/routers/kimi-k2p6-turbo",
    "moonshotai/kimi-k2.7-code-fast": "accounts/fireworks/routers/kimi-k2p7-code-fast",
    "deepseek/deepseek-v4-pro": "accounts/fireworks/models/deepseek-v4-pro",
}
_AUGUST_REPLACEMENTS = {
    "minimax/minimax-m3": "accounts/fireworks/models/minimax-m3",
    "nvidia/nemotron-3.5-lightning": (
        "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
    ),
    "z-ai/glm-5.2": "accounts/fireworks/models/glm-5p2",
    "deepseek/deepseek-v4-pro-0813": "accounts/fireworks/models/deepseek-v4-pro-0813",
}
_STANDARD_KIMI = {
    "moonshotai/kimi-k2.6": "accounts/fireworks/models/kimi-k2p6",
    "moonshotai/kimi-k2.7-code": "accounts/fireworks/models/kimi-k2p7-code",
}


def test_fireworks_august_routes_retire_without_standard_kimi() -> None:
    for model_id, upstream_id in _AUGUST_RETIRING.items():
        assert not provider_lifecycle.provider_model_retired(
            "fireworks",
            model_id,
            upstream_id,
            at=_AUGUST_CUTOFF - timedelta(microseconds=1),
        )
        assert provider_lifecycle.provider_model_retired(
            "fireworks",
            model_id,
            upstream_id,
            at=_AUGUST_CUTOFF,
        )

    for model_id, upstream_id in {**_STANDARD_KIMI, **_AUGUST_REPLACEMENTS}.items():
        assert not provider_lifecycle.provider_model_retired(
            "fireworks",
            model_id,
            upstream_id,
            at=_AUGUST_CUTOFF,
        )


def test_fireworks_qwen_extension_uses_its_own_cutoff() -> None:
    old_model = "qwen/qwen3.7-plus"
    old_upstream = "accounts/fireworks/models/qwen3p7-plus"

    assert not provider_lifecycle.provider_model_retired(
        "fireworks",
        old_model,
        old_upstream,
        at=_QWEN_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "fireworks",
        old_model,
        old_upstream,
        at=_QWEN_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "fireworks",
        "qwen/qwen3.8-max",
        "accounts/fireworks/models/qwen3p8-max",
        at=_QWEN_CUTOFF,
    )


def test_fireworks_retirements_are_provider_scoped() -> None:
    for model_id, upstream_id in {
        **_AUGUST_RETIRING,
        "qwen/qwen3.7-plus": "accounts/fireworks/models/qwen3p7-plus",
    }.items():
        assert not provider_lifecycle.provider_model_retired(
            "another-provider",
            model_id,
            upstream_id,
            at=_QWEN_CUTOFF,
        )


def test_hourly_refresh_cannot_restore_retired_fireworks_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = {
        model_id: ModelPrice(1_000_000, 3_000_000)
        for model_id in {
            *_AUGUST_RETIRING,
            *_AUGUST_REPLACEMENTS,
            *_STANDARD_KIMI,
            "qwen/qwen3.7-plus",
            "qwen/qwen3.8-max",
        }
    }
    result = ProviderPricingResult(
        slug="fireworks",
        prices=prices,
        source="api",
        fetched_url="https://api.fireworks.ai/inference/v1/models",
    )

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _AUGUST_CUTOFF)
    august = refresh._index_provider_prices({"fireworks": result})
    for model_id in _AUGUST_RETIRING:
        assert "fireworks" not in august.get(model_id, {})
    for model_id in {*_AUGUST_REPLACEMENTS, *_STANDARD_KIMI, "qwen/qwen3.7-plus"}:
        assert "fireworks" in august[model_id]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)
    september = refresh._index_provider_prices({"fireworks": result})
    assert "fireworks" not in september.get("qwen/qwen3.7-plus", {})
    assert "fireworks" in september["qwen/qwen3.8-max"]


def test_catalog_filters_fireworks_route_at_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _AUGUST_CUTOFF)

    assert all(
        endpoint.provider != "fireworks"
        for endpoint in endpoints_for_model("deepseek/deepseek-v4-pro")
    )
    assert any(
        endpoint.provider == "fireworks"
        for endpoint in endpoints_for_model("deepseek/deepseek-v4-pro-0813")
    )
