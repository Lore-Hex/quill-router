from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.services.safe_egress import (
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
        "id": "trustedrouter/user-demo",
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


def test_endpoint_url_requires_https_outside_local_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )
    assert validate_endpoint_url(
        "http://owner.example/base/", Settings(environment="test")
    ) == "http://owner.example/base"
    with pytest.raises(HTTPException, match="https"):
        validate_endpoint_url(
            "http://owner.example/base", Settings(environment="staging")
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
