from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from trusted_router.catalog import (
    MODELS,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    ZDR_MODEL_ID,
    ModelEndpoint,
    endpoint_privacy_tier,
    endpoint_zero_data_retention,
    endpoints_for_model,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates


def _first_party_openai_endpoints() -> tuple[ModelEndpoint, ModelEndpoint]:
    endpoints = [
        endpoint
        for endpoint in endpoints_for_model("openai/gpt-5.5")
        if endpoint.provider == "openai"
    ]
    credits = next(endpoint for endpoint in endpoints if endpoint.usage_type == "Credits")
    byok = next(endpoint for endpoint in endpoints if endpoint.usage_type == "BYOK")
    return credits, byok


def _activate_openai_prepaid_zdr(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setitem(
        PROVIDERS,
        "openai",
        replace(PROVIDERS["openai"], prepaid_zero_data_retention=True),
    )


def test_openai_zdr_contract_is_scheduled_but_not_active() -> None:
    provider = PROVIDERS["openai"]
    credits, byok = _first_party_openai_endpoints()

    assert provider.provider_zero_data_retention is False
    assert provider.prepaid_zero_data_retention is False
    assert provider.prepaid_zero_data_retention_effective_on == "2026-07-28"
    assert endpoint_zero_data_retention(credits) is False
    assert endpoint_privacy_tier(credits) == PRIVACY_TIER_STANDARD
    assert endpoint_zero_data_retention(byok) is False
    assert endpoint_privacy_tier(byok) == PRIVACY_TIER_STANDARD


def test_openai_zdr_activation_is_scoped_to_trustedrouter_prepaid_routes(
    monkeypatch: MonkeyPatch,
) -> None:
    _activate_openai_prepaid_zdr(monkeypatch)
    credits, byok = _first_party_openai_endpoints()

    assert endpoint_zero_data_retention(credits) is True
    assert endpoint_privacy_tier(credits) == PRIVACY_TIER_ZERO_RETENTION
    assert endpoint_zero_data_retention(byok) is False
    assert endpoint_privacy_tier(byok) == PRIVACY_TIER_STANDARD


def test_openai_catalog_metadata_does_not_extend_tr_contract_to_byok(
    monkeypatch: MonkeyPatch,
) -> None:
    _activate_openai_prepaid_zdr(monkeypatch)
    provider_shape = provider_to_openrouter_shape(PROVIDERS["openai"])
    assert provider_shape["provider_zero_data_retention"] is False
    assert provider_shape["prepaid_zero_data_retention"] is True
    assert provider_shape["zero_data_retention_scope"] == "trustedrouter_prepaid"

    shape = model_to_openrouter_shape(MODELS["openai/gpt-5.5"])
    endpoint_shapes = [
        endpoint
        for endpoint in shape["trustedrouter"]["endpoints"]
        if endpoint["provider"] == "openai"
    ]
    by_usage = {endpoint["usage_type"]: endpoint for endpoint in endpoint_shapes}
    assert by_usage["Credits"]["provider_zero_data_retention"] is True
    assert by_usage["Credits"]["zero_data_retention_scope"] == "trustedrouter_prepaid"
    assert by_usage["BYOK"]["provider_zero_data_retention"] is False
    assert by_usage["BYOK"]["zero_data_retention_scope"] is None


def test_openai_zdr_filter_selects_credits_and_never_inherits_byok(
    monkeypatch: MonkeyPatch,
) -> None:
    _activate_openai_prepaid_zdr(monkeypatch)
    candidates = chat_route_endpoint_candidates(
        {
            "model": "openai/gpt-5.5",
            "provider": {"only": ["openai"], "min_privacy": "zdr"},
        },
        Settings(environment="test"),
    )

    assert candidates
    assert all(endpoint.provider == "openai" for _model, endpoint in candidates)
    assert all(endpoint.usage_type == "Credits" for _model, endpoint in candidates)
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in candidates
    )


def test_zdr_alias_can_use_openai_only_through_contracted_credits_route(
    monkeypatch: MonkeyPatch,
) -> None:
    _activate_openai_prepaid_zdr(monkeypatch)
    candidates = chat_route_endpoint_candidates(
        {"model": ZDR_MODEL_ID, "provider": {"only": ["openai"]}},
        Settings(environment="test"),
    )

    assert candidates
    assert all(endpoint.provider == "openai" for _model, endpoint in candidates)
    assert all(endpoint.usage_type == "Credits" for _model, endpoint in candidates)


def test_public_pages_explain_scheduled_openai_prepaid_scope(client: TestClient) -> None:
    providers = client.get("/providers")
    assert providers.status_code == 200
    assert "scheduled 2026-07-28" in providers.text
    assert "July 28, 2026" in providers.text
    assert "customer BYOK credentials" in providers.text

    model = client.get("/models/openai/gpt-5.5")
    assert model.status_code == 200
    assert "Credits" in model.text
    assert "BYOK" in model.text
    assert "upstream varies" in model.text
