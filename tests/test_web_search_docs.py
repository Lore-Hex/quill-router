from __future__ import annotations

import json

from bs4 import BeautifulSoup
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
    assert "/v1/chat/completions" in response.text
    assert "openrouter:web_search" in response.text
    assert '"max_tool_calls": 1' in response.text
    assert "not implemented and return explicit errors" in response.text
    assert '"engine": "context.dev"' in response.text
    assert '"engine": "context"' not in response.text
    assert "$0.006 per provider credit" in response.text
    assert "not the provider's actual dollar cost" in response.text
    assert "unused reservation is released" in response.text
    assert "conservative_credit_tariff" in response.text
    assert "retention and use for model training" in response.text


def test_web_search_docs_are_discoverable(client: TestClient) -> None:
    section = BeautifulSoup(client.get("/docs/web-search").text, "html.parser").select_one("#parallel")
    assert section and section.get_text(strip=True) == "Choose Parallel Search"
    for path in ("/docs", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert "/docs/web-search#parallel" in client.get(path).text
    assert 'href="/docs/web-search"' in client.get("/docs").text
    assert "/docs/web-search" in client.get("/docs/llms.txt").text
    assert "/docs/web-search" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/web-search</loc>"
        in client.get("/sitemap-core.xml").text
    )


def test_context_dev_docs_have_a_direct_link_and_canonical_engine(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/docs/web-search").text, "html.parser")
    headings = soup.select("h2#context-dev")
    assert len(headings) == 1
    heading = headings[0]
    assert heading.get_text() == "Choose context.dev"
    assert "scroll-margin-top:16px" in heading.get("style", "")
    section = heading.find_parent("section")
    example = json.loads(section.select_one("pre code").get_text())
    assert example["engine"] == "context.dev"
    assert "context is not accepted" in section.get_text(" ", strip=True)
    for endpoint in (
        "https://api.trustedrouter.com/v1",
        "https://api-aws.trustedrouter.com/v1",
        "https://api-azure.trustedrouter.com/v1",
    ):
        assert endpoint in section.get_text()
    assert "Other gateways return an unavailable error" not in section.get_text()
    for path in ("/docs", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert "/docs/web-search#context-dev" in client.get(path).text


def test_exa_is_disclosed_as_a_system_subprocessor(client: TestClient) -> None:
    response = client.get("/legal/subprocessors.json")

    assert response.status_code == 200
    exa = next(item for item in response.json()["system_subprocessors"] if item["name"] == "Exa")
    assert "model-generated search query" in exa["data_access"]
    assert "blocked on ZDR, E2E/confidential, and EU" in exa["data_access"]
    assert exa["policy_url"] == "https://exa.ai/privacy"


def test_context_is_disclosed_without_claiming_zdr(client: TestClient) -> None:
    response = client.get("/legal/subprocessors.json")
    assert response.status_code == 200
    context = next(item for item in response.json()["system_subprocessors"] if item["name"] == "context.dev")
    assert "explicitly select" in context["purpose"]
    assert "model-generated search query" in context["data_access"]
    assert "retention and use for model training" in context["data_access"]
    assert "does not claim ZDR" in context["data_access"]
    assert "blocked on ZDR, E2E/confidential, and EU" in context["data_access"]
    assert context["policy_url"] == "https://www.context.dev/privacy"
