"""Developer resources are findable by name, at the URLs agents try first.

An audit agent searching for "TrustedRouter developer resources" found nothing
relevant. Page titles already carried the product name; what was missing was
the machine-readable half: openapi.json appeared nowhere in llms.txt, and the
conventional root-level index paths returned 404 while the real documents sat
under /docs, which is discoverable only once you have already found the index
you were looking for.

These assert on the CONTENT of the published documents rather than on internal
helpers, because the failure being guarded is "an agent fetched the published
URL and could not find X".
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.dashboard import llms_txt


@pytest.mark.parametrize(
    "path",
    ["/llms.txt", "/llms-full.txt", "/.well-known/llms.txt", "/sitemap.xml", "/openapi.json"],
)
def test_conventional_entry_points_resolve(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200, path
    assert response.content, path


def test_api_alias_redirects_to_the_docs(client: TestClient) -> None:
    """/api is the first path an agent guesses for API docs, and it 404'd."""
    response = client.get("/api", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/docs"


def test_llms_txt_names_every_developer_resource() -> None:
    """openapi.json was absent from llms.txt entirely, so the index pointed at
    documentation while omitting the machine-readable contract."""
    text = llms_txt(Settings(environment="test"))

    assert "## Developer Resources" in text
    for resource in (
        "/openapi.json",
        "/docs",
        "/docs/mcp",
        "/docs/agent-setup",
        "/llms-full.txt",
        "/sitemap.xml",
    ):
        assert resource in text, resource


def test_developer_resources_are_named_with_the_product() -> None:
    """The audit query was name-based ("trustedrouter" + resource), so the
    resource lines have to carry the product name, not just the URL."""
    text = llms_txt(Settings(environment="test"))
    section = text.split("## Developer Resources", 1)[1].split("\n##", 1)[0]

    for phrase in (
        "TrustedRouter API documentation",
        "TrustedRouter OpenAPI specification",
        "TrustedRouter MCP server",
    ):
        assert phrase in section, phrase


def test_openapi_document_describes_itself(client: TestClient) -> None:
    """Fetched by name the spec arrives with no surrounding page, so the info
    block is the only place it can say what it is or where to call."""
    document = json.loads(client.get("/openapi.json").content)

    info = document["info"]
    assert info["title"] == "TrustedRouter API"
    assert "OpenAI-compatible" in info["description"]
    assert "api.trustedrouter.com/v1" in info["description"]
    assert [server["url"] for server in document["servers"]][0] == (
        "https://api.trustedrouter.com/v1"
    )


def test_the_shipped_public_spec_links_back_to_the_docs() -> None:
    """`externalDocs` has no FastAPI app-level parameter, so it is added by
    scripts/generate_public_openapi.py to the pre-serialized asset. That asset,
    not the dynamic schema, is what the public surface serves."""
    asset = pathlib.Path("src/trusted_router/static/openapi-public.json")
    document = json.loads(asset.read_text())

    assert document["externalDocs"]["url"] == "https://trustedrouter.com/docs"
    assert document["info"]["title"] == "TrustedRouter API"
    assert document["servers"][0]["url"] == "https://api.trustedrouter.com/v1"


def test_llms_full_matches_the_docs_variant(client: TestClient) -> None:
    """The root alias must serve the same document, not a second copy that can
    drift from it."""
    root = client.get("/llms-full.txt").text
    docs = client.get("/docs/llms-full.txt").text

    assert root == docs
    assert len(root) > 1000


def test_llms_txt_interactive_reference_link_opens_the_reference() -> None:
    """It pointed at /docs, so an agent following it never reached the
    endpoint list."""
    text = llms_txt(Settings(environment="test"))

    assert "[TrustedRouter interactive API reference](https://trustedrouter.com/api/reference)" in text


def test_llms_txt_primary_links_are_not_repeated() -> None:
    text = llms_txt(Settings(environment="test"))
    section = text.split("## Primary Links", 1)[1].split("\n##", 1)[0]
    links = [line for line in section.splitlines() if line.startswith("- [")]

    assert len(links) == len(set(links))


def test_docs_page_has_the_authentication_anchor_and_spec_link(client: TestClient) -> None:
    """llms.txt links /docs#authentication, and agents look for an OpenAPI
    service description (RFC 8631) before guessing /openapi.json. The link is
    absolute because observer hosts render these pages without serving the
    spec."""
    html = client.get("/docs").text

    assert 'id="authentication"' in html
    assert (
        '<link rel="service-desc" type="application/json" '
        'href="https://trustedrouter.com/openapi.json"'
    ) in html


def test_homepage_links_the_openapi_service_description(client: TestClient) -> None:
    html = client.get("/").text

    assert 'rel="service-desc" type="application/json" href="https://trustedrouter.com/openapi.json"' in html
