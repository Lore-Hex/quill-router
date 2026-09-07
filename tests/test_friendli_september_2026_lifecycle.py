from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import friendli
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog import ModelEndpoint

_CUTOFF = datetime(2026, 9, 6, tzinfo=UTC)
_MODEL = "lgai-exaone/k-exaone-2.0-750b-a37b"
_NATIVE = "LGAI-EXAONE/K-EXAONE-2.0-750B-A37B"
_OTHER = "z-ai/glm-5.2"


def test_friendli_exaone_2_exact_boundary_and_scope() -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("friendli", _MODEL, _NATIVE, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("friendli", _MODEL, at=_CUTOFF)
    assert retired("friendli", "alternate-canonical-id", _NATIVE, at=_CUTOFF)
    assert not retired("another-provider", _MODEL, _NATIVE, at=_CUTOFF)
    assert not retired("friendli", _OTHER, "zai-org/GLM-5.2", at=_CUTOFF)


def test_stale_runtime_catalog_retires_without_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoints = {}
    for provider in ("friendli", "another-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{_MODEL}@{provider}/{usage}", model_id=_MODEL,
                provider=provider, usage_type=usage, upstream_id=_NATIVE,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1),
    )
    assert len(catalog.endpoints_for_model(_MODEL)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(_MODEL)
    assert len(remaining) == 2
    assert {row.provider for row in remaining} == {"another-provider"}
    assert {row.model_id for row in remaining} == {_MODEL}


def test_hourly_refresh_cannot_resurrect_retired_route(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ProviderPricingResult(
        slug="friendli",
        prices={_MODEL: ModelPrice(600_000, 2_400_000), _OTHER: ModelPrice(1_400_000, 4_400_000)},
        source="api", fetched_url=friendli.URL,
    )
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1),
    )
    assert "friendli" in refresh._index_provider_prices({"friendli": result})[_MODEL]
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"friendli": result})
    assert _MODEL not in after
    assert "friendli" in after[_OTHER]


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_discovery_filters_stale_feed(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool,
) -> None:
    payload = {"data": [
        {"id": native, "pricing": {"input": "0.0000006", "output": "0.0000024"}}
        for native in (_NATIVE, "zai-org/GLM-5.2")
    ]}
    monkeypatch.setattr(
        friendli.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(friendli, "UPSTREAM_ID_MAP", dict(friendli.UPSTREAM_ID_MAP))
    monkeypatch.setattr(friendli, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = friendli.fetch()
    expected = {_OTHER} if after_cutoff else {_OTHER, _MODEL}
    assert set(result.prices) == expected
    assert set(friendli._DISCOVERED_MANIFEST_ROWS) == expected
