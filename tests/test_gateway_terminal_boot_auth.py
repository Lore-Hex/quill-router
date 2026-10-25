"""E4 R3: ``X-TR-Boot-Auth`` on ``/internal/gateway/settle`` and ``/refund``.

The contract is docs/design/durable-settle-outbox.md section 9.2. The outbox
sweeper replays a frozen terminal signed by the AUTHORIZING boot; the sender
need not be that boot. A present header is verified against
``authorization.stage_d_boot_kid`` through the heartbeat verifier; an absent
header is the unsigned behaviour every enclave uses today; an authorization
with no boot kid ignores the header. A refusal never settles, never refunds,
and the retry with a correct header settles exactly once.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from tests.test_settle_one_commit import INTERNAL, MODEL, _money, _settle_body
from tests.test_settle_one_commit import fixed_catalog as fixed_catalog  # noqa: F401
from tests.test_settle_one_commit import prod_store as prod_store  # noqa: F401
from trusted_router.gateway_boot import GatewayBoot, boot_auth_digest
from trusted_router.receipt_keys import b64url_encode
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest

GATEWAY_LOGGER = "trusted_router.routes.internal.gateway"
SETTLE = "/v1/internal/gateway/settle"
REFUND = "/v1/internal/gateway/refund"
WORKSPACE = "ws-one-commit"


def _boot(kid: str) -> tuple[Ed25519PrivateKey, GatewayBoot]:
    private = Ed25519PrivateKey.generate()
    return private, GatewayBoot(
        kid=kid,
        jwk={
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url_encode(private.public_key().public_bytes_raw()),
        },
        approved=True,
        verified=True,
        image_digest="sha256:" + "a1" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-10-24T00:00:00Z",
        declares_settle_outbox=True,
    )


def _authorize(key: Any) -> dict[str, Any]:
    """An unsigned authorize, as every pre-Stage-D enclave sends it."""
    return gateway._authorize_gateway_sync(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, model=MODEL, estimated_input_tokens=100,
            max_output_tokens=100, stream=True, route_type="chat.completions",
        ),
        INTERNAL,
    )["data"]


def _bind_authorizing_boot(store: Any, db: Any, authorization_id: str, boot: GatewayBoot) -> None:
    """What authorize records for a verified boot: the kid on the row."""
    store.observe_gateway_boot(boot)
    db.gateway_authorizations[authorization_id]["stage_d_boot_kid"] = boot.kid
    stored = store.get_gateway_authorization(authorization_id)
    assert stored is not None and stored.stage_d_boot_kid == boot.kid


def _raw(body: GatewaySettleRequest) -> bytes:
    return body.model_dump_json(exclude_none=True).encode("utf-8")


def _header(private: Ed25519PrivateKey, kid: str, path: str, raw_body: bytes) -> str:
    signature = private.sign(boot_auth_digest("POST", path, raw_body))
    return f"kid={kid},sig={b64url_encode(signature)}"


def _request(path: str, raw_body: bytes, header: str | None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if header is not None:
        headers.append((b"x-tr-boot-auth", header.encode("utf-8")))

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": path, "headers": headers}, receive)


def _terminal(path: str, body: GatewaySettleRequest, header: str | None) -> dict[str, Any]:
    entry = gateway.settle_gateway if path == SETTLE else gateway.refund_gateway
    return asyncio.run(entry(_request(path, _raw(body), header), body, INTERNAL))["data"]


def _refused(path: str, body: GatewaySettleRequest, header: str) -> HTTPException:
    with pytest.raises(HTTPException) as raised:
        _terminal(path, body, header)
    return raised.value


def _timing_outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    outcomes = []
    for record in caplog.records:
        message = record.getMessage()
        if record.name == GATEWAY_LOGGER and message.startswith("settle timing "):
            match = re.search(r" boot_auth=(\S+)$", message)
            assert match is not None, message
            outcomes.append(match.group(1))
    return outcomes


def _lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def _assert_refused(error: HTTPException) -> None:
    assert error.status_code == 401
    assert error.detail["error"]["type"] == "unauthorized"
    assert error.detail["error"]["reason"] == "boot_not_accepted"


def test_header_signed_by_the_authorizing_boot_settles(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    private, boot = _boot("boot-r3")
    authorized = _authorize(key)
    _bind_authorizing_boot(store, db, authorized["authorization_id"], boot)
    body = _settle_body(authorized)
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    data = _terminal(SETTLE, body, _header(private, boot.kid, SETTLE, _raw(body)))

    assert data["settled"] is True and data["finalization_outcome"] == "settled"
    assert data["cost_microdollars"] > 0
    assert _money(db, WORKSPACE, key.hash)[0] == data["cost_microdollars"]
    assert _timing_outcomes(caplog) == ["verified"]
    assert _lines(caplog, "gateway.terminal_boot_auth_") == []


def test_absent_header_keeps_the_unsigned_behaviour(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    _private, boot = _boot("boot-r3")
    authorized = _authorize(key)
    _bind_authorizing_boot(store, db, authorized["authorization_id"], boot)
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    data = _terminal(SETTLE, _settle_body(authorized), None)

    assert data["settled"] is True and data["cost_microdollars"] > 0
    assert _timing_outcomes(caplog) == ["absent"]
    assert _lines(caplog, "gateway.terminal_boot_auth_") == []


def test_header_naming_another_verified_boot_is_refused_and_books_nothing(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    _authorizing_private, authorizing = _boot("boot-r3")
    other_private, other = _boot("boot-other")
    store.observe_gateway_boot(other)
    authorized = _authorize(key)
    aid = authorized["authorization_id"]
    _bind_authorizing_boot(store, db, aid, authorizing)
    body = _settle_body(authorized)
    before = _money(db, WORKSPACE, key.hash)
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    # A real, verified boot signed these exact bytes; it is just not the one
    # that authorized the request.
    error = _refused(SETTLE, body, _header(other_private, other.kid, SETTLE, _raw(body)))

    _assert_refused(error)
    assert _money(db, WORKSPACE, key.hash) == before
    stored = store.get_gateway_authorization(aid)
    assert stored is not None and stored.settled is False
    assert _timing_outcomes(caplog) == []
    assert _lines(caplog, "gateway.terminal_boot_auth_refused") == [
        f"gateway.terminal_boot_auth_refused authorization_id={aid} kind=settle "
        "kid=boot-other authorizing_kid=boot-r3"
    ]


@pytest.mark.parametrize("tamper", ["bytes", "key", "malformed", "duplicate"])
def test_header_that_fails_verification_is_refused(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture, tamper: str,
) -> None:
    store, db, key = prod_store
    private, boot = _boot("boot-r3")
    authorized = _authorize(key)
    aid = authorized["authorization_id"]
    _bind_authorizing_boot(store, db, aid, boot)
    body = _settle_body(authorized)
    raw = _raw(body)
    before = _money(db, WORKSPACE, key.hash)
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    if tamper == "bytes":
        header = _header(private, boot.kid, SETTLE, raw + b" ")
        request = _request(SETTLE, raw, header)
    elif tamper == "key":
        stranger, _unregistered = _boot(boot.kid)
        request = _request(SETTLE, raw, _header(stranger, boot.kid, SETTLE, raw))
    elif tamper == "malformed":
        request = _request(SETTLE, raw, f"kid={boot.kid}")
    else:
        good = _header(private, boot.kid, SETTLE, raw).encode("utf-8")
        request = _request(SETTLE, raw, None)
        request.scope["headers"].extend([(b"x-tr-boot-auth", good), (b"x-tr-boot-auth", good)])
    with pytest.raises(HTTPException) as raised:
        asyncio.run(gateway.settle_gateway(request, body, INTERNAL))

    _assert_refused(raised.value)
    assert _money(db, WORKSPACE, key.hash) == before
    stored = store.get_gateway_authorization(aid)
    assert stored is not None and stored.settled is False
    assert _timing_outcomes(caplog) == []
    expected_kid = "-" if tamper in {"malformed", "duplicate"} else boot.kid
    assert _lines(caplog, "gateway.terminal_boot_auth_refused") == [
        f"gateway.terminal_boot_auth_refused authorization_id={aid} kind=settle "
        f"kid={expected_kid} authorizing_kid=boot-r3"
    ]


@pytest.mark.parametrize("signed", [True, False])
def test_authorization_without_a_boot_kid_ignores_the_header(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture, signed: bool,
) -> None:
    """Settles were never signed before #595: a header on a pre-Stage-D
    authorization is logged and ignored, whether or not it would verify."""
    store, db, key = prod_store
    private, boot = _boot("boot-r3")
    store.observe_gateway_boot(boot)
    authorized = _authorize(key)
    aid = authorized["authorization_id"]
    stored = store.get_gateway_authorization(aid)
    assert stored is not None and not stored.stage_d_boot_kid
    body = _settle_body(authorized)
    header = _header(private, boot.kid, SETTLE, _raw(body)) if signed else f"kid={boot.kid},sig=xx"
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    data = _terminal(SETTLE, body, header)

    assert data["settled"] is True and data["cost_microdollars"] > 0
    assert _timing_outcomes(caplog) == ["ignored_no_boot_kid"]
    assert _lines(caplog, "gateway.terminal_boot_auth_ignored") == [
        f"gateway.terminal_boot_auth_ignored authorization_id={aid} kind=settle "
        "kid=boot-r3 reason=no_boot_kid"
    ]
    assert _lines(caplog, "gateway.terminal_boot_auth_refused") == []


def test_refusal_is_idempotent_and_the_good_retry_settles_once(
    prod_store: tuple[Any, Any, Any],
) -> None:
    store, db, key = prod_store
    private, boot = _boot("boot-r3")
    other_private, other = _boot("boot-other")
    store.observe_gateway_boot(other)
    authorized = _authorize(key)
    aid = authorized["authorization_id"]
    _bind_authorizing_boot(store, db, aid, boot)
    body = _settle_body(authorized)
    raw = _raw(body)
    bad = _header(other_private, other.kid, SETTLE, raw)
    good = _header(private, boot.kid, SETTLE, raw)
    before = _money(db, WORKSPACE, key.hash)

    _assert_refused(_refused(SETTLE, body, bad))
    _assert_refused(_refused(SETTLE, body, bad))
    assert _money(db, WORKSPACE, key.hash) == before

    first = _terminal(SETTLE, body, good)
    assert first["settled"] is True and first.get("already_settled") is not True
    booked = _money(db, WORKSPACE, key.hash)
    assert booked[0] == first["cost_microdollars"] and booked[1] == 0

    replay = _terminal(SETTLE, body, good)
    assert replay["already_settled"] is True
    assert replay["cost_microdollars"] == first["cost_microdollars"]
    assert _money(db, WORKSPACE, key.hash) == booked

    # A refused header after settlement is still refused: the stored outcome
    # is not disclosed to a sender the authorizing boot did not sign for.
    _assert_refused(_refused(SETTLE, body, bad))
    assert _money(db, WORKSPACE, key.hash) == booked


def test_refund_verifies_the_header_the_same_way(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    private, boot = _boot("boot-r3")
    other_private, other = _boot("boot-other")
    store.observe_gateway_boot(other)
    authorized = _authorize(key)
    aid = authorized["authorization_id"]
    _bind_authorizing_boot(store, db, aid, boot)
    body = _settle_body(authorized, success=False)
    raw = _raw(body)
    before = _money(db, WORKSPACE, key.hash)
    assert before[1] > 0  # the reservation is still held
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    _assert_refused(_refused(REFUND, body, _header(other_private, other.kid, REFUND, raw)))
    _assert_refused(_refused(REFUND, body, _header(private, boot.kid, SETTLE, raw)))  # wrong path
    assert _money(db, WORKSPACE, key.hash) == before
    stored = store.get_gateway_authorization(aid)
    assert stored is not None and stored.settled is False
    assert [line.split(" kind=")[1].split(" ")[0] for line in
            _lines(caplog, "gateway.terminal_boot_auth_refused")] == ["refund", "refund"]

    data = _terminal(REFUND, body, _header(private, boot.kid, REFUND, raw))

    assert data["settled"] is True and data["finalization_outcome"] == "refunded"
    assert data["cost_microdollars"] == 0
    assert _money(db, WORKSPACE, key.hash) == (0, 0, 0, 0)
    assert _timing_outcomes(caplog) == ["verified"]


def test_refund_without_a_header_is_unchanged(
    prod_store: tuple[Any, Any, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    _private, boot = _boot("boot-r3")
    authorized = _authorize(key)
    _bind_authorizing_boot(store, db, authorized["authorization_id"], boot)
    caplog.set_level("INFO", logger=GATEWAY_LOGGER)

    data = _terminal(REFUND, _settle_body(authorized, success=False), None)

    assert data["finalization_outcome"] == "refunded"
    assert _money(db, WORKSPACE, key.hash) == (0, 0, 0, 0)
    assert _timing_outcomes(caplog) == ["absent"]
