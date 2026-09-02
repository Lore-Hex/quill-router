from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.services.safe_egress import (
    aassert_public_url,
    aresolve_public_or_reject,
    assert_public_url,
    is_safe_public_ip,
    resolve_public_or_reject,
)
from trusted_router.storage_models import UserProvidedModel
from trusted_router.user_model_rules import (
    DispatchBudget,
    dispatch_budget,
    reserved_user_model_names,
    sign_request_body,
    user_model_is_on_the_clock,
    validate_endpoint_url,
    validate_user_model_display_name,
    validate_user_model_slug,
)


def _model(**overrides: Any) -> UserProvidedModel:
    values: dict[str, Any] = {
        "id": "tr-user-model/owner-demo",
        "owner_user_id": "owner",
        "owner_workspace_id": "workspace",
        "name": "Demo",
        "kind": "machine",
    }
    values.update(overrides)
    return UserProvidedModel(**values)


@pytest.mark.parametrize(
    "address",
    (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",  # noqa: S104 - address classification fixture
        "240.0.0.1",
        "::1",
        "fe80::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "100.64.0.1",  # shared address space (CGNAT): cloud-internal, never routable
        "100.127.255.254",
        "192.0.2.1",  # documentation range
        "198.18.0.1",  # benchmarking range
        "2001:db8::1",
        "not-an-ip",
    ),
)
def test_safe_egress_rejects_non_public_addresses(address: str) -> None:
    assert is_safe_public_ip(address) is False


@pytest.mark.parametrize("address", ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"))
def test_safe_egress_accepts_public_addresses(address: str) -> None:
    assert is_safe_public_ip(address) is True


def test_safe_egress_rejects_if_any_dns_answer_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        resolve_public_or_reject("owner.example")
    assert exc_info.value.status_code == 400


def test_public_url_scheme_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )
    assert_public_url("https://owner.example", allow_http=False)
    assert_public_url("http://owner.example", allow_http=True)
    with pytest.raises(HTTPException, match="https"):
        assert_public_url("http://owner.example", allow_http=False)


@pytest.mark.asyncio
async def test_endpoint_url_requires_https_outside_local_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )
    assert (
        await validate_endpoint_url("http://owner.example/base/", Settings(environment="test"))
        == "http://owner.example/base"
    )
    with pytest.raises(HTTPException, match="https"):
        await validate_endpoint_url(
            "http://owner.example/base",
            Settings(
                environment="staging",
                service_surface="control",
                attribution_cookie_secret="staging-attribution-" + "a" * 32,
                stripe_webhook_secret="whsec_" + "staging",
                stripe_secret_key="sk_" + "staging",
            ),
        )


@pytest.mark.parametrize("value", ("openai", "TrustedRouter", "support", "gpt"))
def test_reserved_slugs_and_display_names_are_rejected(value: str) -> None:
    with pytest.raises(HTTPException):
        validate_user_model_slug(value)
    with pytest.raises(HTTPException):
        validate_user_model_display_name(value)


def test_reserved_names_include_catalog_authors_and_providers() -> None:
    reserved = reserved_user_model_names()
    assert "openai" in reserved
    assert "anthropic" in reserved
    assert "trustedrouter" in reserved


def test_non_reserved_slug_and_display_name_are_normalized() -> None:
    assert validate_user_model_slug("My-Model") == "my-model"
    assert validate_user_model_display_name("  A Helpful Operator  ") == (
        "A Helpful Operator"
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"enabled": False, "online": True}, False),
        ({"status": "suspended", "online": True}, False),
        ({"status": "retired", "online": True}, False),
        ({"online": False}, False),
        ({"online": True, "heartbeat_expires_at": None}, True),
        ({"online": True, "heartbeat_expires_at": "invalid"}, False),
    ),
)
def test_on_the_clock_truth_table(overrides: dict[str, Any], expected: bool) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert user_model_is_on_the_clock(_model(**overrides), now) is expected


def test_on_the_clock_heartbeat_must_be_in_the_future() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert user_model_is_on_the_clock(
        _model(online=True, heartbeat_expires_at=(now + timedelta(seconds=1)).isoformat()),
        now,
    )
    assert not user_model_is_on_the_clock(
        _model(online=True, heartbeat_expires_at=now.isoformat()),
        now,
    )


