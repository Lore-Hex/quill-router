"""The control plane's side of the fast path's edge (fastpath/internal/edge;
docs/design/fast-admission-enclave-direct.md, E1): the signature-only check
the edge's verdict is compared with, and the comparison itself."""

from __future__ import annotations

import logging

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Request

from trusted_router.gateway_boot import (
    BootAuthHeader,
    boot_auth_digest,
    verify_boot_auth,
    verify_boot_signature,
)
from trusted_router.receipt_keys import b64url_encode
from trusted_router.routes.internal import gateway
from trusted_router.storage_models import GatewayBoot

PATH = "/internal/gateway/authorize"
BODY = b'{"api_key_hash":"k","model":"m"}'


def _boot(kid: str = "boot-1", *, verified: bool = True) -> tuple[Ed25519PrivateKey, GatewayBoot]:
    private = Ed25519PrivateKey.generate()
    return private, GatewayBoot(
        kid=kid,
        jwk={"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(private.public_key().public_bytes_raw())},
        approved=True,
        verified=verified,
        image_digest="sha256:" + "a" * 64,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-10-18T00:00:00Z",
    )


def _auth(private: Ed25519PrivateKey, kid: str, method: str = "POST", path: str = PATH, body: bytes = BODY) -> BootAuthHeader:
    return BootAuthHeader(kid=kid, signature=b64url_encode(private.sign(boot_auth_digest(method, path, body))))


def test_the_signature_check_asks_the_signature_alone() -> None:
    """A body naming the key by hash alone fails verify_boot_auth's binding
    and a removed digest fails its policy; the signature-only check, the
    edge's question, holds on both, and fails as the edge fails."""
    private, boot = _boot()
    auth = _auth(private, boot.kid)
    assert verify_boot_signature(boot=boot, auth=auth, method="POST", path=PATH, exact_body_bytes=BODY)
    assert not verify_boot_auth(
        boot=boot, auth=auth, method="POST", path=PATH, exact_body_bytes=BODY,
        signed_lookup_hash=None, resolved_lookup_hash="lookup-1", accepted_image_digests=(),
    )
    # The edge's failures are the same failures here.
    assert not verify_boot_signature(boot=boot, auth=auth, method="GET", path=PATH, exact_body_bytes=BODY)
    assert not verify_boot_signature(boot=boot, auth=auth, method="POST", path="/internal/gateway/heartbeat", exact_body_bytes=BODY)
    assert not verify_boot_signature(boot=boot, auth=auth, method="POST", path=PATH, exact_body_bytes=BODY + b" ")
    assert not verify_boot_signature(boot=None, auth=auth, method="POST", path=PATH, exact_body_bytes=BODY)
    other, _ = _boot("boot-2")
    assert not verify_boot_signature(boot=boot, auth=_auth(other, boot.kid), method="POST", path=PATH, exact_body_bytes=BODY)
    _unverified_private, unverified = _boot("boot-3", verified=False)
    assert not verify_boot_signature(
        boot=unverified, auth=_auth(_unverified_private, "boot-3"), method="POST", path=PATH, exact_body_bytes=BODY,
    )
    assert not verify_boot_signature(boot=boot, auth=_auth(private, "boot-9"), method="POST", path=PATH, exact_body_bytes=BODY)


def _request(verdict: str | None) -> Request:
    headers = [(b"x-tr-edge-boot", verdict.encode())] if verdict is not None else []
    return Request({"type": "http", "method": "POST", "path": PATH, "headers": headers})


@pytest.mark.parametrize(
    ("verdict", "signed", "differs"),
    [
        ("verified", True, False),
        ("verified", False, True),
        ("invalid", False, False),
        ("invalid", True, True),
        ("unread", True, False),
        ("unread", False, False),
        ("unchecked", False, False),
        ("absent", False, False),
        (None, True, False),
    ],
)
def test_the_edges_verdict_is_compared_with_the_routes(
    caplog: pytest.LogCaptureFixture, verdict: str | None, signed: bool, differs: bool,
) -> None:
    with caplog.at_level(logging.WARNING, logger=gateway.logger.name):
        gateway._compare_edge_boot_verdict(_request(verdict), "boot-1", signed)
    lines = [r.getMessage() for r in caplog.records if "edge boot verdict differs" in r.getMessage()]
    assert bool(lines) == differs, lines
    if differs:
        assert f"edge={verdict}" in lines[0] and "kid=boot-1" in lines[0]


@pytest.mark.parametrize(
    ("verdict", "signed", "result", "logged_edge"),
    [
        ("verified", True, "agree", "verified"),
        ("invalid", False, "agree", "invalid"),
        ("verified", False, "differ", "verified"),
        ("invalid", True, "differ", "invalid"),
        ("unread", True, "skipped", "unread"),
        ("unchecked", False, "skipped", "unchecked"),
        ("absent", False, "skipped", "absent"),
        ("forged\nvalue", True, "skipped", "other"),
        (None, True, None, None),
    ],
)
def test_every_comparison_of_an_edge_verdict_is_counted(
    caplog: pytest.LogCaptureFixture, verdict: str | None, signed: bool, result: str | None, logged_edge: str | None,
) -> None:
    """Agreement, disagreement and a skip are each one counted line, so a
    run with no disagreements can be told from a run with no comparisons;
    a call without the verdict header is not counted, and a header value
    outside the edge's verdicts is never logged as given."""
    with caplog.at_level(logging.INFO, logger=gateway.logger.name):
        gateway._compare_edge_boot_verdict(_request(verdict), "boot-1", signed)
    lines = [r.getMessage() for r in caplog.records if "edge boot verdict compared" in r.getMessage()]
    if result is None:
        assert lines == []
        return
    python = "verified" if signed else "invalid"
    assert lines == [f"fastpath edge boot verdict compared result={result} edge={logged_edge} python={python}"]


def test_a_header_the_route_cannot_read_is_compared_as_invalid(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two boot headers parse to none here, and the route's signature
    verdict is then `invalid`: the edge's `verified` on them is a
    disagreement the comparison sees, since it runs whatever the header
    parsed to."""
    from tests.test_fastpath_pilot_route import _body, _pilot, _seed

    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    _store, _database, key = _seed()
    headers = [(b"x-tr-boot-auth", b"kid=a,sig=AA"), (b"x-tr-boot-auth", b"kid=b,sig=AA"), (b"x-tr-edge-boot", b"verified")]
    request = Request({"type": "http", "method": "POST", "path": PATH, "headers": headers})
    with caplog.at_level(logging.WARNING, logger=gateway.logger.name):
        gateway._authorize_gateway_sync(request, _body(key.hash), _pilot(fastpath_pilot_enabled=False))
    lines = [r.getMessage() for r in caplog.records if "edge boot verdict differs" in r.getMessage()]
    assert len(lines) == 1 and "edge=verified python=invalid kid= " in lines[0], lines


def test_the_route_compares_the_signature_not_its_whole_verification(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boot whose signature holds but whose digest Python does not accept:
    verify_boot_auth says no, the edge says verified, and that is no
    disagreement, since the comparison is of the signature alone."""
    from tests.test_fastpath_pilot_route import _body, _pilot, _seed

    private, boot = _boot()
    store, _database, key = _seed()
    monkeypatch.setattr(store, "get_gateway_boot", lambda kid: boot if kid == boot.kid else None)
    monkeypatch.setattr(gateway, "_stage_d_accepted_image_digests", lambda _request, _settings: frozenset())
    body = _body(key.hash)
    raw = body.model_dump_json().encode()
    auth = _auth(private, boot.kid, body=raw)
    headers = [
        (b"x-tr-boot-auth", f"kid={auth.kid},sig={auth.signature}".encode()),
        (b"x-tr-edge-boot", b"verified"),
    ]
    request = Request({"type": "http", "method": "POST", "path": PATH, "headers": headers})
    with caplog.at_level(logging.INFO, logger=gateway.logger.name):
        gateway._authorize_gateway_sync(request, body, _pilot(fastpath_pilot_enabled=False), raw_body=raw)
    assert not [r for r in caplog.records if "edge boot verdict differs" in r.getMessage()]
    compared = [r.getMessage() for r in caplog.records if "edge boot verdict compared" in r.getMessage()]
    assert compared == ["fastpath edge boot verdict compared result=agree edge=verified python=verified"]


def test_the_comparison_reads_the_path_as_the_server_decoded_it(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path with an escaped line feed: the enclave signed over the decoded
    path, the edge checked over it, and request.url.path drops the line
    feed (verify_boot_auth then fails); the comparison reads the scope's
    path and agrees with the edge."""
    from tests.test_fastpath_pilot_route import _body, _pilot, _seed

    private, boot = _boot()
    store, _database, key = _seed()
    monkeypatch.setattr(store, "get_gateway_boot", lambda kid: boot if kid == boot.kid else None)
    body = _body(key.hash)
    raw = body.model_dump_json().encode()
    decoded = PATH + "\n"
    auth = _auth(private, boot.kid, path=decoded, body=raw)
    headers = [
        (b"x-tr-boot-auth", f"kid={auth.kid},sig={auth.signature}".encode()),
        (b"x-tr-edge-boot", b"verified"),
    ]
    request = Request({"type": "http", "method": "POST", "path": decoded, "headers": headers})
    assert request.url.path == PATH
    with caplog.at_level(logging.INFO, logger=gateway.logger.name):
        gateway._authorize_gateway_sync(request, body, _pilot(fastpath_pilot_enabled=False), raw_body=raw)
    assert not [r for r in caplog.records if "edge boot verdict differs" in r.getMessage()]
    compared = [r.getMessage() for r in caplog.records if "edge boot verdict compared" in r.getMessage()]
    assert compared == ["fastpath edge boot verdict compared result=agree edge=verified python=verified"]
