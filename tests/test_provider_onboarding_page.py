from __future__ import annotations

from fastapi.testclient import TestClient


def test_provider_onboarding_page_has_machine_readable_requirements(
    client: TestClient,
) -> None:
    response = client.get("/providers/apply")

    assert response.status_code == 200
    assert "List your models on TrustedRouter." in response.text
    assert "providers@trustedrouter.com" in response.text
    assert "Dedicated API key." in response.text
    assert "Inference API URL." in response.text
    assert "Models API URL." in response.text
    assert "Pricing API URL." in response.text
    assert "GET /v1/models" in response.text
    assert "GET /v1/pricing" in response.text
    assert "unrestricted administrative key" in response.text
    assert (
        'href="mailto:providers@trustedrouter.com?subject=Provider%20integration%20for%20%5Bcompany%5D"'
        in response.text
    )
    assert '<link rel="canonical" href="https://trustedrouter.com/providers/apply">' in response.text


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
