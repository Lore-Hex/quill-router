from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.routes import billing, oauth
from trusted_router.schemas import CheckoutRequest
from trusted_router.storage import InMemoryStore


@pytest.mark.parametrize("group_by", ["model", "none"])
@pytest.mark.parametrize("date", ["2026-10-20'", "2026-02-30", "20261020", "", "2026-10-20T00:00:00Z"])
def test_invalid_activity_date_never_reaches_storage(
    client: TestClient, user_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    group_by: str, date: str,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid date reached storage")

    monkeypatch.setattr(InMemoryStore, "activity_result", unexpected)
    monkeypatch.setattr(InMemoryStore, "activity_events_result", unexpected)
    response = client.get(
        "/v1/activity", headers=user_headers, params={"date": date, "group_by": group_by},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["error"]["message"]


@pytest.mark.parametrize("date", [None, "2024-02-29", "2026-10-20"])
def test_valid_activity_dates_still_work(
    client: TestClient, user_headers: dict[str, str], date: str | None,
) -> None:
    response = client.get(
        "/v1/activity", headers=user_headers, params={"date": date} if date else {},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("field", ["success_url", "cancel_url"])
@pytest.mark.parametrize("url", ["not a URL", "/billing", "//evil.example", "javascript:alert(1)",
                                      "https:example.com", "https:///example.com", "https:////example.com",
                                      "https://", "https://example.com:bad", "https://user:pass@example.com",
                                      "https://example.com/\r\nheader", "https://bad host.example/"])
def test_invalid_checkout_urls_rejected_before_provider(
    client: TestClient, user_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    field: str, url: str,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid checkout URL reached provider")

    monkeypatch.setattr(billing, "create_checkout_session", unexpected)
    response = client.post(
        "/v1/billing/checkout", headers=user_headers, json={"amount": 25, field: url},
    )
    assert response.status_code == 400, response.text


def test_checkout_url_preserves_stripe_template_and_query() -> None:
    url = "https://trustedrouter.com/billing/success?session_id={CHECKOUT_SESSION_ID}&next=%2Fconsole"
    body = CheckoutRequest(success_url=url, cancel_url="http://localhost:8000/billing")
    assert body.success_url == url
    assert body.cancel_url == "http://localhost:8000/billing"
    assert CheckoutRequest(success_url="").success_url is None


def test_invalid_portal_url_rejected_before_provider(
    client: TestClient, user_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid portal URL reached provider")

    monkeypatch.setattr(billing, "create_billing_portal_session", unexpected)
    response = client.post(
        "/v1/billing/portal", headers=user_headers, json={"return_url": "not a URL"},
    )
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("path", ["/\ufffd\ufffd\ufffd\ufffdevil.example", "/\u4e2d\u6587?name=\u00e9", "/console?next=%2Fauth"])
@pytest.mark.parametrize("kind", ["next", "from"])
def test_oauth_path_cookies_are_ascii_and_remain_local(path: str, kind: str) -> None:
    response = Response()
    setter = oauth._set_next_cookie if kind == "next" else oauth._set_from_cookie
    getter = oauth._next_target if kind == "next" else oauth._from_target
    setter(response, path, Settings(environment="test"))
    cookie = response.headers["set-cookie"]
    cookie.encode("ascii")
    request = Request({"type": "http", "headers": [(b"cookie", cookie.split(";")[0].encode("ascii"))]})
    expected = quote(path, safe=":/%#?=@[]!$&'()*+,;")
    assert getter(request) == expected
    assert expected.startswith("/") and not expected.startswith("//")


@pytest.mark.parametrize("path", ["//evil.example", "/\\evil.example", "/\nevil.example", "/\tevil.example"])
def test_oauth_rejects_ambiguous_redirects(path: str) -> None:
    assert oauth._safe_next_path(path) is None


def test_oauth_cookie_limit_applies_after_unicode_encoding() -> None:
    assert oauth._safe_next_path("/" + "\u4e2d" * 228) is None
    assert oauth._safe_next_path("/" + "\u4e2d" * 227) is not None
