from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts.deploy.service_surface_url_map import route_surface
from trusted_router.config import Settings
from trusted_router.dashboard import public_not_found_html, public_not_found_markdown

_NOT_FOUND_BODY = (
    b'{"error":{"code":404,"message":"Not Found","type":"http_error","source":"router"}}'
)
_REFERENCE_ALIASES = (
    "/api/docs", "/docs/api", "/docs/api-reference", "/api-docs",
    "/api-reference", "/reference", "/swagger", "/docs/reference",
)
_SPEC_ALIASES = ("/docs/openapi", "/openapi.yaml")


@pytest.mark.parametrize("path", _REFERENCE_ALIASES + _SPEC_ALIASES)
@pytest.mark.parametrize("suffix", ["", "/"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_documentation_alias_redirects_directly(
    client: TestClient, path: str, suffix: str, method: str
) -> None:
    response = client.request(method, path + suffix, follow_redirects=False)

    assert response.status_code == 301
    target = "/api/reference" if path in _REFERENCE_ALIASES else "/openapi.json"
    assert response.headers["location"] == target
    assert route_surface(path + suffix) == "public"
    if method == "HEAD":
        assert response.content == b""


@pytest.mark.parametrize("path", ["/api/reference", "/openapi.json"])
def test_documentation_redirect_target_answers_successfully(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 200
    assert response.content


@pytest.mark.parametrize("path", ["/redoc", "/docs/oauth2-redirect"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_existing_reference_redirect_is_unchanged(
    client: TestClient, path: str, method: str
) -> None:
    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/api/reference"


def test_documentation_aliases_are_absent_from_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    for base in _REFERENCE_ALIASES + _SPEC_ALIASES:
        for path in (base, base + "/"):
            assert path not in paths


@pytest.mark.parametrize("path", ["/v1/missing-documentation", "/api/missing-documentation"])
@pytest.mark.parametrize("accept", [None, "*/*", "application/json", "text/html", "text/markdown"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_unknown_api_paths_keep_exact_error_body(
    client: TestClient, path: str, accept: str | None, method: str
) -> None:
    request = client.build_request(method, path)
    request.headers.pop("accept", None)
    if accept is not None:
        request.headers["accept"] = accept
    response = client.send(request)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-length"] == str(len(_NOT_FOUND_BODY))
    assert response.content == (_NOT_FOUND_BODY if method == "GET" else b"")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("accept", [None, "*/*", "text/markdown", "text/html", "application/json"])
def test_unknown_public_path_keeps_current_representation(
    client: TestClient, test_settings: Settings, method: str, accept: str | None
) -> None:
    path = "/missing-documentation"
    request = client.build_request(method, path, headers={"accept-encoding": "identity"})
    request.headers.pop("accept", None)
    if accept is not None:
        request.headers["accept"] = accept
    response = client.send(request)

    if accept == "application/json":
        expected = _NOT_FOUND_BODY
        content_type = "application/json"
    elif accept == "text/html":
        policy = response.headers["content-security-policy-report-only"]
        nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
        expected = public_not_found_html(test_settings, path).replace(
            'nonce=""', f'nonce="{nonce}"'
        ).encode()
        content_type = "text/html"
    else:
        expected = public_not_found_markdown(test_settings, path).encode()
        content_type = "text/markdown"
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-length"] == str(len(expected))
    assert response.content == (expected if method == "GET" else b"")
