"""PayPal top-up guard: refusals before money moves, latches after adverse PayPal events."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.paypal_approved_order import approved_order, mock_approved_order
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.schemas import CheckoutRequest
from trusted_router.services import (
    abuse_tripwires,
    paused_topup,
    paypal_billing,
    paypal_topup_guard,
)
from trusted_router.services.auto_refill import AutoRefillOutcome, maybe_charge_after_settle
from trusted_router.services.paused_topup import (
    BILLING_PAUSED_MESSAGE,
    CHECKOUT_GUARD_BILLING_PAUSED,
)
from trusted_router.services.paypal_billing import (
    capture_paypal_order_for_workspace,
    create_paypal_checkout_session,
)
from trusted_router.services.paypal_topup_guard import (
    GUARD_CODE_AMOUNT_CAP,
    GUARD_CODE_DAILY_LIMIT,
    GUARD_CODE_UNAVAILABLE,
    check_paypal_capture,
    check_paypal_checkout,
    checkout_guard_code,
    owner_is_established,
)
from trusted_router.storage import STORE, InMemoryStore

NOW = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id=None,
        **overrides,
    )


def _account(email: str = "owner@example.com") -> tuple[str, str]:
    result = STORE.signup(email=email)
    assert result is not None
    return result.workspace.id, result.user.id


def _make_established(user_id: str) -> None:
    user = STORE.get_user(user_id)
    assert user is not None
    user.created_at = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).isoformat()
    assert STORE.add_lifetime_topup(user_id, 500_000_000, f"seed-topup-{user_id}")


def _latch(workspace_id: str, ref: str = "ops-1") -> None:
    assert STORE.record_workspace_abuse_and_demote(
        workspace_id, abuse_ref=ref, operator_identity="ops", reason="review",
    )


def _order(workspace_id: str, user_id: str, *, capture_id: str = "CAPTURE-G",
           seller_protection: str | None = None) -> dict[str, Any]:
    capture: dict[str, Any] = {
        "id": capture_id,
        "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "26.06"},
    }
    if seller_protection is not None:
        capture["seller_protection"] = {"status": seller_protection, "dispute_categories": []}
    return {
        "id": f"ORDER-{capture_id}",
        "status": "COMPLETED",
        "purchase_units": [{
            "reference_id": workspace_id,
            "custom_id": f"tr1|{workspace_id}|2500|2606|{user_id}",
            "payments": {"captures": [capture]},
        }],
    }


def _refusal(exc: pytest.ExceptionInfo[HTTPException]) -> tuple[int, str | None]:
    return exc.value.status_code, checkout_guard_code(exc.value)


# --- established -------------------------------------------------------------


def test_identity_verified_owner_is_established() -> None:
    _, user_id = _account()
    user = STORE.get_user(user_id)
    assert user is not None
    assert owner_is_established(STORE, user, _settings(), NOW) is False
    user.identity_status = "approved"
    assert owner_is_established(STORE, user, _settings(), NOW) is True


def test_old_owner_needs_both_age_and_lifetime_topups() -> None:
    _, user_id = _account()
    user = STORE.get_user(user_id)
    assert user is not None
    user.created_at = (NOW - dt.timedelta(days=45)).isoformat()
    # Old enough but never paid: not established.
    assert owner_is_established(STORE, user, _settings(), NOW) is False
    assert STORE.add_lifetime_topup(user_id, 50_000_000, "seed-50")
    assert owner_is_established(STORE, user, _settings(), NOW) is True
    # Paid enough but too young.
    user.created_at = (NOW - dt.timedelta(days=3)).isoformat()
    assert owner_is_established(STORE, user, _settings(), NOW) is False


def test_lifetime_read_failure_is_not_established(monkeypatch: pytest.MonkeyPatch) -> None:
    _, user_id = _account()
    user = STORE.get_user(user_id)
    assert user is not None
    user.created_at = (NOW - dt.timedelta(days=400)).isoformat()

    def broken(*_a: object, **_k: object) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(InMemoryStore, "get_lifetime_topup_microdollars", broken)
    assert owner_is_established(STORE, user, _settings(), NOW) is False


# --- checkout rules ----------------------------------------------------------


def test_new_workspace_small_topup_is_allowed() -> None:
    workspace_id, user_id = _account()
    subject = check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=2_500, settings=_settings(),
    )
    assert subject is not None and subject.established is False


def test_new_workspace_amount_cap_refuses_with_its_own_code() -> None:
    workspace_id, user_id = _account()
    check_paypal_checkout(  # exactly the cap is allowed
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=2_500, settings=_settings(),
    )
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_501, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_AMOUNT_CAP)
    assert "$25" in raised.value.detail["error"]["message"]


def test_established_workspace_has_no_amount_cap() -> None:
    workspace_id, user_id = _account()
    _make_established(user_id)
    subject = check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=500_000, settings=_settings(),
    )
    assert subject is not None and subject.established is True


@pytest.mark.parametrize("established", [False, True])
def test_abuse_latched_workspace_is_refused_even_when_established(established: bool) -> None:
    workspace_id, user_id = _account()
    if established:
        _make_established(user_id)
    _latch(workspace_id)
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)


def test_principal_recovery_pause_refuses_paypal() -> None:
    workspace_id, user_id = _account()
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    workspace.billing_pause_causes = ["principal_recovery"]
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)


def test_operational_pause_does_not_refuse_paypal() -> None:
    workspace_id, user_id = _account()
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    workspace.billing_pause_causes = ["migration"]
    check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=2_500, settings=_settings(),
    )


def test_suspended_paying_member_is_refused() -> None:
    workspace_id, _ = _account()
    member = STORE.ensure_user("member@example.com")
    member.suspended = True
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=member.id,
            credit_amount_cents=2_500, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)


@pytest.mark.parametrize("established", [False, True])
def test_new_github_account_is_refused_unless_established(established: bool) -> None:
    workspace_id, user_id = _account()
    user = STORE.get_user(user_id)
    assert user is not None
    if established:
        user.identity_status = "approved"
    # GitHub account created an hour before the TrustedRouter signup.
    signed_up = dt.datetime.fromisoformat(user.created_at.replace("Z", "+00:00"))
    user.github_account_created_at = (signed_up - dt.timedelta(hours=1)).isoformat()
    if established:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )
        return
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    # The refusal never names the rule.
    assert "github" not in raised.value.detail["error"]["message"].lower()


def test_old_github_account_is_allowed() -> None:
    workspace_id, user_id = _account()
    user = STORE.get_user(user_id)
    assert user is not None
    user.github_account_created_at = "2015-01-01T00:00:00Z"
    check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=2_500, settings=_settings(),
    )


def test_disposable_email_owner_is_refused() -> None:
    workspace_id, user_id = _account("ring@qtymail.com")
    with pytest.raises(HTTPException) as raised:
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)


def test_log_mode_never_refuses_but_logs(caplog: pytest.LogCaptureFixture) -> None:
    workspace_id, user_id = _account("ring@qtymail.com")
    _latch(workspace_id)
    with caplog.at_level(logging.INFO, logger=paypal_topup_guard.__name__):
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=50_000, settings=_settings(paypal_topup_guard_mode="log"),
        )
    record = next(r for r in caplog.records if r.getMessage() == "paypal_topup_guard.observed")
    rules = record.__dict__["rules"].split(",")
    assert {"workspace_paused", "disposable_email", "new_workspace_amount_cap"} <= set(rules)
    assert record.__dict__["enforced_rules"] == ""


def test_log_only_rules_demote_one_rule_and_keep_the_rest() -> None:
    workspace_id, user_id = _account()
    settings = _settings(paypal_topup_guard_log_only_rules="new_workspace_amount_cap")
    check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=50_000, settings=settings,
    )
    _latch(workspace_id)
    with pytest.raises(HTTPException):
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=settings,
        )


def test_off_mode_skips_the_guard() -> None:
    workspace_id, user_id = _account()
    _latch(workspace_id)
    assert check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=50_000, settings=_settings(paypal_topup_guard_mode="off"),
    ) is None


def test_settings_reject_unknown_mode_and_non_positive_limits() -> None:
    with pytest.raises(ValueError, match="PAYPAL_TOPUP_GUARD_MODE"):
        _settings(paypal_topup_guard_mode="strict")
    with pytest.raises(ValueError, match="DAILY_CAPTURES"):
        _settings(paypal_new_workspace_daily_captures=0)


# --- capture rules -----------------------------------------------------------


def test_capture_velocity_limits_new_workspace_per_day() -> None:
    workspace_id, _ = _account()
    settings = _settings()
    check_paypal_capture(STORE, workspace_id=workspace_id, settings=settings)
    check_paypal_capture(STORE, workspace_id=workspace_id, settings=settings)
    with pytest.raises(HTTPException) as raised:
        check_paypal_capture(STORE, workspace_id=workspace_id, settings=settings)
    assert _refusal(raised) == (429, GUARD_CODE_DAILY_LIMIT)


def test_capture_velocity_limits_one_owner_across_workspaces() -> None:
    first, user_id = _account()
    settings = _settings(paypal_new_workspace_daily_captures=5, paypal_new_owner_daily_captures=2)
    second = STORE.create_workspace(user_id, "second").id
    check_paypal_capture(STORE, workspace_id=first, settings=settings)
    check_paypal_capture(STORE, workspace_id=second, settings=settings)
    with pytest.raises(HTTPException) as raised:
        check_paypal_capture(STORE, workspace_id=second, settings=settings)
    assert _refusal(raised) == (429, GUARD_CODE_DAILY_LIMIT)


def test_established_workspace_has_no_capture_velocity_limit() -> None:
    workspace_id, user_id = _account()
    _make_established(user_id)
    for _ in range(10):
        check_paypal_capture(STORE, workspace_id=workspace_id, settings=_settings())


def test_capture_rate_limiter_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, _ = _account()

    def broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("limiter down")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", broken)
    with pytest.raises(HTTPException) as raised:
        check_paypal_capture(STORE, workspace_id=workspace_id, settings=_settings())
    assert raised.value.status_code == 503


def test_refused_capture_never_calls_paypal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A latch that lands between order creation and capture stops the capture
    itself, so the buyer is never charged."""
    workspace_id, user_id = _account()
    calls: list[str] = []

    def paypal_post(_settings: Settings, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        return _order(workspace_id, user_id)

    monkeypatch.setattr(paypal_billing, "_paypal_post", paypal_post)
    _latch(workspace_id)
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER-X", workspace_id=workspace_id, settings=_settings(),
        )
    assert _refusal(raised) == (403, CHECKOUT_GUARD_BILLING_PAUSED)
    assert calls == []


