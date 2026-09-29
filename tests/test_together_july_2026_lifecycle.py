from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import together
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS, endpoints_for_model
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
_M27 = "minimax/minimax-m2.7"
_M27_UPSTREAM = "MiniMaxAI/MiniMax-M2.7"
_M3 = "minimax/minimax-m3"
_M3_UPSTREAM = "MiniMaxAI/MiniMax-M3"


def test_together_minimax_m27_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "together",
        _M27,
        _M27_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "together",
        _M27,
        _M27_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "together",
        _M3,
        _M3_UPSTREAM,
        at=_CUTOFF,
    )


def test_together_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider discovery may remove or add routes independently of an announced
    # cutoff. Use a self-contained catalog so this test covers only the
    # provider-scoped lifecycle rule.
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id in (_M27, _M3):
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    for provider, model_id, upstream_id in (
        ("together", _M27, _M27_UPSTREAM),
        ("another-provider", _M27, _M27),
        ("together", _M3, _M3_UPSTREAM),
    ):
        endpoint = ModelEndpoint(
            id=f"{model_id}@{provider}/prepaid",
            model_id=model_id,
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
    before = {endpoint.provider for endpoint in endpoints_for_model(_M27)}
    assert before == {"together", "another-provider"}

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    m27_providers = {endpoint.provider for endpoint in endpoints_for_model(_M27)}
    m3_providers = {endpoint.provider for endpoint in endpoints_for_model(_M3)}

    assert m27_providers == {"another-provider"}
    assert m3_providers == {"together"}


def test_hourly_refresh_drops_retired_together_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="together",
        prices={
            _M27: ModelPrice(300_000, 1_200_000),
            _M3: ModelPrice(300_000, 1_200_000),
        },
        source="api",
        fetched_url=together.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"together": result})
    assert "together" in before[_M27]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"together": result})
    assert _M27 not in after
    assert "together" in after[_M3]


def test_together_parser_pins_replacement_and_retiring_native_ids() -> None:
    assert together.UPSTREAM_ID_MAP[_M27] == _M27_UPSTREAM
    assert together.UPSTREAM_ID_MAP[_M3] == _M3_UPSTREAM
    assert _M3 in together.EXPECTED_MODELS
