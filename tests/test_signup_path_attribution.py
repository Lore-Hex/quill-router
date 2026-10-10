"""The page a sign-up started on travels as a path only, and exports safely."""
from __future__ import annotations

import pytest
from starlette.requests import Request

from scripts.axiom_growth import model as m
from scripts.axiom_growth.runtime import project
from trusted_router.acquisition import same_site_referer_path


def _request(referer: str | None) -> Request:
    headers = [(b"referer", referer.encode())] if referer else []
    return Request({
        "type": "http", "method": "GET", "scheme": "http", "http_version": "1.1",
        "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
        "path": "/auth/google/login", "raw_path": b"/auth/google/login",
        "root_path": "", "query_string": b"", "headers": headers,
    })


@pytest.mark.parametrize(("referer", "expected"), [
    ("http://testserver/pricing?plan=team#compare", "/pricing"),
    ("http://testserver/", "/"),
    ("http://TESTSERVER/docs/mcp", "/docs/mcp"),
    ("https://evil.example/pricing", None),
    ("http://testserver//evil", None),
    ("not a url", None),
    (None, None),
])
def test_same_site_referer_path_keeps_only_the_same_site_path(
    referer: str | None, expected: str | None,
) -> None:
    assert same_site_referer_path(_request(referer)) == expected


def _signup_row(signup_path: str) -> dict[str, object]:
    return project({
        "_time": "2026-10-10T12:00:00.000000000Z",
        "event": "acquisition.signup_completed",
        "anonymous_fingerprint": "a" * 64,
        "utm_source": "direct",
        "signup_path": signup_path,
    }, "axiom")


def test_signup_path_is_exported_like_the_other_paths() -> None:
    assert "signup_path" in m.FIELDS
    assert _signup_row("/Pricing/?plan=team")["signup_path"] == "/pricing"
    assert _signup_row("/console/api-keys")["signup_path"] == "(account flow)"
