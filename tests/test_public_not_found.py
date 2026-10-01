from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.dashboard import public_not_found_html


@pytest.mark.parametrize(
    ("path", "canonical_path"),
    [
        ("//backend/.env", "/backend/.env"),
        ("///external.example/missing", "/external.example/missing"),
        ("/missing%3Fpage", "/missing%3Fpage"),
        ("/missing%23section", "/missing%23section"),
    ],
)
def test_untrusted_public_path_remains_a_404(
    client: TestClient, path: str, canonical_path: str, test_settings: Settings
) -> None:
    # An absolute request URL preserves the double slash from the production incident.
    response = client.get(
        f"http://testserver{path}?private-query=do-not-publish",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "That page is not here." in response.text
    assert (
        f'<link rel="canonical" href="https://{test_settings.trusted_domain}{canonical_path}">'
        in response.text
    )
    assert "do-not-publish" not in response.text
    assert '<meta name="robots" content="noindex,follow">' in response.text


@pytest.mark.parametrize("path", ["missing", "/missing"])
def test_not_found_preserves_ordinary_canonical_path(test_settings: Settings, path: str) -> None:
    html = public_not_found_html(test_settings, path)

    assert f'<link rel="canonical" href="https://{test_settings.trusted_domain}/missing">' in html


def test_unknown_public_browser_route_uses_styled_not_found(client: TestClient) -> None:
    response = client.get(
        "/this-page-does-not-exist",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "That page is not here." in response.text
    assert '<meta name="robots" content="noindex,follow">' in response.text
    for path in ("/docs", "/models", "/status", "/support"):
        assert f'href="{path}"' in response.text


def test_unknown_api_route_keeps_json_even_for_browser_accept(client: TestClient) -> None:
    response = client.get(
        "/v1/this-route-does-not-exist",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["type"] == "http_error"


def test_unknown_public_non_browser_request_keeps_json(client: TestClient) -> None:
    response = client.get(
        "/this-page-does-not-exist",
        headers={"accept": "application/json"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_missing_blog_post_uses_styled_not_found(client: TestClient) -> None:
    response = client.get("/blog/not-a-real-post", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert "That page is not here." in response.text
    assert "Quickstart: one base_url change" not in response.text


def test_missing_blog_post_gives_an_agent_markdown(client: TestClient) -> None:
    """The blog route used to return HTML directly for every client.

    It now raises, so the central handler negotiates it like any other 404 and
    a non-browser gets the recoverable body instead of a styled page it has to
    parse. `*/*` is the case that matters: it is what curl and most SDKs send.
    """
    response = client.get("/blog/not-a-real-post", headers={"accept": "*/*"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/markdown")
    assert "/llms.txt" in response.text


def test_agent_404_names_the_machine_readable_indexes(client: TestClient) -> None:
    """The recovery path is the whole point of the body.

    A 404 that says only "not found" leaves an agent guessing more paths. Each
    of these is a real, published entry point, so the assertion is on all of
    them rather than on the body being non-empty.
    """
    response = client.get("/some-path-that-does-not-exist", headers={"accept": "*/*"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["vary"].lower().startswith("accept")
    for pointer in ("/llms.txt", "/sitemap.xml", "/docs", "/openapi.json", "/v1/models"):
        assert pointer in response.text, pointer
    assert response.text.startswith("# 404 Not Found")


def test_agent_404_fires_with_no_accept_header_at_all(client: TestClient) -> None:
    response = client.get("/nope", headers={"accept": ""})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/markdown")


def test_explicitly_requested_json_404_is_still_json(client: TestClient) -> None:
    """A caller that named application/json asked for a format, and gets it.

    This is the line between "no preference" and "a preference this change
    would be overriding". Existing integrations that parse the error envelope
    are on this side of it.
    """
    response = client.get("/this-page-does-not-exist", headers={"accept": "application/json"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == 404
