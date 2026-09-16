from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import azure
from scripts.providers.sync_azure_foundry import select_deployment_candidates
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_MODEL = "cohere/command-a-plus-05-2026"
_NATIVE = "Cohere-command-a-plus-05-2026"
_CUTOFF = datetime(2026, 10, 16, tzinfo=UTC)


def test_azure_command_a_plus_date_and_native_deployment_ids() -> None:
    assert provider_lifecycle.AZURE_COMMAND_A_PLUS_RETIREMENT_AT == _CUTOFF
    for native in (_NATIVE, _NATIVE.lower()):
        assert not provider_lifecycle.provider_model_retired(
            "azure", _MODEL, native, at=_CUTOFF - timedelta(microseconds=1),
        )
        assert provider_lifecycle.provider_model_retired("azure", _MODEL, at=_CUTOFF)
        assert provider_lifecycle.provider_model_retired(
            "azure", "unknown-canonical", native, at=_CUTOFF,
        )
        assert not provider_lifecycle.provider_model_retired(
            "cohere", _MODEL, native, at=_CUTOFF,
        )
    assert not provider_lifecycle.provider_model_retired(
        "azure", "cohere/command-a", "cohere-command-a", at=_CUTOFF,
    )


def test_azure_retirement_filters_stale_routes_prices_and_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoints = [
        ModelEndpoint(
            id=f"{_MODEL}@{provider}/{usage}", model_id=_MODEL,
            provider=provider, upstream_id=_NATIVE, usage_type=usage,
        )
        for provider in ("azure", "cohere") for usage in ("Credits", "BYOK")
    ]
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {row.id: row for row in endpoints})
    results = {
        provider: ProviderPricingResult(
            slug=provider, prices={_MODEL: ModelPrice(800_000, 3_200_000)},
            source="api", fetched_url="https://example.com/prices",
        )
        for provider in ("azure", "cohere")
    }
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(_MODEL)) == 4
    assert set(refresh._index_provider_prices(results)[_MODEL]) == {"azure", "cohere"}
    before, _ = provider_lifecycle.provider_catalog_revision()

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(_MODEL)
    assert len(remaining) == 2
    assert {row.provider for row in remaining} == {"cohere"}
    assert {row.model_id for row in remaining} == {_MODEL}
    assert set(refresh._index_provider_prices(results)[_MODEL]) == {"cohere"}
    after, _ = provider_lifecycle.provider_catalog_revision()
    assert after == before + 1


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_azure_sync_cannot_reintroduce_retired_model_even_if_hold_is_lifted(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: (
        _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1)
    ))
    # A later conformance fix must not undo the independently scheduled retirement.
    monkeypatch.setitem(azure._RETAIL_RULES, _MODEL, replace(
        azure._RETAIL_RULES[_MODEL], production_hold_reason=None,
    ))
    assert (_MODEL in azure.retail_model_ids()) is not after_cutoff
    assert (_MODEL in azure.retail_model_versions()) is not after_cutoff
    prices = azure.parse_retail_prices([
        {"productName": "Cohere Models", "skuName": f"Command A Plus {kind} Glbl",
         "meterName": f"Command A Plus {kind} Glbl Tokens", "retailPrice": rate,
         "unitOfMeasure": "1M"}
        for kind, rate in (("Inp", "0.8"), ("Outp", "3.2"))
    ], model_versions={_MODEL: "1"})
    assert (_MODEL in prices) is not after_cutoff
    # ARM can still advertise an active model, and a caller may hold stale prices.
    candidates = select_deployment_candidates([
        {"name": _NATIVE, "version": "1", "format": "Cohere",
         "lifecycleStatus": "Preview", "isDefaultVersion": True,
         "capabilities": {"chatCompletion": "true"}, "skus": [{
             "name": "GlobalStandard", "usageName": "command-a-plus-quota",
             "capacity": {"minimum": 1},
         }]},
    ], [{"name": {"value": "command-a-plus-quota"}, "limit": 20, "currentValue": 0}],
        frozenset({_MODEL}), allowed_versions={_MODEL: frozenset({"1"})},
    )
    assert [row.canonical_id for row in candidates] == ([] if after_cutoff else [_MODEL])


def test_retirement_does_not_publish_the_held_model() -> None:
    assert azure._RETAIL_RULES[_MODEL].production_hold_reason == (
        "openai-tool-call-response-nonconformant"
    )
    rows = json.loads(azure.MANIFEST_PATH.read_text())["models"]
    assert _MODEL not in {row["id"] for row in rows}
