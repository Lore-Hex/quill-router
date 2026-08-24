"""Discovery surfaces an assistant needs when handed only the domain.

Each of these existed as prose on a marketing page and nowhere a machine could
read it. That is the same gap openapi.json had: published, documented, and
unfindable without first reading a page written for humans.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.dashboard import llms_txt
from trusted_router.domains import configured_control_domains
from trusted_router.main import create_app
from trusted_router.mcp_metadata import (
    MCP_SERVER_DESCRIPTION,
    MCP_SERVER_NAME,
    MCP_SERVER_TITLE,
)
from trusted_router.routes.oauth_keys import PKCE_METHODS

PAGES_WITH_STRUCTURED_DATA = ["/", "/trust", "/docs", "/models", "/legal", "/support"]
MCP_DISCOVERY_PATHS = (
    "/.well-known/mcp.json",
    "/.well-known/mcp/server-card.json",
)
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "trusted_router" / "static"


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
    responses = [client.get(path) for path in MCP_DISCOVERY_PATHS]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].content == responses[1].content
    document = responses[0].json()

    assert document["url"].endswith("/mcp")
    assert document["transport"] == "http"
    assert document["authentication"]["scheme"] == "bearer"
    assert document["protocolVersion"]
    assert document["name"] == MCP_SERVER_NAME
    assert document["title"] == MCP_SERVER_TITLE
    assert document["description"] == MCP_SERVER_DESCRIPTION


def test_the_mcp_discovery_icon_is_a_served_repository_asset(
    client: TestClient,
) -> None:
    document = client.get(MCP_DISCOVERY_PATHS[0]).json()
    icon = urlsplit(document["iconUrl"])

    assert icon.scheme == "https"
    assert icon.netloc == "trustedrouter.com"
    assert icon.path == "/static/favicon.svg"
    assert (STATIC_DIR / icon.path.rsplit("/", 1)[-1]).is_file()

    response = client.get(icon.path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("trustedrouter.com", "trustedrouter.com"),
        ("allyrouter.com", "allyrouter.com"),
        ("www.uptimerouter.com", "uptimerouter.com"),
        ("attacker.example", "trustedrouter.com"),
    ],
)
@pytest.mark.parametrize("path", MCP_DISCOVERY_PATHS)
def test_mcp_discovery_urls_use_the_request_canonical_domain(
    client: TestClient,
    path: str,
    host: str,
    domain: str,
) -> None:
    document = client.get(path, headers={"host": host}).json()

    assert document["url"] == f"https://{domain}/mcp"
    assert document["iconUrl"] == f"https://{domain}/static/favicon.svg"
    assert document["authentication"]["tokenUrl"] == f"https://{domain}/console/keys"
    assert document["documentation"] == f"https://{domain}/docs/mcp"


def test_the_discovery_document_matches_the_server_it_describes(
    client: TestClient,
) -> None:
    """A discovery document that drifts from the server is worse than none: it
    sends a client to a protocol version the server will not speak."""
    from trusted_router.routes.mcp import MCP_PROTOCOL_VERSION

    document = client.get("/.well-known/mcp.json").json()

    assert document["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert document["name"] == MCP_SERVER_NAME
    assert document["title"] == MCP_SERVER_TITLE


def test_oauth_authorization_server_metadata_resolves_to_mounted_routes(
    client: TestClient,
) -> None:
    response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    document = json.loads(response.text)
    assert document == response.json()

    advertised_routes = {
        "issuer": "GET",
        "authorization_endpoint": "GET",
        "token_endpoint": "POST",
        "service_documentation": "GET",
    }
    for field, method in advertised_routes.items():
        parsed = urlsplit(document[field])
        assert parsed.scheme == "https", field
        assert parsed.netloc == "trustedrouter.com", field
        path = parsed.path or "/"
        assert any(
            route.path == path and method in (route.methods or set())
            for route in client.app.routes
            if isinstance(route, APIRoute)
        ), field

    assert document["response_types_supported"] == ["code"]
    assert document["grant_types_supported"] == ["authorization_code"]
    assert document["token_endpoint_auth_methods_supported"] == ["none"]


def test_oauth_metadata_pkce_methods_match_the_exchange_exactly(client: TestClient) -> None:
    document = client.get("/.well-known/oauth-authorization-server").json()

    assert document["code_challenge_methods_supported"] == sorted(PKCE_METHODS)


def test_oauth_metadata_omits_scopes_because_keys_are_not_scoped(client: TestClient) -> None:
    document = client.get("/.well-known/oauth-authorization-server").json()

    assert "scopes_supported" not in document


@pytest.mark.parametrize(
    "domain",
    configured_control_domains(Settings(environment="test")),
)
def test_oauth_metadata_uses_each_first_party_request_domain(
    client: TestClient,
    domain: str,
) -> None:
    document = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"host": domain},
    ).json()

    origin = f"https://{domain}"
    assert document["issuer"] == origin
    for field in ("authorization_endpoint", "token_endpoint", "service_documentation"):
        assert document[field].startswith(f"{origin}/"), field


def test_oauth_metadata_is_mounted_on_the_public_surface() -> None:
    public_app = create_app(
        Settings(environment="test", service_surface="public"),
        configure_store_arg=False,
        init_observability=False,
    )
    paths = {route.path for route in public_app.routes}

    assert "/.well-known/oauth-authorization-server" in paths
    response = TestClient(public_app).get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200


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