def test_third_capture_of_the_day_is_refused_before_paypal(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account()
    mock_approved_order(monkeypatch, workspace_id)
    calls: list[str] = []
    counter = iter(range(100))

    def paypal_post(_settings: Settings, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        return _order(workspace_id, user_id, capture_id=f"CAPTURE-{next(counter)}")

    monkeypatch.setattr(paypal_billing, "_paypal_post", paypal_post)
    for order_id in ("ORDER-A", "ORDER-B"):
        result = capture_paypal_order_for_workspace(
            order_id=order_id, workspace_id=workspace_id, settings=_settings(),
        )
        assert result.credited is True
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER-C", workspace_id=workspace_id, settings=_settings(),
        )
    assert raised.value.status_code == 429
    assert len(calls) == 2


def test_capture_reports_seller_protection_and_logs_not_eligible(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id, user_id = _account()
    mock_approved_order(monkeypatch, workspace_id)
    monkeypatch.setattr(
        paypal_billing, "_paypal_post",
        lambda *_a, **_k: _order(workspace_id, user_id, seller_protection="NOT_ELIGIBLE"),
    )
    with caplog.at_level(logging.INFO, logger=paypal_topup_guard.__name__):
        result = capture_paypal_order_for_workspace(
            order_id="ORDER-SP", workspace_id=workspace_id, settings=_settings(),
        )
    # Logged only: the credit still lands.
    assert result.credited is True
    assert result.seller_protection == "NOT_ELIGIBLE"
    observed = [r for r in caplog.records if r.getMessage() == "paypal_topup_guard.observed"]
    assert observed and "seller_protection_not_eligible" in observed[-1].__dict__["rules"]


def test_pending_echeck_capture_is_not_spendable(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account()
    mock_approved_order(monkeypatch, workspace_id)
    order = _order(workspace_id, user_id)
    capture = order["purchase_units"][0]["payments"]["captures"][0]
    capture["status"] = "PENDING"
    capture["status_details"] = {"reason": "ECHECK"}
    before = STORE.get_credit_account(workspace_id)
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda *_a, **_k: order)
    result = capture_paypal_order_for_workspace(
        order_id="ORDER-ECHECK", workspace_id=workspace_id, settings=_settings(),
    )
    assert (result.status, result.credited) == ("PENDING", False)
    assert STORE.get_credit_account(workspace_id) == before


# --- route-level -------------------------------------------------------------


def test_api_checkout_over_cap_is_refused_before_an_order_exists(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {},
    )
    app = create_app(_settings(), init_observability=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout", headers=user_headers,
            json={"amount": 250, "payment_method": "paypal"},
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["checkout_guard"] == GUARD_CODE_AMOUNT_CAP
    assert calls == []


def test_mock_checkout_also_runs_the_guard() -> None:
    workspace_id, user_id = _account()
    with pytest.raises(HTTPException):
        create_paypal_checkout_session(
            body=CheckoutRequest(amount=250, payment_method="paypal"),
            workspace_id=workspace_id, initiating_user_id=user_id,
            customer_email=None, settings=Settings(environment="local"),  # type: ignore[call-arg]
        )


def _console_client() -> tuple[TestClient, Any, Any]:
    app = create_app(Settings(environment="local"), init_observability=False)  # type: ignore[call-arg]
    client = TestClient(app)
    user = STORE.ensure_user("guard-console@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="email", label="guard-console@example.com",
        ttl_seconds=3600, workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, user, workspace


@pytest.mark.parametrize(("path", "expected"), [
    ("/billing/paypal/success", "error=paypal_missing_order"),
    ("/billing/paypal/cancel?token=ignored", "checkout=cancel"),
])
def test_api_order_defaults_resolve_without_asserting_payment(path: str, expected: str) -> None:
    client, _, _ = _console_client()
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?{expected}"


@pytest.mark.parametrize(("wallet", "established", "accepted"), [
    ({"account_status": "VERIFIED", "email_address": "billing+ai@example.co.uk"}, False, True),
    ({"account_status": "UNVERIFIED", "email_address": "valid@example.org"}, False, False),
    ({"account_id": "KNOWN-ID", "email_address": "valid@example.org"}, False, False),
    ({"account_status": "UNKNOWN", "email_address": "valid@example.org"}, False, False),
    ({}, False, False),
    ({"account_status": "VERIFIED"}, False, False),
    ({"account_status": "VERIFIED", "email_address": "broken"}, False, False),
    ({"account_status": "VERIFIED", "email_address": "valid@EXAMPLE.CON"}, False, False),
    ({"account_status": "VERIFIED", "email_address": "valid@GMIAL.COM"}, False, False),
    ({"account_status": "UNVERIFIED", "email_address": "valid@example.org"}, True, True),
    ({"account_status": "VERIFIED", "email_address": "invalid"}, True, False),
])
def test_wallet_rules_gate_real_capture_call(
    monkeypatch: pytest.MonkeyPatch, wallet: dict[str, str], established: bool, accepted: bool,
) -> None:
    ws, user = _account()
    if established:
        _make_established(user)
    order = approved_order("ORDER-X", ws)
    order["payment_source"] = {"paypal": wallet} if wallet else {"card": {}}
    monkeypatch.setattr(paypal_billing, "_paypal_get", lambda *a, **kw: order)
    posts: list[str] = []
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda _s, p, **kw: posts.append(p) or _order(ws, user))
    if accepted:
        result = capture_paypal_order_for_workspace(order_id="ORDER-X", workspace_id=ws, settings=_settings())
        assert result.credited and result.amount_microdollars == 25_000_000
        assert posts == ["/v2/checkout/orders/ORDER-X/capture"]
    else:
        with pytest.raises(HTTPException) as exc:
            capture_paypal_order_for_workspace(order_id="ORDER-X", workspace_id=ws, settings=_settings())
        assert checkout_guard_code(exc.value) == GUARD_CODE_UNAVAILABLE
        assert posts == []


@pytest.mark.parametrize("problem", ["foreign", "old-cap", "gross-mismatch", "wrong-currency"])
def test_preapproved_order_bound_before_charge(monkeypatch: pytest.MonkeyPatch, problem: str) -> None:
    ws, user = _account()
    if problem == "foreign":
        _make_established(user)
    order = approved_order("ORDER-X", "other-workspace" if problem == "foreign" else ws)
    unit = order["purchase_units"][0]
    if problem == "old-cap":
        unit["custom_id"] = f"tr1|{ws}|2501|2606"
    elif problem == "gross-mismatch":
        unit["amount"]["value"] = "27.00"
    elif problem == "wrong-currency":
        unit["amount"]["currency_code"] = "EUR"
    monkeypatch.setattr(paypal_billing, "_paypal_get", lambda *a, **kw: order)
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda *a, **kw: pytest.fail("must not charge"))
    with pytest.raises(HTTPException) as exc:
        capture_paypal_order_for_workspace(order_id="ORDER-X", workspace_id=ws, settings=_settings())
    if problem == "old-cap":
        assert checkout_guard_code(exc.value) == GUARD_CODE_AMOUNT_CAP
    else:
        assert exc.value.status_code == (403 if problem == "foreign" else 400)


def test_return_retry_reconciles_existing_capture_without_post(monkeypatch: pytest.MonkeyPatch) -> None:
    ws, user = _account()
    paid = _order(ws, user)
    monkeypatch.setattr(paypal_billing, "_paypal_get", lambda *a, **kw: paid)
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda *a, **kw: pytest.fail("already captured"))
    results = [capture_paypal_order_for_workspace(order_id=paid["id"], workspace_id=ws, settings=_settings())
               for _ in range(2)]
    assert [r.credited for r in results] == [True, False]
    assert all(r.capture_id == "CAPTURE-G" for r in results)


def test_console_checkout_over_cap_redirects_with_the_cap_line() -> None:
    client, _, _ = _console_client()
    response = client.post(
        "/console/credits/checkout",
        data={"amount": "250", "payment_method": "paypal"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={GUARD_CODE_AMOUNT_CAP}"
    page = client.get(response.headers["location"]).text
    assert "up to $25 per PayPal payment" in page


def test_console_capture_for_latched_workspace_redirects_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, workspace = _console_client()
    calls: list[str] = []
    monkeypatch.setattr(
        paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {},
    )
    _latch(workspace.id)
    # The console capture route needs PayPal configured to reach the guard.
    monkeypatch.setattr(Settings, "paypal_enabled", property(lambda _self: True))
    response = client.get("/console/credits/paypal/capture?token=ORDER-1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={CHECKOUT_GUARD_BILLING_PAUSED}"
    assert calls == []
    assert "Billing for this workspace is paused" in client.get(response.headers["location"]).text


# --- adverse webhook latch -----------------------------------------------------


def _capture_event(workspace_id: str, user_id: str, capture_id: str = "CAPTURE-W") -> dict[str, Any]:
    return {
        "id": f"WH-{capture_id}",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "custom_id": f"tr1|{workspace_id}|2500|2606|{user_id}",
            "amount": {"currency_code": "USD", "value": "26.06"},
            "create_time": "2026-11-20T00:00:00Z",
            "seller_protection": {"status": "NOT_ELIGIBLE"},
            "supplementary_data": {"related_ids": {"order_id": "ORDER-W"}},
        },
    }


def _reversal_event(capture_id: str = "CAPTURE-W", event_id: str = "WH-REV") -> dict[str, Any]:
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.REVERSED",
        "create_time": "2026-11-24T00:00:00Z",
        "resource": {
            "id": capture_id,
            "status": "REVERSED",
            "amount": {"currency_code": "USD", "value": "26.06"},
            "create_time": "2026-11-24T00:00:00Z",
            "update_time": "2026-11-24T00:00:00Z",
        },
    }


def _refund_event(capture_id: str = "CAPTURE-W") -> dict[str, Any]:
    return {
        "id": "WH-REFUND",
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "create_time": "2026-11-24T00:00:00Z",
        "resource": {
            "id": "REFUND-1",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "26.06"},
            "create_time": "2026-11-24T00:00:00Z",
            "update_time": "2026-11-24T00:00:00Z",
            "supplementary_data": {"related_ids": {"capture_id": capture_id}},
        },
    }


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    def record(message: str, **_kwargs: Any) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(paypal_topup_guard, "ops_alert", record)
    return sent


def _webhook(client: TestClient, event: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/internal/paypal/webhook", json=event)
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def test_reversal_on_new_workspace_latches_it_once(alerts: list[str]) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        assert _webhook(client, _capture_event(workspace_id, user_id))["credited"] is True
        data = _webhook(client, _reversal_event())
        assert data["adverse"] == ["applied"]
        assert data["latches"] == [{"workspace_id": workspace_id, "action": "latched"}]
        workspace = STORE.get_workspace(workspace_id)
        assert workspace is not None and "abuse" in workspace.billing_pause_causes
        # A redelivery does not latch or alert again.
        replay = _webhook(client, _reversal_event(event_id="WH-REV-2"))
        assert replay["latches"] == [{"workspace_id": workspace_id, "action": "already_latched"}]
    assert len(alerts) == 1 and "latched" in alerts[0]
    # And the latched workspace can no longer start a PayPal top-up.
    with pytest.raises(HTTPException):
        check_paypal_checkout(
            STORE, workspace_id=workspace_id, initiating_user_id=user_id,
            credit_amount_cents=2_500, settings=_settings(),
        )


def test_operator_clear_is_not_undone_by_a_redelivery(alerts: list[str]) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        _webhook(client, _capture_event(workspace_id, user_id))
        _webhook(client, _reversal_event())
        assert STORE.clear_workspace_abuse_pause(
            workspace_id, abuse_ref=_abuse_ref(workspace_id), operator_identity="ops",
            reason="reviewed",
        )
        _webhook(client, _reversal_event(event_id="WH-REV-3"))
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None and "abuse" not in workspace.billing_pause_causes


def _abuse_ref(workspace_id: str) -> str:
    refs = [
        event.adverse_ref for event in STORE.target.trust_events.values()
        if event.workspace_id == workspace_id and event.kind == "abuse"
    ]
    assert len(refs) == 1 and refs[0] is not None and refs[0].startswith("paypal-adverse:")
    return refs[0]


def test_reversal_on_established_workspace_alerts_without_latching(alerts: list[str]) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        _make_established(user_id)
        _webhook(client, _capture_event(workspace_id, user_id))
        data = _webhook(client, _reversal_event())
    assert data["latches"] == [{"workspace_id": workspace_id, "action": "alert_only_established"}]
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None and "abuse" not in workspace.billing_pause_causes
    assert len(alerts) == 1 and "alert_only_established" in alerts[0]


def test_auto_latch_off_only_alerts_would_latch(alerts: list[str]) -> None:
    settings = _settings(paypal_adverse_auto_latch=False)
    with TestClient(create_app(settings, init_observability=False)) as client:
        workspace_id, user_id = _account()
        _webhook(client, _capture_event(workspace_id, user_id))
        data = _webhook(client, _reversal_event())
    assert data["latches"] == [{"workspace_id": workspace_id, "action": "would_latch"}]
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None and "abuse" not in workspace.billing_pause_causes


def test_refund_never_latches(alerts: list[str]) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        _webhook(client, _capture_event(workspace_id, user_id))
        data = _webhook(client, _refund_event())
    assert data["adverse"] == ["applied"]
    assert "latches" not in data
    assert alerts == []


def test_latch_failure_never_fails_the_webhook(
    alerts: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_a: object, **_k: object) -> bool:
        raise RuntimeError("store down")

    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        _webhook(client, _capture_event(workspace_id, user_id))
        monkeypatch.setattr(InMemoryStore, "record_workspace_abuse_and_demote", broken)
        data = _webhook(client, _reversal_event())
    assert data["adverse"] == ["applied"]
    assert "latches" not in data
    assert len(alerts) == 1 and "latch_failed" in alerts[0]


# --- tripwire: linked siblings -------------------------------------------------


class _Reader:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self.sessions = sessions

    def shared_client_sessions(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.sessions

    def concentrated_model_spend(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


def _ring() -> tuple[str, str, _Reader]:
    latched, _ = _account("latched@example.com")
    sibling, _ = _account("sibling@example.com")
    _latch(latched, "ops-ring")
    reader = _Reader([{
        "session_id": "sess-ring", "workspace_ids": [latched, sibling],
        "app": "cli", "max_workspace_microdollars": 20_000_000,
    }])
    return latched, sibling, reader


def test_tripwire_names_linked_siblings_without_latching_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(abuse_tripwires, "ops_alert", lambda *_a, **_k: True)
    latched, sibling, reader = _ring()
    emitted: list[abuse_tripwires.Finding] = []
    abuse_tripwires.run_tripwire_sweep(
        reader, STORE, now=NOW, emit=lambda finding: emitted.append(finding) or True,
    )
    assert len(emitted) == 1
    assert emitted[0].latch_candidates == (sibling,)
    assert f"linked_to_latched={sibling}" in emitted[0].message
    assert f"{latched}[already_paused,abuse_latched]" in emitted[0].message
    workspace = STORE.get_workspace(sibling)
    assert workspace is not None and not workspace.billing_paused


def test_tripwire_latches_linked_siblings_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(abuse_tripwires, "ops_alert", lambda message, **_k: sent.append(message) or True)
    _, sibling, reader = _ring()
    abuse_tripwires.run_tripwire_sweep(
        reader, STORE, now=NOW, emit=lambda _f: True, auto_latch_linked=True,
    )
    workspace = STORE.get_workspace(sibling)
    assert workspace is not None and "abuse" in workspace.billing_pause_causes
    assert any("linked_latch" in message for message in sent)


def test_unfunded_farm_never_latches_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse_tripwires, "ops_alert", lambda *_a, **_k: True)
    latched, sibling, _ = _ring()
    reader = _Reader([{
        "session_id": "sess-farm", "workspace_ids": [latched, sibling],
        "app": "cli", "max_workspace_microdollars": 100_000,
    }])
    abuse_tripwires.run_tripwire_sweep(
        reader, STORE, now=NOW, emit=lambda _f: True, auto_latch_linked=True,
    )
    workspace = STORE.get_workspace(sibling)
    assert workspace is not None and not workspace.billing_paused


# --- abuse-latched workspaces: no new money ---------------------------------------


@pytest.fixture
def held_alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        paused_topup, "ops_alert", lambda message, **_k: sent.append(message) or True,
    )
    return sent


def _latched_api_workspace(client: TestClient, user_headers: dict[str, str]) -> str:
    workspace_id = str(client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"])
    _latch(workspace_id)
    return workspace_id


@pytest.mark.parametrize("payment_method", ["paypal", "auto", "ach"])
def test_api_checkout_for_abuse_latched_workspace_is_refused(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str], payment_method: str,
) -> None:
    """Neither a PayPal order nor a Stripe Checkout session is created."""
    calls: list[str] = []
    monkeypatch.setattr(
        paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {},
    )
    monkeypatch.setattr(
        "stripe.checkout.Session.create", lambda **_k: calls.append("stripe") or {},
    )
    settings = _settings(stripe_secret_key="sk_test_dummy")  # noqa: S106 - fixture key
    with TestClient(create_app(settings, init_observability=False)) as client:
        _latched_api_workspace(client, user_headers)
        response = client.post(
            "/v1/billing/checkout", headers=user_headers,
            json={"amount": 25, "payment_method": payment_method},
        )
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["checkout_guard"] == CHECKOUT_GUARD_BILLING_PAUSED
    assert error["message"] == BILLING_PAUSED_MESSAGE
    assert calls == []


def test_abuse_pause_refusal_holds_even_with_the_guard_off(user_headers: dict[str, str]) -> None:
    settings = _settings(paypal_topup_guard_mode="off")
    with TestClient(create_app(settings, init_observability=False)) as client:
        _latched_api_workspace(client, user_headers)
        response = client.post(
            "/v1/billing/checkout", headers=user_headers,
            json={"amount": 25, "payment_method": "paypal"},
        )
    assert response.status_code == 403


def test_principal_recovery_pause_still_allows_card_checkout(user_headers: dict[str, str]) -> None:
    """Only the abuse latch stops card top-ups; a recovery debt is repaid by them."""
    with TestClient(create_app(Settings(environment="local"), init_observability=False)) as client:  # type: ignore[call-arg]
        workspace_id = str(client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"])
        workspace = STORE.get_workspace(workspace_id)
        assert workspace is not None
        workspace.billing_pause_causes = ["principal_recovery"]
        response = client.post(
            "/v1/billing/checkout", headers=user_headers,
            json={"amount": 25, "payment_method": "auto"},
        )
    assert response.status_code == 201, response.text


def test_console_card_checkout_for_latched_workspace_shows_the_paused_line() -> None:
    client, _, workspace = _console_client()
    _latch(workspace.id)
    response = client.post(
        "/console/credits/checkout",
        data={"amount": "25", "payment_method": "auto"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={CHECKOUT_GUARD_BILLING_PAUSED}"
    assert "Billing for this workspace is paused" in client.get(response.headers["location"]).text


def test_paypal_capture_landing_on_latched_workspace_is_held_and_alerted(
    held_alerts: list[str], alerts: list[str],
) -> None:
    """An order approved before the latch: PayPal sends the capture anyway."""
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        _latch(workspace_id)
        data = _webhook(client, _capture_event(workspace_id, user_id, capture_id="CAPTURE-HELD"))
        assert (data["credited"], data["held"]) == (True, True)
        # A redelivery credits nothing and alerts nothing new.
        replay = _webhook(client, _capture_event(workspace_id, user_id, capture_id="CAPTURE-HELD"))
        assert (replay["credited"], replay["held"]) == (False, False)
    workspace = STORE.get_workspace(workspace_id)
    # Held: the abuse pause blocks every spend path until an operator clears it.
    assert workspace is not None and workspace.billing_paused
    assert "abuse" in workspace.billing_pause_causes
    assert len(held_alerts) == 1
    assert "paused_topup.held provider=paypal" in held_alerts[0]
    assert "CAPTURE-HELD" in held_alerts[0]


def test_capture_raced_by_a_latch_redirects_to_the_paused_line(
    monkeypatch: pytest.MonkeyPatch, held_alerts: list[str],
) -> None:
    """The latch lands while PayPal is capturing: the credit is held, not shown as success."""
    client, user, workspace = _console_client()

    def paypal_post(_settings: Settings, path: str, **_kwargs: Any) -> dict[str, Any]:
        _latch(workspace.id, "ops-race")
        return _order(workspace.id, user.id, capture_id="CAPTURE-RACE")

    monkeypatch.setattr(paypal_billing, "_paypal_post", paypal_post)
    mock_approved_order(monkeypatch, workspace.id)
    monkeypatch.setattr(Settings, "paypal_enabled", property(lambda _self: True))
    response = client.get("/console/credits/paypal/capture?token=ORDER-RACE", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={CHECKOUT_GUARD_BILLING_PAUSED}"
    assert len(held_alerts) == 1 and "CAPTURE-RACE" in held_alerts[0]


def test_credit_on_unlatched_workspace_is_not_held(held_alerts: list[str]) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id, user_id = _account()
        data = _webhook(client, _capture_event(workspace_id, user_id, capture_id="CAPTURE-OK"))
    assert (data["credited"], data["held"]) == (True, False)
    assert held_alerts == []


def test_stripe_checkout_landing_on_latched_workspace_is_alerted(
    client: TestClient, user_headers: dict[str, str], held_alerts: list[str],
) -> None:
    workspace_id = _latched_api_workspace(client, user_headers)
    session = {
        "id": "cs_held",
        "mode": "payment",
        "payment_intent": "pi_held",
        "payment_status": "paid",
        "amount_total": 2_521,
        "metadata": {
            "workspace_id": workspace_id,
            "payment_method": "ach",
            "credit_amount_microdollars": "25000000",
            "processing_fee_cents": "21",
            "charge_amount_cents": "2521",
            "fee_variable_basis_points": "80",
            "fee_fixed_cents": "0",
            "fee_max_cents": "500",
        },
    }
    response = client.post(
        "/v1/internal/stripe/webhook",
        json={"id": "evt_held", "type": "checkout.session.completed", "data": {"object": session}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["credited"] is True
    assert len(held_alerts) == 1
    assert "provider=stripe" in held_alerts[0] and "pi_held" in held_alerts[0]


def test_auto_refill_never_charges_an_abuse_latched_workspace() -> None:
    settings = Settings(environment="test", stripe_secret_key="sk_test_dummy")  # type: ignore[call-arg]  # noqa: S106
    user = STORE.ensure_user("refill-latched@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.update_auto_refill_settings(
        workspace.id, enabled=True, threshold_microdollars=5_000_000, amount_microdollars=20_000_000,
    )
    STORE.set_stripe_customer(workspace.id, customer_id="cus_test_1", payment_method_id="pm_test_1")
    reservation = STORE.reserve(workspace.id, "latched-key", 9_000_000)
    STORE.settle(reservation.id, 9_000_000)
    _latch(workspace.id)
    outcome = maybe_charge_after_settle(workspace.id, settings=settings)
    assert outcome == AutoRefillOutcome(fired=False, reason="abuse_paused")
