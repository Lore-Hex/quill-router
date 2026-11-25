"""PayPal payer-email match: recorded as a boolean on the paying user, never blocking."""

from __future__ import annotations

from typing import Any

import pytest

from trusted_router.config import Settings
from trusted_router.services import paypal_billing
from trusted_router.services.paypal_billing import (
    capture_paypal_order_for_workspace,
    record_paypal_payer_email_match,
)
from trusted_router.storage import STORE, InMemoryStore


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id=None,
    )


def _order(workspace_id: str, user_id: str | None, *, payer_email: str | None) -> dict[str, Any]:
    custom_id = f"tr1|{workspace_id}|2500|2606" + (f"|{user_id}" if user_id else "")
    order: dict[str, Any] = {
        "id": "ORDER-PAYER",
        "status": "COMPLETED",
        "purchase_units": [{
            "reference_id": workspace_id,
            "custom_id": custom_id,
            "payments": {"captures": [{
                "id": "CAPTURE-PAYER",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "26.06"},
            }]},
        }],
    }
    if payer_email is not None:
        order["payer"] = {"email_address": payer_email, "payer_id": "PAYERID"}
    return order


def _account(email: str) -> tuple[str, str]:
    result = STORE.signup(email=email)
    assert result is not None
    return result.workspace.id, result.user.id


@pytest.mark.parametrize(("payer_email", "expected"), [
    ("owner@example.com", True),
    ("  Owner@Example.COM ", True),
    ("someone-else@example.net", False),
])
def test_capture_records_payer_match_boolean(
    monkeypatch: pytest.MonkeyPatch, payer_email: str, expected: bool,
) -> None:
    workspace_id, user_id = _account("owner@example.com")
    monkeypatch.setattr(
        paypal_billing, "_paypal_post",
        lambda *_a, **_k: _order(workspace_id, user_id, payer_email=payer_email),
    )

    result = capture_paypal_order_for_workspace(
        order_id="ORDER-PAYER", workspace_id=workspace_id, settings=_settings(),
    )

    assert result.credited is True
    user = STORE.get_user(user_id)
    assert user is not None
    assert user.paypal_payer_email_matches is expected
    if not expected:
        # Only the boolean is kept; the payer's address is stored nowhere on the user.
        assert "someone-else" not in repr(user)


def test_capture_without_payer_email_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account("owner@example.com")
    monkeypatch.setattr(
        paypal_billing, "_paypal_post",
        lambda *_a, **_k: _order(workspace_id, user_id, payer_email=None),
    )

    capture_paypal_order_for_workspace(order_id="ORDER-PAYER", workspace_id=workspace_id, settings=_settings())

    user = STORE.get_user(user_id)
    assert user is not None
    assert user.paypal_payer_email_matches is None


def test_legacy_reference_falls_back_to_workspace_owner() -> None:
    workspace_id, user_id = _account("owner@example.com")

    recorded = record_paypal_payer_email_match(
        _order(workspace_id, None, payer_email="other@example.net"), workspace_id=workspace_id,
    )

    assert recorded is False
    user = STORE.get_user(user_id)
    assert user is not None
    assert user.paypal_payer_email_matches is False


def test_store_failure_never_fails_the_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account("owner@example.com")

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(InMemoryStore, "set_user_paypal_payer_match", broken)
    monkeypatch.setattr(
        paypal_billing, "_paypal_post",
        lambda *_a, **_k: _order(workspace_id, user_id, payer_email="other@example.net"),
    )

    result = capture_paypal_order_for_workspace(
        order_id="ORDER-PAYER", workspace_id=workspace_id, settings=_settings(),
    )

    assert result.credited is True
