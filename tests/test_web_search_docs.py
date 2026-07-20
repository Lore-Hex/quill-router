from __future__ import annotations

from fastapi.testclient import TestClient


def test_web_search_docs_publish_contract_and_privacy_boundary(client: TestClient) -> None:
    response = client.get("/docs/web-search")

    assert response.status_code == 200
    assert '"type": "web_search"' in response.text
    assert "response.web_search_call.completed" in response.text
    assert "web_search_call.action.sources" in response.text
    assert "Exa receives those queries" in response.text
    assert "trustedrouter/zdr" in response.text
    assert "trustedrouter/e2e" in response.text


def test_web_search_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/web-search"' in client.get("/docs").text
    assert "/docs/web-search" in client.get("/docs/llms.txt").text
    assert "/docs/web-search" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/web-search</loc>"
        in client.get("/sitemap-core.xml").text
    )


def test_exa_is_disclosed_as_a_system_subprocessor(client: TestClient) -> None:
    response = client.get("/legal/subprocessors.json")

    assert response.status_code == 200
    exa = next(item for item in response.json()["system_subprocessors"] if item["name"] == "Exa")
    assert "model-generated search query" in exa["data_access"]
    assert "blocked on ZDR, E2E/confidential, and EU" in exa["data_access"]
    assert exa["policy_url"] == "https://exa.ai/privacy"
