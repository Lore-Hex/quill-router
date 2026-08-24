from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
_MODEL = "inclusionai/ling-3.0-tiny"


def test_novita_ling_30_tiny_retires_at_exact_announced_time() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "novita",
        _MODEL,
        _MODEL,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "novita",
        _MODEL,
        _MODEL,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _MODEL,
        _MODEL,
        at=_CUTOFF,
    )


def test_hourly_refresh_cannot_restore_retired_novita_ling_30_tiny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="novita",
        prices={_MODEL: ModelPrice(10_000, 10_000)},
        source="api",
        fetched_url="https://api.novita.ai/v3/openai/models",
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"novita": result})
    assert "novita" in before[_MODEL]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"novita": result})
    assert _MODEL not in after
