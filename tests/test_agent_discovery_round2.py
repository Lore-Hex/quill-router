"""Discovery surfaces an assistant needs when handed only the domain.

Each of these existed as prose on a marketing page and nowhere a machine could
read it. That is the same gap openapi.json had: published, documented, and
unfindable without first reading a page written for humans.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.dashboard import llms_txt

PAGES_WITH_STRUCTURED_DATA = ["/", "/trust", "/docs", "/models", "/legal", "/support"]


def _organization(html: str) -> dict | None:
    for blob in re.findall(r"application/ld\+json[^>]*>(.*?)</script>", html, re.S):
        try:
            graph = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for node in graph.get("@graph", [graph]):
            if node.get("@type") == "Organization" and "address" in node:
                return node
    return None


@pytest.mark.parametrize("path", PAGES_WITH_STRUCTURED_DATA)
def test_every_key_page_identifies_the_operating_company(client: TestClient, path: str) -> None:
    """Not just the homepage. An assistant asked who runs this has no reason to
    have landed on whichever page somebody remembered to annotate."""
    node = _organization(client.get(path, headers={"accept": "text/html"}).text)

    assert node is not None, path
    assert node["legalName"] == "Lore Hex Corp"


def test_the_organization_node_answers_a_contact_query(client: TestClient) -> None:
    node = _organization(client.get("/", headers={"accept": "text/html"}).text)
    assert node is not None

    address = node["address"]
    assert address["@type"] == "PostalAddress"
    assert address["streetAddress"] == "1111 Brickell Ave, Floor 10"
    assert address["addressLocality"] == "Miami"
    assert address["addressRegion"] == "FL"
    assert address["postalCode"] == "33131"
    assert address["addressCountry"] == "US"

    contact_types = {point["contactType"] for point in node["contactPoint"]}
    assert {"customer support", "security", "sales"} <= contact_types
    assert any(point.get("telephone") for point in node["contactPoint"])
    assert all(point.get("email") for point in node["contactPoint"])


def test_the_organization_node_carries_verifiable_identifiers(
    client: TestClient,
) -> None:
    """EIN and DUNS are the fields a procurement check actually looks up, and
    they were human-readable on /legal only."""
    node = _organization(client.get("/legal", headers={"accept": "text/html"}).text)

    assert node is not None
    assert node["taxID"] == "41-5339728"
    assert node["duns"] == "144992055"


def test_the_mcp_server_is_discoverable_without_reading_a_page(
    client: TestClient,
) -> None:
    """Everything needed to connect was prose on /docs/mcp."""
    document = client.get("/.well-known/mcp.json").json()

    assert document["url"].endswith("/mcp")
    assert document["transport"] == "http"
    assert document["authentication"]["scheme"] == "bearer"
    assert document["protocolVersion"]


def test_the_discovery_document_matches_the_server_it_describes(
    client: TestClient,
) -> None:
    """A discovery document that drifts from the server is worse than none: it
    sends a client to a protocol version the server will not speak."""
    from trusted_router.routes.mcp import MCP_PROTOCOL_VERSION

    document = client.get("/.well-known/mcp.json").json()

    assert document["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert document["name"] == "trustedrouter"


def test_llms_txt_is_a_navigation_index() -> None:
    """The convention asks for a heading, markdown links to deeper resources,
    and under 30,000 characters. It was 7.8k of bare `Label: https://url`
    bullets -- readable, but not a link graph anything can traverse."""
    text = llms_txt(Settings(environment="test"))

    assert text.startswith("# TrustedRouter")
    assert len(text) < 30_000
    assert len(re.findall(r"\]\(https?://", text)) > 40
    bare = re.findall(r"^- [^\[\n]*: https?://", text, re.M)
    assert not bare, bare[:3]


def test_llms_txt_points_at_the_long_form_index() -> None:
    """Long-form content lives in /llms-full.txt; the index has to link it or
    the split just hides it."""
    text = llms_txt(Settings(environment="test"))

    assert "/llms-full.txt" in text


def test_every_sdk_is_linked_from_the_docs_page(client: TestClient) -> None:
    """Three of six SDKs were listed. Go, Rust and Java shipped and were
    reachable only by guessing the repository name."""
    html = client.get("/docs", headers={"accept": "text/html"}).text

    for repo in (
        "trusted-router-py",
        "trusted-router-js",
        "trusted-router-go",
        "trusted-router-rust",
        "trusted-router-swift",
        "trusted-router-java",
    ):
        assert f"github.com/Lore-Hex/{repo}" in html, repo


def test_no_sdk_link_points_at_a_moved_repository(client: TestClient) -> None:
    """The Swift card pointed at github.com/jperla/..., which 301s to the
    Lore-Hex org. A redirect is not a broken link, but it is a stale one, and
    it is the sort that rots into a 404."""
    html = client.get("/docs", headers={"accept": "text/html"}).text

    assert "github.com/jperla/" not in html
