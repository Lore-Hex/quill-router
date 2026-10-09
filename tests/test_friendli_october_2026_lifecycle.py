from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import friendli
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog import ModelEndpoint

_CUTOFF = datetime(2026, 10, 22, tzinfo=UTC)
_MODEL = "minimax/minimax-m2.5"
_NATIVE = "MiniMaxAI/MiniMax-M2.5"
_OTHER = "z-ai/glm-5.3"


def test_friendli_minimax_m25_exact_boundary_and_scope() -> None:
    assert provider_lifecycle.FRIENDLI_MINIMAX_M25_RETIREMENT_AT == _CUTOFF
    retired = provider_lifecycle.provider_model_retired
    assert not retired("friendli", _MODEL, _NATIVE, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("friendli", _MODEL, at=_CUTOFF)
    assert retired("friendli", "alternate-canonical-id", _NATIVE, at=_CUTOFF)
    for other in ("novita", "gmi", "featherless", "io-net", "minimax"):
        assert not retired(other, _MODEL, _NATIVE, at=_CUTOFF)
    assert not retired("friendli", _OTHER, "zai-org/GLM-5.3", at=_CUTOFF)


def test_routed_before_cutover_retired_after(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoints = {}
    for provider in ("friendli", "another-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{_MODEL}@{provider}/{usage}", model_id=_MODEL,
                provider=provider, usage_type=usage, upstream_id=_NATIVE,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(_MODEL)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(_MODEL)
    assert {row.provider for row in remaining} == {"another-provider"}
    assert {row.model_id for row in remaining} == {_MODEL}


def test_hourly_refresh_cannot_resurrect_retired_route(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ProviderPricingResult(
        slug="friendli",
        prices={_MODEL: ModelPrice(300_000, 1_200_000), _OTHER: ModelPrice(1_400_000, 4_400_000)},
        source="api", fetched_url=friendli.URL,
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert set(refresh._index_provider_prices({"friendli": result})) == {_MODEL, _OTHER}
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"friendli": result})) == {_OTHER}


def test_manifest_records_the_announced_retirement() -> None:
    rows = {row["id"]: row for row in json.loads(friendli.MANIFEST_PATH.read_text())["models"]}
    assert rows[_MODEL]["upstream_id"] == _NATIVE
    assert rows[_MODEL]["retirement_at"] == "2026-10-22T00:00:00Z"
    assert "replacement_model_id" not in rows[_MODEL]
    assert "retirement_at" not in rows[_OTHER]


_V32_CUTOFF = datetime(2026, 10, 24, tzinfo=UTC)
_V32 = "deepseek/deepseek-v3.2"
_V32_NATIVE = "deepseek-ai/DeepSeek-V3.2"


def test_friendli_deepseek_v32_exact_boundary_and_scope() -> None:
    assert provider_lifecycle.FRIENDLI_DEEPSEEK_V32_RETIREMENT_AT == _V32_CUTOFF
    retired = provider_lifecycle.provider_model_retired
    assert not retired("friendli", _V32, _V32_NATIVE, at=_V32_CUTOFF - timedelta(microseconds=1))
    assert retired("friendli", _V32, at=_V32_CUTOFF)
    assert retired("friendli", "alternate-canonical-id", _V32_NATIVE, at=_V32_CUTOFF)
    assert not retired("another-provider", _V32, _V32_NATIVE, at=_V32_CUTOFF)
    assert not retired("friendli", _OTHER, "zai-org/GLM-5.3", at=_V32_CUTOFF)


def test_friendli_deepseek_v32_routed_before_cutover_retired_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoints = {}
    for provider in ("friendli", "another-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{_V32}@{provider}/{usage}", model_id=_V32,
                provider=provider, usage_type=usage, upstream_id=_V32_NATIVE,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _V32_CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(_V32)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _V32_CUTOFF)
    assert {row.provider for row in catalog.endpoints_for_model(_V32)} == {"another-provider"}


def test_friendli_deepseek_v32_manifest_records_the_retirement() -> None:
    rows = {row["id"]: row for row in json.loads(friendli.MANIFEST_PATH.read_text())["models"]}
    assert rows[_V32]["upstream_id"] == _V32_NATIVE
    assert rows[_V32]["retirement_at"] == "2026-10-24T00:00:00Z"
