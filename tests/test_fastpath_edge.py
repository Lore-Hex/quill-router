"""The control plane's side of the fast path's edge (fastpath/internal/edge;
docs/design/fast-admission-enclave-direct.md, E1): the accepted image
digests it reads, and the comparison of its boot verdict with the route's."""

from __future__ import annotations

import logging

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway

TOKEN = "edge-internal-token"  # noqa: S105 - a test fixture, not a credential


def _client(**overrides: object) -> TestClient:
    options: dict[str, object] = {
        "environment": "test",
        "internal_gateway_token": TOKEN,
        "spend_lease_accepted_gcp_image_digests": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    options.update(overrides)
    return TestClient(create_app(Settings(**options), init_observability=False))


def test_the_accepted_digests_are_served_to_the_edge_under_the_internal_token() -> None:
    client = _client()
    resp = client.get("/internal/gateway/stage-d/accepted-digests", headers={"x-trustedrouter-internal-token": TOKEN})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"image_digests": ["sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]}


def test_the_accepted_digests_need_the_internal_token() -> None:
    client = _client()
    assert client.get("/internal/gateway/stage-d/accepted-digests").status_code == 401
    assert client.get(
        "/internal/gateway/stage-d/accepted-digests", headers={"x-trustedrouter-internal-token": "other"},
    ).status_code == 401


def _request(verdict: str | None) -> Request:
    headers = [(b"x-tr-edge-boot", verdict.encode())] if verdict is not None else []
    return Request({"type": "http", "method": "POST", "path": "/internal/gateway/authorize", "headers": headers})


@pytest.mark.parametrize(
    ("verdict", "verified", "differs"),
    [
        ("verified", True, False),
        ("verified", False, True),
        ("invalid", False, False),
        ("invalid", True, True),
        ("not_accepted", True, True),
        ("not_accepted", False, False),
        ("unread", True, False),
        ("unread", False, False),
        ("unchecked", False, False),
        (None, True, False),
    ],
)
def test_the_edges_verdict_is_compared_with_the_routes(
    caplog: pytest.LogCaptureFixture, verdict: str | None, verified: bool, differs: bool,
) -> None:
    with caplog.at_level(logging.WARNING, logger=gateway.logger.name):
        gateway._compare_edge_boot_verdict(_request(verdict), "boot-1", verified)
    lines = [r.getMessage() for r in caplog.records if "edge boot verdict differs" in r.getMessage()]
    assert bool(lines) == differs, lines
    if differs:
        assert f"edge={verdict}" in lines[0] and "kid=boot-1" in lines[0]
