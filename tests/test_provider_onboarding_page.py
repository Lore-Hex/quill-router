from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.provider_contract import (
    PROVIDER_CATALOG_EXAMPLE,
    PROVIDER_CATALOG_SCHEMA_URL,
    PROVIDER_CATALOG_V2_EXAMPLE,
    PROVIDER_CATALOG_V2_SCHEMA_URL,
)


def test_provider_onboarding_page_has_machine_readable_requirements(
    client: TestClient,
) -> None:
    response = client.get("/providers/marketplace")

    assert response.status_code == 200
    assert "List your models on TrustedRouter." in response.text
    assert "providers@trustedrouter.com" in response.text
    assert "Legal company name:" in response.text
    assert "Privacy policy:" in response.text
    assert "DUNS number:" in response.text
    assert "Registered address:" in response.text
    assert "Business phone:" in response.text
    assert "EIN / VAT / corporate tax ID:" in response.text
    assert "Primary contact name:" in response.text
    assert "Primary contact title:" in response.text
    assert "Primary contact email:" in response.text
    assert "Primary contact phone:" in response.text
    assert "Company registration number:" in response.text
    assert "Technical contact name / title / email / phone:" in response.text
    assert "CEO:" in response.text
    assert "Subprocessor list:" in response.text
    assert "Do not email an API key" in response.text
    assert "API key: DO NOT INCLUDE" in response.text
    assert "separate secure handoff" in response.text
    assert "OpenAI-compatible base URL." in response.text
    assert "Canonical catalog." in response.text
    assert "Provider Reliability Contract v2" in response.text
    assert "Copy this output exactly." in response.text
    assert "GET /v1/models" in response.text
    assert "POST /v1/chat/completions" in response.text
    assert "No separate pricing endpoint is required." in response.text
    assert "per_1m_tokens" in response.text
    assert "Do not invent a second format." in response.text
    assert "Featured partnership" in response.text
    assert "Neurometric AI is live on TrustedRouter." in response.text
    assert 'href="/providers/neurometric"' in response.text
    assert "ZDR classification: not contractual" in response.text
    assert client.get("/providers/neurometric").status_code == 200
    assert 'href="/providers/marketplace/catalog.schema.json"' in response.text
    assert 'href="/providers/marketplace/catalog.v2.schema.json"' in response.text
    assert "Always send <code>Retry-After</code>" in response.text
    assert "Provider sign in" in response.text
    assert "Exclude account administration, billing, user management" in response.text
    assert (
        'href="mailto:providers@trustedrouter.com?subject=Provider%20marketplace%20application%20for%20%5Bcompany%5D"'
        in response.text
    )
    assert (
        '<link rel="canonical" href="https://trustedrouter.com/providers/marketplace">'
        in response.text
    )


def test_provider_catalog_schema_is_public_and_matches_documented_example(
    client: TestClient,
) -> None:
    response = client.get("/providers/marketplace/catalog.schema.json")

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


def test_provider_reliability_contract_v2_is_public_and_complete(
    client: TestClient,
) -> None:
    response = client.get("/providers/marketplace/catalog.v2.schema.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["$id"] == PROVIDER_CATALOG_V2_SCHEMA_URL
    assert set(schema["required"]) == {
        "object",
        "contract_version",
        "provider",
        "data",
    }
    model_schema = schema["$defs"]["model"]
    assert "reliability" in model_schema["required"]
    assert set(model_schema["properties"]["reliability"]["required"]) == set(
        PROVIDER_CATALOG_V2_EXAMPLE["data"][0]["reliability"]
    )
    provider_schema = schema["properties"]["provider"]
    assert "error_contract" in provider_schema["required"]
    assert (
        provider_schema["properties"]["error_contract"]["properties"][
            "overload_status"
        ]["const"]
        == 503
    )


def test_provider_onboarding_page_is_discoverable(client: TestClient) -> None:
    providers = client.get("/providers")
    footer = client.get("/")
    sitemap = client.get("/sitemap-core.xml")
    llms = client.get("/llms.txt")

    assert providers.status_code == footer.status_code == sitemap.status_code == llms.status_code == 200
    assert 'href="/providers/marketplace"' in providers.text
    assert 'href="/providers/marketplace"' in footer.text
    assert "<loc>https://trustedrouter.com/providers/marketplace</loc>" in sitemap.text
    assert "Provider marketplace: https://trustedrouter.com/providers/marketplace" in llms.text
    assert "https://trustedrouter.com/providers/apply" not in sitemap.text
    assert "https://trustedrouter.com/providers/apply" not in llms.text


def test_provider_onboarding_page_supports_head(client: TestClient) -> None:
    response = client.head("/providers/marketplace")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_legacy_provider_apply_routes_redirect_permanently(client: TestClient) -> None:
    page = client.get("/providers/apply", follow_redirects=False)
    page_with_slash = client.get("/providers/apply/", follow_redirects=False)
    schema = client.get(
        "/providers/apply/catalog.schema.json",
        follow_redirects=False,
    )

    assert page.status_code == 301
    assert page.headers["location"] == "/providers/marketplace"
    assert page_with_slash.status_code == 301
    assert page_with_slash.headers["location"] == "/providers/marketplace"
    assert schema.status_code == 301
    assert schema.headers["location"] == "/providers/marketplace/catalog.schema.json"