def test_dispatch_budgets_are_kind_derived() -> None:
    assert dispatch_budget("machine") == DispatchBudget(10, 30, 60, 300)
    assert dispatch_budget("agent") == DispatchBudget(10, 60, 60, 600)
    assert dispatch_budget("human") == DispatchBudget(10, 300, 120, 900)
    with pytest.raises(ValueError, match="invalid_user_model_kind"):
        dispatch_budget("other")


def test_tr_signature_documented_vector() -> None:
    body = b'{"model":"demo","stream":false}'
    assert sign_request_body("test-signing-secret", body, 1_700_000_000) == (
        "t=1700000000,"
        "v1=a7597e2bfa4bc480b058f31a24542b3ab0c99fe6231ae15aa0498fd5bd1d4304"
    )


# --- Review-round regressions -------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["Has Spaces", "under_score", "trailing-", "-leading", "a" * 80, "ünïcode", ""],
)
def test_invalid_slug_grammar_is_a_400_not_a_500(value: str) -> None:
    with pytest.raises(HTTPException) as captured:
        validate_user_model_slug(value)
    assert captured.value.status_code == 400


@pytest.mark.parametrize(
    "url",
    [
        "https://host]:1@127.0.0.1/",  # malformed IPv6 brackets: urlparse raises
        "https://[::1/",
        "https:///nohost",
        "https://",
        "ftp://owner.example/",
        "javascript:alert(1)",
    ],
)
@pytest.mark.asyncio
async def test_malformed_urls_are_400_not_500(url: str) -> None:
    with pytest.raises(HTTPException) as captured:
        await aassert_public_url(url, allow_http=False)
    assert captured.value.status_code == 400
    with pytest.raises(HTTPException) as captured_sync:
        assert_public_url(url, allow_http=False)
    assert captured_sync.value.status_code == 400


@pytest.mark.asyncio
async def test_async_resolve_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow nameserver behind an attacker-chosen hostname must not stall
    every other request on the worker: the lookup runs in a thread and the
    loop keeps turning while it blocks."""
    import asyncio
    import time

    def slow_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(0.4)  # blocking on purpose: this is what a stuck resolver does
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await aresolve_public_or_reject("slow.example")
    finally:
        beat.cancel()
    # ~20 ticks fit in 0.4s if the loop is free; a blocked loop yields ~0.
    assert ticks >= 5, ticks


@pytest.mark.asyncio
async def test_async_resolve_is_bounded_by_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from trusted_router.services import safe_egress

    monkeypatch.setattr(safe_egress, "RESOLVE_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()

    def blackholed(*_args: Any, **_kwargs: Any) -> list[Any]:
        release.wait(5.0)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", blackholed)
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as captured:
            await aresolve_public_or_reject("blackhole.example")
        assert captured.value.status_code == 400
        assert time.monotonic() - started < 2.0
    finally:
        release.set()  # let the worker thread finish so the run stays clean


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        # explicit non-fault tokens win over any status
        (502, "client_closed", False),
        (500, "internal_error", False),
        (422, "upstream_client_error", False),
        (None, "cancelled", False),
        # a status decides otherwise: 5xx strikes, everything else does not
        (503, "provider_error", True),
        (502, None, True),
        (599, "anything", True),
        (401, "provider_error", False),
        (422, "provider_error", False),
        (499, "provider_error", False),
        (429, "timeout", False),
        # no status: only transport/shape tokens strike; a bare provider_error
        # is the enclave's default label and carries no evidence
        (None, "timeout", True),
        (None, "user_model_timeout", True),
        (None, "connection_error", True),
        (None, "malformed_response", True),
        (None, "provider_error", False),
        (None, None, False),
    ],
)
def test_is_owner_fault_rule(status: int | None, error_type: str | None, expected: bool) -> None:
    from trusted_router.user_model_rules import is_owner_fault

    assert is_owner_fault(status, error_type) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://api.trustedrouter.com/v1",
        "https://TrustedRouter.com/v1",
        "https://api.allyrouter.com/v1/",
        "https://uptimerouter.com./v1",
    ],
)
@pytest.mark.asyncio
async def test_endpoint_url_refuses_trustedrouter_itself(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A→TR→A recursion: an owner model must never point back at us."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )
    with pytest.raises(HTTPException) as captured:
        await validate_endpoint_url(url, Settings(environment="test"))
    assert captured.value.status_code == 400
    assert "TrustedRouter" in str(captured.value.detail)
    # a look-alike that is not a subdomain is fine
    assert (
        await validate_endpoint_url("https://nottrustedrouter.com/v1", Settings(environment="test"))
        == "https://nottrustedrouter.com/v1"
    )
