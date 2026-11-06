from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trusted_router import catalog_ingest
from trusted_router.pricing import (
    _customer_price,
    _provider_manifest_cached_customer_price,
    select_price_tier,
)
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars


@pytest.mark.parametrize("cost,expected", [(0, 10_000), (1, 2), (5_000, 5_275), (650_000, 685_750)])
def test_hixtube_cache_tariff_keeps_markup_and_explicit_zero_floor(cost: int, expected: int) -> None:
    assert _provider_manifest_cached_customer_price(
        cost, provider_slug="hixtube", apply_markup=True,
    ) == expected
    assert _customer_price(cost) == max(expected, 10_000)


@pytest.mark.parametrize("provider", ["baseten", "deepseek", "Hixtube", "", "unknown"])
def test_other_provider_cache_floors_are_unchanged(provider: str) -> None:
    assert _provider_manifest_cached_customer_price(
        5_000, provider_slug=provider, apply_markup=True,
    ) == 10_000


@pytest.mark.parametrize("provider", ["hixtube", "baseten"])
def test_passthrough_cache_prices_stay_passthrough(provider: str) -> None:
    assert _provider_manifest_cached_customer_price(
        5_000, provider_slug=provider, apply_markup=False,
    ) == 5_000


def _manifest(*, tiered: bool, cached: int | None, held: bool = False) -> dict:
    start = datetime(2026, 11, 5, tzinfo=UTC)
    rates = {"input_token_price_per_m": 650_000, "output_token_price_per_m": 1_450_000}
    if cached is not None:
        rates["cached_input_token_price_per_m"] = cached
    row = {
        "id": "deepseek/deepseek-v4.1-flash-1017", "model_type": "chat",
        "endpoints": ["chat/completions"], "context_length": 1_048_576,
        "routable": not held, "lifecycle_status": "retired" if held else "active",
        "retirement_at": (start + timedelta(hours=2)).isoformat(), **rates,
    }
    if held:
        row["routable_reason"] = "pilot-retired"
    if tiered:
        row["price_tiers"] = [
            {"max_prompt_tokens": 500_000, **rates},
            {"max_prompt_tokens": None, **{key: 2 * value for key, value in rates.items()}},
        ]
    return {
        "provider": "hixtube", "price_scale": "microdollars_per_million", "models": [row],
        "generated_at": start.isoformat(), "pricing_valid_until": row["retirement_at"],
    }


@pytest.mark.parametrize("tiered", [False, True])
@pytest.mark.parametrize("prompt,cached", [(500_000, 0), (500_000, 500_000), (1_000_000, 500_000)])
def test_cache_tariff_matches_catalog_and_both_cost_paths(
    tiered: bool, prompt: int, cached: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _manifest(tiered=tiered, cached=5_000)
    (tmp_path / "hixtube.json").write_text(json.dumps(raw))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints(
        at=datetime(2026, 11, 5, 1, tzinfo=UTC),
    )
    model = models["deepseek/deepseek-v4.1-flash-1017"]
    endpoint = endpoints[f"{model.id}@hixtube/prepaid"]
    assert model.price_tiers == model.published_price_tiers == endpoint.price_tiers == endpoint.published_price_tiers
    multiplier = 2 if tiered and prompt > 500_000 else 1
    tier = select_price_tier(endpoint.price_tiers, prompt)
    assert tier.prompt_cached_price_microdollars_per_million_tokens == 5_275 * multiplier
    assert tier.prompt_price_microdollars_per_million_tokens == 685_750 * multiplier
    assert tier.completion_price_microdollars_per_million_tokens == 1_529_750 * multiplier
    expected = sum((tokens * rate + 500_000) // 1_000_000 for tokens, rate in (
        (prompt - cached, 685_750 * multiplier), (cached, 5_275 * multiplier),
        (100, 1_529_750 * multiplier),
    ))
    assert cost_microdollars(model, prompt, 100, cached_input_tokens=cached) == expected
    assert _endpoint_cost_microdollars(endpoint, prompt - cached, 100, cache_read_tokens=cached) == expected
    assert raw["models"][0]["cached_input_token_price_per_m"] == 5_000
    assert endpoint.catalog_valid_until == datetime(2026, 11, 5, 2, tzinfo=UTC)


@pytest.mark.parametrize("tiered", [False, True])
@pytest.mark.parametrize("cached,expected", [(None, None), (0, 10_000)])
def test_absent_or_zero_cache_price_does_not_invent_discount(
    tiered: bool, cached: int | None, expected: int | None,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "hixtube.json").write_text(json.dumps(_manifest(tiered=tiered, cached=cached)))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, _ = catalog_ingest._supplemental_provider_models_and_endpoints(
        at=datetime(2026, 11, 5, 1, tzinfo=UTC),
    )
    assert models["deepseek/deepseek-v4.1-flash-1017"].price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == expected


def test_discount_cannot_reactivate_retired_hixtube(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "hixtube.json").write_text(json.dumps(_manifest(tiered=False, cached=5_000, held=True)))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    assert catalog_ingest._supplemental_provider_models_and_endpoints(
        at=datetime(2026, 11, 5, 1, tzinfo=UTC),
    ) == ({}, {})


@pytest.mark.parametrize("later_cost,admitted", [(5_000, False), (10_000, True)])
def test_zero_to_positive_cache_tiers_cannot_decrease_retail_price(
    later_cost: int, admitted: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = _manifest(tiered=True, cached=0)
    tiers = raw["models"][0]["price_tiers"]
    tiers[1].update(tiers[0], max_prompt_tokens=None, cached_input_token_price_per_m=later_cost)
    (tmp_path / "hixtube.json").write_text(json.dumps(raw))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints(
        at=datetime(2026, 11, 5, 1, tzinfo=UTC),
    )
    if not admitted:
        assert (models, endpoints) == ({}, {})
    else:
        model = models["deepseek/deepseek-v4.1-flash-1017"]
        assert [tier.prompt_cached_price_microdollars_per_million_tokens for tier in model.price_tiers] == [10_000, 10_550]
