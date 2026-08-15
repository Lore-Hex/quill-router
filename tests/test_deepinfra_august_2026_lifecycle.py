from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS, endpoints_for_model
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
_TERMINUS = "deepseek/deepseek-v3.1-terminus"
_TERMINUS_UPSTREAM = "deepseek-ai/DeepSeek-V3.1-Terminus"
_FLASH = "deepseek/deepseek-v4-flash-0731"
_FLASH_UPSTREAM = "deepseek-ai/DeepSeek-V4-Flash-0731"


def test_deepinfra_terminus_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _TERMINUS,
        _TERMINUS_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _TERMINUS,
        _TERMINUS_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _FLASH,
        _FLASH_UPSTREAM,
        at=_CUTOFF,
    )


def test_deepinfra_terminus_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider discovery may remove or add routes independently of an announced
    # cutoff. Use a self-contained catalog so this test covers only the
    # provider-scoped lifecycle rule.
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == _TERMINUS:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    for provider, upstream_id in (
        ("deepinfra", _TERMINUS_UPSTREAM),
        ("novita", "deepseek/deepseek-v3.1-terminus"),
    ):
        endpoint = ModelEndpoint(
            id=f"{_TERMINUS}@{provider}/prepaid",
            model_id=_TERMINUS,
            provider=provider,
            usage_type="Credits",
            upstream_id=upstream_id,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = {endpoint.provider for endpoint in endpoints_for_model(_TERMINUS)}
    assert before == {"deepinfra", "novita"}

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = {endpoint.provider for endpoint in endpoints_for_model(_TERMINUS)}

    assert after == {"novita"}


def test_hourly_refresh_cannot_restore_retired_deepinfra_terminus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="deepinfra",
        prices={
            _TERMINUS: ModelPrice(270_000, 950_000),
            _FLASH: ModelPrice(80_000, 180_000),
        },
        source="api",
        fetched_url="https://api.deepinfra.com/v1/openai/models",
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"deepinfra": result})
    assert "deepinfra" in before[_TERMINUS]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"deepinfra": result})

    assert _TERMINUS not in after
    assert "deepinfra" in after[_FLASH]
