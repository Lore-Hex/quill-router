from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import wafer
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_CUTOFF = datetime(2026, 9, 5, 6, 59, tzinfo=UTC)
_MODEL_ID = "z-ai/glm-5.2"
_UPSTREAM_ID = "GLM-5.2"


def test_wafer_glm52_retires_at_announced_pacific_cutoff() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "wafer",
        _MODEL_ID,
        _UPSTREAM_ID,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "wafer",
        _MODEL_ID,
        _UPSTREAM_ID,
        at=_CUTOFF,
    )


def test_wafer_glm52_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    providers = {endpoint.provider for endpoint in endpoints_for_model(_MODEL_ID)}
    assert "wafer" not in providers
    assert providers
    assert not provider_lifecycle.provider_model_retired(
        "another-provider",
        _MODEL_ID,
        _UPSTREAM_ID,
        at=_CUTOFF,
    )


def test_hourly_refresh_cannot_restore_retired_wafer_glm52(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="wafer",
        prices={_MODEL_ID: ModelPrice(1_260_000, 3_960_000)},
        source="api",
        fetched_url=wafer.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    assert refresh._index_provider_prices({"wafer": result})[_MODEL_ID]["wafer"]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert _MODEL_ID not in refresh._index_provider_prices({"wafer": result})


def test_wafer_parser_filters_glm52_from_stale_feed_after_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(native_id: str) -> dict[str, object]:
        return {
            "id": native_id,
            "wafer": {
                "pricing": {
                    "input_cents_per_million": 126,
                    "output_cents_per_million": 396,
                }
            },
        }

    payload = {
        "data": [
            row(_UPSTREAM_ID),
            row("Kimi-K2.6"),
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

    monkeypatch.setattr(wafer.httpx, "Client", FakeClient)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    assert _MODEL_ID in wafer.fetch().prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = wafer.fetch()
    assert _MODEL_ID not in after.prices
    assert "moonshotai/kimi-k2.6" in after.prices
    assert _MODEL_ID not in wafer._DISCOVERED_MANIFEST_ROWS


def test_wafer_manifest_records_glm52_retirement() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(wafer.MANIFEST_PATH.read_text())["models"]
    }

    assert rows[_MODEL_ID]["retirement_at"] == "2026-09-05T06:59:00Z"
