from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router import catalog_ingest, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS


def test_perplexity_billing_hold_survives_fresh_catalog_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PREPAID_PROVIDER_HOLD_REASONS["perplexity"] == "billing-contract-unverified"
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    endpoints = {
        provider: ModelEndpoint(
            id=provider, model_id="perplexity/sonar", provider=provider,
            usage_type="Credits", upstream_id="sonar",
        )
        for provider in ("perplexity", "other-provider")
    }
    assert catalog_ingest._filter_unserved_provider_endpoints(endpoints) == {
        "other-provider": endpoints["other-provider"],
    }


def test_perplexity_stays_visible_without_managed_billing_routes() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS, provider_to_openrouter_shape

    assert "perplexity" in PROVIDERS
    assert not any(
        endpoint.provider == "perplexity" and endpoint.usage_type == "Credits"
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert provider_to_openrouter_shape(PROVIDERS["perplexity"])["routing_status"] == "blocked"


def test_held_perplexity_tariff_is_available_only_for_settlement() -> None:
    from trusted_router.catalog import endpoint_for_id, endpoints_for_model
    from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat

    identity = "perplexity/sonar@perplexity/prepaid"
    held = HELD_PREPAID_SETTLEMENT_ENDPOINTS[identity]
    assert held.request_price_microdollars > 0
    assert _endpoint_for_id_compat(identity) == held
    assert endpoint_for_id(identity) is None
    assert held not in endpoints_for_model(held.model_id)
    assert all(
        endpoint.usage_type == "Credits"
        and (
            endpoint.provider in PREPAID_PROVIDER_HOLD_REASONS
            or endpoint.provider in {"system1models", "system1models-eu"}
        )
        for endpoint in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values()
    )


@pytest.mark.parametrize("provider", ["crusoe", "sambanova"])
def test_rejected_operator_credentials_block_only_prepaid(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    # Simulate fresh discovery claiming availability after a key rejection.
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {
        provider: "operator-credential-rejected",
    })
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    model = "openai/gpt-oss-120b"
    endpoints = {
        f"{slug}/{usage}": ModelEndpoint(
            id=f"{slug}/{usage}", model_id=model, provider=slug,
            usage_type=usage, upstream_id=model,
        )
        for slug in (provider, "novita") for usage in ("Credits", "BYOK")
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(endpoints)
    assert f"{provider}/Credits" not in filtered
    assert f"{provider}/BYOK" in filtered
    assert {"novita/Credits", "novita/BYOK"} <= filtered.keys()
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {})
    assert catalog_ingest._filter_unserved_provider_endpoints(endpoints) == endpoints


@pytest.mark.parametrize("native", ["kat-coder-pro-v2", "kat-coder-air-v2.5", "kat-coder-pro-v2.5"])
def test_streamlake_confirmed_retirements_are_provider_scoped(native: str) -> None:
    cutoff = datetime(2026, 10, 3, 20, tzinfo=UTC)
    model = f"kwaipilot/{native}"
    retired = provider_lifecycle.provider_model_retired
    assert not retired("streamlake", model, native, at=cutoff - timedelta(microseconds=1))
    assert retired("streamlake", model, at=cutoff)
    assert retired("streamlake", "other-canonical-alias", native, at=cutoff)
    assert not retired("novita", model, native, at=cutoff)
    assert not retired("streamlake", "kwaipilot/future-model", "future-model", at=cutoff)


def test_stale_streamlake_prices_cannot_reenable_deprecated_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: datetime(2026, 10, 4, tzinfo=UTC))
    models = ["kwaipilot/kat-coder-pro-v2", "kwaipilot/kat-coder-air-v2.5", "kwaipilot/kat-coder-pro-v2.5"]
    result = ProviderPricingResult(
        slug="streamlake", source="stale_snapshot", fetched_url="https://www.streamlake.ai",
        prices={model: ModelPrice(300_000, 1_200_000) for model in models},
    )
    assert refresh._index_provider_prices({"streamlake": result}) == {}


@pytest.mark.parametrize("usage", ["Credits", "BYOK"])
def test_retired_streamlake_endpoint_is_removed_without_model_substitution(
    monkeypatch: pytest.MonkeyPatch, usage: str,
) -> None:
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    model = "kwaipilot/kat-coder-pro-v2.5"
    endpoints = {
        provider: ModelEndpoint(
            id=provider, provider=provider, model_id=model,
            usage_type=usage, upstream_id="kat-coder-pro-v2.5",
        )
        for provider in ("streamlake", "novita")
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    assert filtered == {"novita": endpoints["novita"]}
