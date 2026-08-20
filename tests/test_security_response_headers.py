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
from trusted_router.middleware import CSP_SCRIPT_ORIGINS, content_security_policy


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


def test_csp_ships_report_only_until_the_unseen_paths_are_proven(
    client: TestClient,
) -> None:
    """Report-only FIRST, then enforce.

    The nonces are already correct -- verified in a browser under an enforcing
    policy on /, /choose, /docs and /chat. What was NOT exercised is /console/*
    (needs a session) and the Adyen checkout page, whose third-party SDK
    injects its own iframes. Under enforcement a blocked script does not warn,
    it silently does not run, and checkout is a money path. So real traffic
    proves those two before the header is switched.
    """
    html = client.get("/")
    assert html.headers.get("content-security-policy-report-only")
    assert html.headers.get("content-security-policy") is None

    api = client.get("/status.json")
    assert api.headers.get("content-type", "").startswith("application/json")
    assert api.headers.get("content-security-policy-report-only") is None


def test_script_src_uses_a_nonce_and_not_unsafe_inline(client: TestClient) -> None:
    """A nonce in script-src makes browsers ignore 'unsafe-inline'. Shipping
    both would be the policy that looks strict and blocks nothing."""
    policy = client.get("/").headers["content-security-policy-report-only"]
    script_src = next(d for d in policy.split("; ") if d.startswith("script-src"))
    assert "'nonce-" in script_src
    assert "'unsafe-inline'" not in script_src


def test_the_nonce_is_per_request_and_matches_the_page(client: TestClient) -> None:
    """The header nonce must authorise THIS page's inline scripts, and must
    not repeat: a reused nonce is worth exactly as much as none."""
    import re

    seen = set()
    for _ in range(3):
        response = client.get("/")
        header = re.search(
            r"'nonce-([A-Za-z0-9_-]+)'",
            response.headers["content-security-policy-report-only"],
        )
        assert header, response.headers["content-security-policy-report-only"]
        body = set(re.findall(r'nonce="([A-Za-z0-9_-]+)"', response.text))
        assert body, "page has no nonced inline blocks"
        assert body == {header.group(1)}, "page nonce does not match the header"
        seen.add(header.group(1))
    assert len(seen) == 3, "nonce was reused across requests"


def test_every_inline_script_in_every_template_carries_the_nonce() -> None:
    """The guard that keeps enforcement survivable.

    Under an enforcing policy an inline <script> without a nonce does not warn
    -- it silently does not run, and the page looks fine until somebody clicks
    the thing it powered. A new template must not be able to do that quietly.
    """
    import re
    from pathlib import Path

    import trusted_router

    root = Path(trusted_router.__file__).parent / "templates"
    missing = []
    for path in sorted(root.rglob("*.html")):
        for match in re.finditer(r"<(script|style)\b[^>]*>", path.read_text()):
            tag = match.group(0)
            if re.search(r"\bsrc\s*=", tag) or "nonce=" in tag:
                continue
            missing.append(f"{path.relative_to(root)}: {tag[:60]}")
    assert not missing, (
        "inline blocks without nonce=\"{{ csp_nonce() }}\" -- these will not "
        "execute under the enforcing policy:\n  " + "\n  ".join(missing)
    )


def test_external_script_origins_are_allowlisted() -> None:
    """Every https:// script the templates load must be in CSP_SCRIPT_ORIGINS."""
    import re
    from pathlib import Path

    import trusted_router

    root = Path(trusted_router.__file__).parent / "templates"
    origins = set()
    for path in root.rglob("*.html"):
        for match in re.finditer(r'<script[^>]*src="(https://[^"/]+)', path.read_text()):
            origins.add(match.group(1))
    assert origins <= set(CSP_SCRIPT_ORIGINS), (
        f"templates load scripts from {origins - set(CSP_SCRIPT_ORIGINS)}, "
        "which the policy blocks"
    )


def test_the_policy_still_collects_nothing_automatically() -> None:
    """No report-uri: violations surface as broken pages and console errors,
    not as telemetry. Stated so the header is not mistaken for monitoring."""
    policy = content_security_policy("abc")
    assert "report-uri" not in policy
    assert "report-to" not in policy
    for directive in ("frame-ancestors 'self'", "object-src 'none'", "base-uri 'self'"):
        assert directive in policy
