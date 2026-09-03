from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import xiaomi
from tests.lifecycle_clock import catalog_predates
from trusted_router import provider_lifecycle
from trusted_router.catalog import FAST_MODEL_ORDER, endpoints_for_model

_CUTOFF = datetime(2026, 9, 7, 16, 0, tzinfo=UTC)
_ULTRASPEED = "xiaomi/mimo-v2.5-pro-ultraspeed"
_ULTRASPEED_UPSTREAM = "mimo-v2.5-pro-ultraspeed"
_PRO = "xiaomi/mimo-v2.5-pro"


def test_xiaomi_ultraspeed_retires_at_announced_local_date() -> None:
    assert provider_lifecycle.XIAOMI_MIMO_V25_PRO_ULTRASPEED_RETIREMENT_AT == _CUTOFF
    assert not provider_lifecycle.provider_model_retired(
        "xiaomi",
        _ULTRASPEED,
        _ULTRASPEED_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "xiaomi",
        _ULTRASPEED,
        _ULTRASPEED_UPSTREAM,
        at=_CUTOFF,
    )


def test_xiaomi_ultraspeed_retirement_is_provider_scoped() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "another-provider",
        _ULTRASPEED,
        _ULTRASPEED_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "xiaomi",
        _PRO,
        "mimo-v2.5-pro",
        at=_CUTOFF,
    )


def test_xiaomi_catalog_route_retires_on_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if catalog_predates(_CUTOFF):
        monkeypatch.setattr(
            provider_lifecycle,
            "_utc_now",
            lambda: _CUTOFF - timedelta(microseconds=1),
        )
        assert "xiaomi" in {endpoint.provider for endpoint in endpoints_for_model(_ULTRASPEED)}

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert "xiaomi" not in {endpoint.provider for endpoint in endpoints_for_model(_ULTRASPEED)}
    assert "xiaomi" in {endpoint.provider for endpoint in endpoints_for_model(_PRO)}


def test_hourly_refresh_cannot_restore_retired_xiaomi_ultraspeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="xiaomi",
        prices={
            _ULTRASPEED: ModelPrice(1_305_000, 2_610_000),
            _PRO: ModelPrice(435_000, 870_000),
        },
        source="api",
        fetched_url=xiaomi.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"xiaomi": result})
    assert "xiaomi" in before[_ULTRASPEED]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"xiaomi": result})
    assert _ULTRASPEED not in after
    assert "xiaomi" in after[_PRO]


def test_xiaomi_manifest_records_retirement_without_inventing_replacement() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(xiaomi.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }

    assert rows[_ULTRASPEED]["retirement_at"] == "2026-09-07T16:00:00Z"
    assert "replacement_model_id" not in rows[_ULTRASPEED]


def test_xiaomi_manifest_refresh_restores_retirement_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "xiaomi.json"
    raw = json.loads(xiaomi.MANIFEST_PATH.read_text(encoding="utf-8"))
    ultraspeed = next(row for row in raw["models"] if row["id"] == _ULTRASPEED)
    ultraspeed.pop("retirement_at", None)
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(xiaomi, "MANIFEST_PATH", manifest_path)

    xiaomi.write_provider_manifest(
        ProviderPricingResult(
            slug="xiaomi",
            prices={
                "xiaomi/mimo-v2.5": ModelPrice(140_000, 280_000),
                _PRO: ModelPrice(435_000, 870_000),
            },
            source="fixture",
            fetched_url=xiaomi.URL,
        )
    )

    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    refreshed_ultraspeed = next(row for row in refreshed["models"] if row["id"] == _ULTRASPEED)
    assert refreshed_ultraspeed["retirement_at"] == "2026-09-07T16:00:00Z"


def test_fast_pool_no_longer_depends_on_limited_beta() -> None:
    assert _ULTRASPEED not in FAST_MODEL_ORDER
