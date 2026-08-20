"""The headers an outside reviewer checks first, and one they cannot yet.

Prompted by a public comment claiming "basic security holes" with no
specifics. What was actually missing, verified against production on
2026-08-20: HSTS and the cookie flags were correct; CSP, X-Frame-Options,
X-Content-Type-Options and Referrer-Policy were absent on every path,
including /docs and the /console flow where people paste API keys.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.main import create_app
from trusted_router.middleware import CONTENT_SECURITY_POLICY_REPORT_ONLY


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "header,value",
    [
        ("strict-transport-security", "max-age=63072000; includeSubDomains"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "strict-origin-when-cross-origin"),
        ("x-frame-options", "SAMEORIGIN"),
    ],
)
def test_every_response_carries_the_baseline_headers(
    client: TestClient, header: str, value: str
) -> None:
    """Including JSON: nosniff on an API error costs nothing and a missing
    header is judged on whichever path the reviewer happened to fetch."""
    for path in ("/", "/docs", "/status.json"):
        response = client.get(path)
        assert response.headers.get(header) == value, f"{path} missing {header}"


def test_x_frame_options_is_sameorigin_not_deny() -> None:
    """DENY would blank the model picker.

    /choose embeds /static/choose-app.html in a SAME-ORIGIN iframe, so DENY
    breaks a live page while buying nothing: the clickjacking risk is a THIRD
    party framing a page where somebody is creating an API key, and SAMEORIGIN
    already blocks that. This test exists because DENY is the reflexive choice
    and it is wrong here.
    """
    from trusted_router import middleware

    source = (
        __import__("pathlib").Path(middleware.__file__).read_text()
    )
    assert '"x-frame-options", "SAMEORIGIN"' in source
    assert '"x-frame-options", "DENY"' not in source


def test_csp_is_report_only_and_only_on_html(client: TestClient) -> None:
    """Report-only, and NOT enforcing: the templates carry inline script and
    style, so enforcing today would either need 'unsafe-inline' (pointless) or
    nonces in every template (the real fix, and a separate change)."""
    html = client.get("/")
    assert html.headers.get("content-security-policy-report-only")
    assert "content-security-policy" not in {k.lower() for k in html.headers} - {
        "content-security-policy-report-only"
    }

    api = client.get("/status.json")
    assert api.headers.get("content-type", "").startswith("application/json")
    assert api.headers.get("content-security-policy-report-only") is None


def test_the_policy_allows_what_the_templates_actually_load() -> None:
    """A policy copied from a blog post reports violations nobody acts on.

    These origins are the ones the templates reference today; if a template
    starts loading from somewhere else, the violation should be a real signal
    rather than noise this policy was always going to produce.
    """
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in CONTENT_SECURITY_POLICY_REPORT_ONLY
    assert "https://cdn.jsdelivr.net" in CONTENT_SECURITY_POLICY_REPORT_ONLY


def test_report_only_collects_nothing_automatically() -> None:
    """No report-uri/report-to, stated so the header is not mistaken for
    monitoring. Violations land in the browser console and nowhere else until
    somebody looks -- which is the point of shipping it before enforcing."""
    assert "report-uri" not in CONTENT_SECURITY_POLICY_REPORT_ONLY
    assert "report-to" not in CONTENT_SECURITY_POLICY_REPORT_ONLY
