from fastapi.testclient import TestClient


def test_confidential_hostname_docs_require_explicit_policy(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    for domain in ("trustedrouter", "quillrouter", "allyrouter", "uptimerouter"):
        assert f"https://api.confidential.{domain}.com/v1" in response.text
    assert "confidential_privacy_required" in response.text
    assert "not an implicit default" in response.text
    assert "Hosted web search, batch/files, embeddings, and media" in response.text
    assert "not independent proof" in response.text
    assert 'id="confidential-api"' in response.text


def test_routing_guide_links_to_confidential_origin_contract(client: TestClient) -> None:
    response = client.get("/docs/provider-routing")
    assert response.status_code == 200
    assert 'href="/docs#confidential-api"' in response.text
