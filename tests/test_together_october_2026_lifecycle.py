from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import together
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 10, 22, tzinfo=UTC)
_RETIRING = {
    "meta-llama/llama-3.3-70b-instruct": (
        "meta-llama/Llama-3.3-70B-Instruct-Turbo", "meta-models/muse-glimmer-30b",
    ),
    "deepseek/deepseek-v4-flash-0731": (
        "deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek/deepseek-v4.1-flash",
    ),
}


def test_together_october_cutoff_is_midnight_utc() -> None:
    assert provider_lifecycle.TOGETHER_OCTOBER_22_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize("model", sorted(_RETIRING))
def test_together_route_routed_before_cutover_retired_after(
    monkeypatch: pytest.MonkeyPatch, model: str,
) -> None:
    native, _ = _RETIRING[model]
    retired = provider_lifecycle.provider_model_retired
    assert not retired("together", model, native, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("together", model, at=_CUTOFF)
    assert retired("together", "unknown-canonical", native, at=_CUTOFF)
    assert not retired("unaffected-provider", model, native, at=_CUTOFF)
    endpoints = {}
    for slug in ("together", "unaffected-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{model}@{slug}/{usage}", model_id=model, provider=slug,
                usage_type=usage, upstream_id=native,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(model)
    assert {row.provider for row in remaining} == {"unaffected-provider"}
    assert {row.upstream_id for row in remaining} == {native}


@pytest.mark.parametrize("model", sorted(_RETIRING))
def test_together_replacements_are_quoted_not_substituted(model: str) -> None:
    _, replacement = _RETIRING[model]
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "together" and model in entry.model_ids
        and entry.effective_at == _CUTOFF
    ]
    assert len(entries) == 1
    assert entries[0].replacement_model_ids == (replacement,)
    assert not provider_lifecycle.provider_model_retired("together", replacement, at=_CUTOFF)


def test_together_refresh_cannot_restore_retired_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    keep = "deepseek/deepseek-v4.1-flash"
    result = ProviderPricingResult(
        slug="together", source="api", fetched_url=together.URL,
        prices={model: ModelPrice(100_000, 200_000) for model in (*_RETIRING, keep)},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"together": result})) == {keep}


def test_together_manifest_records_the_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(together.MANIFEST_PATH.read_text())["models"]}
    for model, (native, replacement) in _RETIRING.items():
        if model not in rows:
            continue
        assert rows[model]["upstream_id"] == native
        assert rows[model]["retirement_at"] == "2026-10-22T00:00:00Z"
        assert rows[model]["replacement_model_id"] == replacement
