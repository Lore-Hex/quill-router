from dataclasses import replace
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.pinned_manifests import OPENAI_GPT_5_5, serve_manifest_rows
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    endpoint_meets_privacy_requirement,
    endpoint_zero_data_retention,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
)
from trusted_router.catalog_privacy import endpoint_retention_metadata
from trusted_router.config import Settings
from trusted_router.managed_retention import managed_retention_policy
from trusted_router.routing import chat_route_endpoint_candidates


@pytest.fixture(autouse=True)
def azure_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    serve_manifest_rows(monkeypatch, tmp_path, "azure", [{
        **OPENAI_GPT_5_5,
        "id": "openai/gpt-5-mini", "upstream_id": "gpt-5-mini",
        "display_name": "gpt-5-mini", "title": "gpt-5-mini",
    }])


def test_azure_approval_is_managed_and_model_scoped() -> None:
    provider = PROVIDERS["azure"]
    assert provider.provider_zero_data_retention is False
    assert provider.prepaid_zero_data_retention is True
    assert provider.prepaid_zero_data_retention_effective_on == "2026-11-12"
    shape = provider_to_openrouter_shape(provider)
    assert shape["retention_requires_model_review"] is True
    assert shape["zero_data_retention_scope"] == "trustedrouter_prepaid"
    assert "ContentLogging=false" in shape["retention_notice"]
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False


@pytest.mark.parametrize("model,upstream,usage,eligible", [
    ("openai/gpt-5-mini", "gpt-5-mini", "Credits", True),
    ("openai/gpt-5-mini", "gpt-5-mini", "BYOK", False),
    ("openai/gpt-5-mini", "gpt-new", "Credits", False),
    ("openai/gpt-new", "gpt-5-mini", "Credits", False),
    ("openai/gpt-5.4-mini", "gpt-5-4-mini", "Credits", False),
    ("cohere/command-a", "cohere-command-a", "Credits", True),
    ("moonshotai/kimi-k2.5", "kimi-k2-5", "Credits", True),
    ("moonshotai/kimi-k2.6", "kimi-k2-6", "Credits", True),
    ("moonshotai/kimi-k2.7-code", "kimi-k2-7-code", "Credits", True),
    ("x-ai/grok-4.1-fast-reasoning", "grok-4-1-fast-reasoning", "Credits", True),
    ("x-ai/grok-4.1-fast-non-reasoning", "grok-4-1-fast-non-reasoning", "Credits", True),
    ("x-ai/grok-4.20-reasoning", "grok-4-20-reasoning", "Credits", True),
    ("x-ai/grok-4.20-non-reasoning", "grok-4-20-non-reasoning", "Credits", True),
    ("x-ai/grok-4.3", "grok-4-3", "Credits", False),
    ("microsoft/microsoft-decision-1", "tr-decision-1", "Credits", True),
    ("microsoft/mai-voice-2.1", "MAI-Voice-2.1", "Credits", False),
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4-6", "Credits", False),
])
def test_only_reviewed_azure_hosted_foundry_qualifies(
    model: str, upstream: str, usage: str, eligible: bool,
) -> None:
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="azure",
                       model_id=model, upstream_id=upstream, usage_type=usage)
    assert endpoint_zero_data_retention(endpoint) is eligible
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION) is eligible
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
    metadata = endpoint_retention_metadata(endpoint)
    assert metadata is not None
    assert metadata["strict_zdr_eligible"] is eligible
    if eligible:
        assert metadata["reviewed_on"] == "2026-11-12"
        assert metadata["human_review"] is False
        assert metadata["maximum_days"] == 0


def test_azure_zdr_catalog_and_routing_agree() -> None:
    shape = model_to_openrouter_shape(MODELS["openai/gpt-5-mini"])
    azure = [endpoint for endpoint in shape["trustedrouter"]["endpoints"]
             if endpoint["provider"] == "azure"]
    assert azure
    assert all(endpoint["provider_zero_data_retention"] is True for endpoint in azure)
    assert all(endpoint["usage_type"] == "Credits" for endpoint in azure)
    candidates = chat_route_endpoint_candidates(
        {"model": "openai/gpt-5-mini",
         "provider": {"only": ["azure"], "min_privacy": "zdr", "allow_fallbacks": False}},
        Settings(environment="test"),
    )
    assert candidates
    assert all(endpoint.provider == "azure" and endpoint.usage_type == "Credits"
               for _, endpoint in candidates)


def test_azure_approval_can_be_revoked_without_changing_route_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PROVIDERS, "azure", replace(PROVIDERS["azure"], prepaid_zero_data_retention=False))
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="azure",
                       model_id="openai/gpt-5-mini", upstream_id="gpt-5-mini", usage_type="Credits")
    assert managed_retention_policy("azure", endpoint.model_id, endpoint.upstream_id, "Credits")
    assert endpoint_zero_data_retention(endpoint) is False


def test_azure_provider_and_model_pages_show_scoped_zdr(client: TestClient) -> None:
    providers = BeautifulSoup(client.get("/providers").text, "html.parser")
    card = providers.select_one('[data-provider-id="azure"]')
    assert card is not None
    assert card.select_one('[data-privacy="zdr"]') is not None
    detail = client.get("/providers/azure")
    assert detail.status_code == 200
    assert "ContentLogging=false" in detail.text
    assert "Partner-hosted" in detail.text
    model = client.get("/models/openai/gpt-5-mini")
    assert model.status_code == 200
    azure = BeautifulSoup(model.text, "html.parser").select_one("#provider-azure")
    assert azure is not None
    assert azure.select_one('[data-privacy="zdr"]') is not None
    assert azure.select_one('[data-privacy="confidential"]') is None
