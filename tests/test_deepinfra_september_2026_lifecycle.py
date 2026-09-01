from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import deepinfra
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
_KIMI_K25 = "moonshotai/kimi-k2.5"
_KIMI_K25_UPSTREAM = "moonshotai/Kimi-K2.5"
_KIMI_K3 = "moonshotai/kimi-k3"
_KIMI_K3_UPSTREAM = "moonshotai/Kimi-K3"


def test_deepinfra_kimi_k25_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _KIMI_K25,
        _KIMI_K25_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _KIMI_K25,
        _KIMI_K25_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _KIMI_K3,
        _KIMI_K3_UPSTREAM,
        at=_CUTOFF,
    )


def test_deepinfra_kimi_k25_retirement_is_provider_scoped() -> None:
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _KIMI_K25,
        _KIMI_K25_UPSTREAM,
        at=_CUTOFF,
    )
    for provider in ("alibaba", "atlas-cloud", "kimi", "novita", "siliconflow"):
        assert not provider_lifecycle.provider_model_retired(
            provider,
            _KIMI_K25,
            at=_CUTOFF,
        )


def test_hourly_refresh_cannot_restore_retired_deepinfra_kimi_k25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="deepinfra",
        prices={
            _KIMI_K25: ModelPrice(450_000, 2_250_000),
            _KIMI_K3: ModelPrice(2_850_000, 14_250_000),
        },
        source="api",
        fetched_url=deepinfra.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"deepinfra": result})
    assert "deepinfra" in before[_KIMI_K25]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"deepinfra": result})

    assert _KIMI_K25 not in after
    assert "deepinfra" in after[_KIMI_K3]


def test_deepinfra_parser_filters_retired_kimi_k25_from_stale_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(native_id: str) -> dict[str, object]:
        return {
            "id": native_id,
            "metadata": {
                "pricing": {
                    "input_tokens": 0.45,
                    "output_tokens": 2.25,
                }
            },
        }

    payload = {"data": [row(_KIMI_K25_UPSTREAM), row(_KIMI_K3_UPSTREAM)]}

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

    monkeypatch.setattr(deepinfra.httpx, "Client", FakeClient)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    result = deepinfra.fetch()

    assert _KIMI_K25 not in result.prices
    assert _KIMI_K25 not in deepinfra._DISCOVERED_MANIFEST_ROWS
    assert _KIMI_K3 in result.prices
    assert _KIMI_K3 in deepinfra._DISCOVERED_MANIFEST_ROWS


def test_deepinfra_manifest_records_announced_kimi_replacement() -> None:
    rows = {row["id"]: row for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]}

    retired = rows[_KIMI_K25]
    assert retired["retirement_at"] == "2026-09-07T00:00:00Z"
    assert retired["replacement_model_id"] == _KIMI_K3
