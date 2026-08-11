from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import wafer
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_CUTOFF = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
_RETIRING = {
    "z-ai/glm-5.1": "GLM-5.1",
    "moonshotai/kimi-k3-fast": "kimi-k3-fast",
}
_REPLACEMENTS = {
    "z-ai/glm-5.2": "GLM-5.2",
    "moonshotai/kimi-k3": "Kimi-K3",
}


def test_wafer_routes_retire_at_announced_date() -> None:
    for model_id, upstream_id in _RETIRING.items():
        assert not provider_lifecycle.provider_model_retired(
            "wafer",
            model_id,
            upstream_id,
            at=_CUTOFF - timedelta(microseconds=1),
        )
        assert provider_lifecycle.provider_model_retired(
            "wafer",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )

    for model_id, upstream_id in _REPLACEMENTS.items():
        assert not provider_lifecycle.provider_model_retired(
            "wafer",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )


def test_wafer_retirements_are_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    for model_id in _RETIRING:
        providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
        assert "wafer" not in providers
        assert not provider_lifecycle.provider_model_retired(
            "another-provider",
            model_id,
            _RETIRING[model_id],
            at=_CUTOFF,
        )

    for model_id in _REPLACEMENTS:
        providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
        assert "wafer" in providers


def test_hourly_refresh_cannot_restore_retired_wafer_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = {
        model_id: ModelPrice(1_000_000, 3_000_000)
        for model_id in {*_RETIRING, *_REPLACEMENTS}
    }
    result = ProviderPricingResult(
        slug="wafer",
        prices=prices,
        source="api",
        fetched_url=wafer.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"wafer": result})
    for model_id in _RETIRING:
        assert "wafer" in before[model_id]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"wafer": result})
    for model_id in _RETIRING:
        assert model_id not in after
    for model_id in _REPLACEMENTS:
        assert "wafer" in after[model_id]


def test_wafer_parser_filters_retired_rows_from_stale_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(native_id: str) -> dict[str, object]:
        return {
            "id": native_id,
            "wafer": {
                "pricing": {
                    "input_cents_per_million": 100,
                    "output_cents_per_million": 300,
                }
            },
        }

    payload = {
        "data": [
            row("GLM-5.1"),
            row("GLM-5.2"),
            row("glm5.2-fast"),
            row("MiniMax-M3"),
            row("Kimi-K3"),
            row("kimi-k3-fast"),
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
    before = wafer.fetch()
    assert set(_RETIRING).issubset(before.prices)

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = wafer.fetch()
    assert set(_RETIRING).isdisjoint(after.prices)
    assert set(_RETIRING).isdisjoint(wafer._DISCOVERED_MANIFEST_ROWS)
    assert set(_REPLACEMENTS).issubset(after.prices)


def test_wafer_manifest_records_announced_replacements() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(wafer.MANIFEST_PATH.read_text())["models"]
    }

    assert rows["z-ai/glm-5.1"]["retirement_at"] == "2026-08-17T00:00:00Z"
    assert rows["z-ai/glm-5.1"]["replacement_model_id"] == "z-ai/glm-5.2"
    assert (
        rows["moonshotai/kimi-k3-fast"]["retirement_at"]
        == "2026-08-17T00:00:00Z"
    )
    assert (
        rows["moonshotai/kimi-k3-fast"]["replacement_model_id"]
        == "moonshotai/kimi-k3"
    )
