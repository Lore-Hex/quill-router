from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trusted_router import catalog_ingest, provider_contracts, provider_lifecycle
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
            or endpoint.provider in {"decisionmodelsio", "decisionmodelsio-eu"}
        )
        for endpoint in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values()
    )


@pytest.mark.parametrize("provider", ["crusoe", "sambanova", "lightning"])
def test_rejected_operator_credentials_block_only_prepaid(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    # Simulate fresh discovery claiming availability after a key rejection.
    # Isolate credential holds from the later provider-wide security incident.
    monkeypatch.setattr(provider_contracts, "PROVIDER_INCIDENT_HOLD_REASONS", {})
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
            usage_type=usage, upstream_id="lightning-ai/fixture" if slug == "lightning" else model,
        )
        for slug in (provider, "novita") for usage in ("Credits", "BYOK")
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(endpoints)
    assert f"{provider}/Credits" not in filtered
    assert f"{provider}/BYOK" in filtered
    assert {"novita/Credits", "novita/BYOK"} <= filtered.keys()
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {})
    assert catalog_ingest._filter_unserved_provider_endpoints(endpoints) == endpoints


def test_lightning_fresh_discovery_cannot_bypass_operator_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "lightning-ai/future-model"
    monkeypatch.setattr(provider_contracts, "PROVIDER_INCIDENT_HOLD_REASONS", {})
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {
        "lightning": "operator-credential-rejected",
    })
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    endpoints = {
        usage: ModelEndpoint(
            id=usage, model_id=model, provider="lightning", usage_type=usage, upstream_id=model,
        ) for usage in ("Credits", "BYOK")
    }
    # Even a new explicit registration must not bypass the rejected operator key.
    assert catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, explicit_model_ids=frozenset({model}),
    ) == {"BYOK": endpoints["BYOK"]}


def test_streamlake_qwen3_coder_next_retirement_spares_qwen_3_8_flash() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    cutoff = provider_lifecycle.STREAMLAKE_QWEN3_CODER_NEXT_RETIREMENT_AT
    assert not provider_lifecycle.provider_model_retired(
        "streamlake", "qwen/qwen3.8-flash", "qwen3.8-flash", at=cutoff,
    )


def test_streamlake_qwen3_235b_instruct_retirement_is_observation_dated() -> None:
    cutoff = provider_lifecycle.STREAMLAKE_QWEN3_235B_INSTRUCT_RETIREMENT_AT
    assert cutoff == datetime(2026, 10, 10, 19, 39, tzinfo=UTC)
    retired = provider_lifecycle.provider_model_retired
    # StreamLake still serves the Thinking variant.
    assert not retired(
        "streamlake",
        "qwen/qwen3-235b-a22b-thinking-2507",
        "Qwen3-235B-A22B-Thinking-2507",
        at=cutoff,
    )
