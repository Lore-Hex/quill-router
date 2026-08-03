from __future__ import annotations

from fastapi.testclient import TestClient


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
    response = client.get("/blog/not-a-real-post")

    assert response.status_code == 404
    assert "That page is not here." in response.text
    assert "Quickstart: one base_url change" not in response.text
