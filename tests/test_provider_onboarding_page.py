from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.provider_contract import (
    PROVIDER_CATALOG_EXAMPLE,
    PROVIDER_CATALOG_SCHEMA_URL,
)


def test_provider_onboarding_page_has_machine_readable_requirements(
    client: TestClient,
) -> None:
    response = client.get("/providers/apply")

    assert response.status_code == 200
    assert "List your models on TrustedRouter." in response.text
    assert "providers@trustedrouter.com" in response.text
    assert "Dedicated API key." in response.text
    assert "OpenAI-compatible base URL." in response.text
    assert "Canonical catalog." in response.text
    assert "Copy this output exactly." in response.text
    assert "GET /v1/models" in response.text
    assert "POST /v1/chat/completions" in response.text
    assert "No separate pricing endpoint is required." in response.text
    assert "per_1m_tokens" in response.text
    assert "Do not invent a second format." in response.text
    assert 'href="/providers/apply/catalog.schema.json"' in response.text
    assert "unrestricted administrative key" in response.text
    assert (
        'href="mailto:providers@trustedrouter.com?subject=Provider%20integration%20for%20%5Bcompany%5D"'
        in response.text
    )
    assert '<link rel="canonical" href="https://trustedrouter.com/providers/apply">' in response.text


def test_provider_catalog_schema_is_public_and_matches_documented_example(
    client: TestClient,
) -> None:
    response = client.get("/providers/apply/catalog.schema.json")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public")
    assert response.headers["content-type"] == "application/schema+json"
    schema = response.json()
    assert schema["$id"] == PROVIDER_CATALOG_SCHEMA_URL
    assert schema["additionalProperties"] is False
    model_schema = schema["$defs"]["model"]
    assert set(model_schema["required"]) == set(PROVIDER_CATALOG_EXAMPLE["data"][0])
    pricing_schema = model_schema["properties"]["pricing"]
    pricing_example = PROVIDER_CATALOG_EXAMPLE["data"][0]["pricing"]
    assert set(pricing_schema["required"]) == set(pricing_example)
    assert pricing_schema["properties"]["unit"]["const"] == "per_1m_tokens"
    assert isinstance(pricing_example["input"], str)
    assert isinstance(pricing_example["output"], str)
    assert model_schema["properties"]["type"]["const"] == "chat"
    assert "embeddings" not in model_schema["properties"]["endpoints"]["items"]["enum"]


def test_provider_onboarding_page_is_discoverable(client: TestClient) -> None:
    providers = client.get("/providers")
    footer = client.get("/")
    sitemap = client.get("/sitemap-core.xml")
    llms = client.get("/llms.txt")

    assert providers.status_code == footer.status_code == sitemap.status_code == llms.status_code == 200
    assert 'href="/providers/apply"' in providers.text
    assert 'href="/providers/apply"' in footer.text
    assert "<loc>https://trustedrouter.com/providers/apply</loc>" in sitemap.text
    assert "Provider onboarding: https://trustedrouter.com/providers/apply" in llms.text


def test_provider_onboarding_page_supports_head(client: TestClient) -> None:
    response = client.head("/providers/apply")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
