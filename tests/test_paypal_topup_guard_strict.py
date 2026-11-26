"""PayPal top-up guard, second round: verified payers, card-first, payer email
match, verified phone, impossible emails and the lower new-workspace cap.

Every rule applies only to workspaces that are not established; each has a
positive control showing an established workspace is unaffected, and the
capture-path tests show a refused capture never reaches PayPal's capture call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import paypal_billing, paypal_topup_guard
from trusted_router.services.email_typos import email_domain, is_impossible_email
from trusted_router.services.paypal_billing import (
    capture_paypal_order_for_workspace,
    paypal_payer_facts,
)
from trusted_router.services.paypal_topup_guard import (
    GUARD_CODE_AMOUNT_CAP,
    GUARD_CODE_FIRST_TOPUP,
    GUARD_CODE_UNAVAILABLE,
    PAYPAL_FIRST_TOPUP_MESSAGE,
    PayPalPayerFacts,
    check_paypal_capture,
    check_paypal_checkout,
    checkout_guard_code,
)
from trusted_router.storage import STORE, InMemoryStore

OWNER_EMAIL = "owner@example.com"


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id=None,
        **overrides,
    )


def _account(
    email: str = OWNER_EMAIL, *, phone: bool = True, prior_topup: bool = True,
) -> tuple[str, str]:
    """A new, not established account. By default it has a verified phone and
    one small completed card top-up, so only the rule under test can fire."""
    result = STORE.signup(email=email)
    assert result is not None
    user = result.user
    user.phone_verified = phone
    if prior_topup:
        assert STORE.add_lifetime_topup(user.id, 10_000_000, f"card-topup-{user.id}")
    return result.workspace.id, user.id


def _establish(user_id: str) -> None:
    user = STORE.get_user(user_id)
    assert user is not None
    user.identity_status = "approved"


def _checkout(workspace_id: str, user_id: str, settings: Settings | None = None,
              cents: int = 2_000) -> Any:
    return check_paypal_checkout(
        STORE, workspace_id=workspace_id, initiating_user_id=user_id,
        credit_amount_cents=cents, settings=settings or _settings(),
    )


def _refusal(raised: pytest.ExceptionInfo[HTTPException]) -> tuple[int, str | None]:
    return raised.value.status_code, checkout_guard_code(raised.value)


def _message(raised: pytest.ExceptionInfo[HTTPException]) -> str:
    return str(raised.value.detail["error"]["message"])


def _facts(email: str | None = OWNER_EMAIL, status: str | None = "VERIFIED",
           user_id: str | None = None) -> PayPalPayerFacts:
    return PayPalPayerFacts(email=email, account_status=status, initiating_user_id=user_id)


def _capture_check(workspace_id: str, facts: PayPalPayerFacts | None = None,
                   settings: Settings | None = None) -> Any:
    return check_paypal_capture(
        STORE, workspace_id=workspace_id, settings=settings or _settings(),
        load_payer=lambda: facts or _facts(),
    )


def _observed_rules(caplog: pytest.LogCaptureFixture) -> set[str]:
    rules: set[str] = set()
    for record in caplog.records:
        if record.getMessage() in {"paypal_topup_guard.observed", "paypal_topup_guard.refused"}:
            rules |= set(record.__dict__["rules"].split(","))
    return rules


# --- impossible emails (unit) ----------------------------------------------------


@pytest.mark.parametrize("email", [
    "someone@gmail.con",
    "someone@yahoo.cmo",
    "someone@outlook.comm",
    "someone@company.ocm",
    "someone@mail.nett",
    "someone@gmial.com",
    "someone@gmail.co",
    "someone@gamil.com",
    "someone@hotmial.com",
    "someone@yaho.com",
    "Someone@GMAIL.CON ",
    # malformed
    "nobody",
    "nobody@",
    "@gmail.com",
    "someone@localhost",
    "someone@gmail..com",
    "",
    None,
])
def test_impossible_emails_are_caught(email: str | None) -> None:
    assert is_impossible_email(email) is True


@pytest.mark.parametrize("email", [
    "someone@gmail.com",
    "Someone@Gmail.COM ",
    "someone@hotmail.co.uk",
    "someone@yahoo.co.jp",
    "someone@outlook.com",
    "someone@icloud.com",
    # Real ccTLDs that look like typos are never caught as bare TLDs.
    "someone@startup.co",
    "someone@business.cm",
    "someone@company.om",
    "someone@example.org",
    "someone@mail.example.net",
    "someone@gmail.com.",
])
def test_real_emails_are_not_caught(email: str) -> None:
    assert is_impossible_email(email) is False


def test_email_domain_normalises() -> None:
    assert email_domain(" A@Example.COM. ") == "example.com"
    assert email_domain("no-at-sign") is None


# --- defaults ----------------------------------------------------------------------


def test_new_rules_default_to_enforced_and_cap_to_25_dollars() -> None:
    settings = _settings()
    assert settings.paypal_new_workspace_max_topup_cents == 2_500
    assert settings.paypal_require_prior_topup is True
    assert settings.paypal_require_verified_payer is True
    assert settings.paypal_require_payer_email_match is True
    assert settings.paypal_require_phone_verified is True
    assert settings.paypal_reject_impossible_email is True


# --- rule 6: lower cap -------------------------------------------------------------


def test_new_workspace_cap_is_25_dollars() -> None:
    workspace_id, user_id = _account()
    _checkout(workspace_id, user_id, cents=2_500)
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id, cents=2_501)
    assert _refusal(raised) == (403, GUARD_CODE_AMOUNT_CAP)
    assert "$25 " in _message(raised)


def test_established_workspace_is_not_capped_at_25_dollars() -> None:
    workspace_id, user_id = _account()
    _establish(user_id)
    assert _checkout(workspace_id, user_id, cents=50_000).established is True


# --- rule 2: no PayPal for the first top-up ----------------------------------------


def test_first_topup_with_paypal_is_refused_with_the_card_line() -> None:
    workspace_id, user_id = _account(prior_topup=False)
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_FIRST_TOPUP)
    assert _message(raised) == PAYPAL_FIRST_TOPUP_MESSAGE
    assert "card" in _message(raised)


def test_after_a_completed_card_topup_paypal_is_allowed() -> None:
    workspace_id, user_id = _account(prior_topup=False)
    with pytest.raises(HTTPException):
        _checkout(workspace_id, user_id)
    assert STORE.add_lifetime_topup(user_id, 5_000_000, "stripe-first")
    assert _checkout(workspace_id, user_id) is not None


def test_a_paying_member_with_a_prior_topup_counts() -> None:
    workspace_id, _ = _account(prior_topup=False)
    member = STORE.ensure_user("member-paid@example.com")
    assert STORE.add_lifetime_topup(member.id, 5_000_000, "member-card")
    assert _checkout(workspace_id, member.id) is not None


def test_signup_trial_credit_is_not_a_prior_topup() -> None:
    workspace_id, user_id = _account(prior_topup=False)
    account = STORE.get_credit_account(workspace_id)
    assert account is not None  # the workspace exists with its signup credit
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_FIRST_TOPUP)


def test_established_workspace_skips_the_first_topup_rule() -> None:
    workspace_id, user_id = _account(prior_topup=False)
    _establish(user_id)
    assert _checkout(workspace_id, user_id) is not None


def test_first_topup_rule_switch_and_log_only(caplog: pytest.LogCaptureFixture) -> None:
    workspace_id, user_id = _account(prior_topup=False)
    assert _checkout(workspace_id, user_id, _settings(paypal_require_prior_topup=False)) is not None
    with caplog.at_level(logging.INFO, logger=paypal_topup_guard.__name__):
        assert _checkout(
            workspace_id, user_id,
            _settings(paypal_topup_guard_log_only_rules="paypal_first_topup"),
        ) is not None
    assert "paypal_first_topup" in _observed_rules(caplog)


def test_unreadable_topup_history_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account()

    def broken(*_a: object, **_k: object) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(InMemoryStore, "get_lifetime_topup_microdollars", broken)
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_FIRST_TOPUP)


def test_suspended_account_never_sees_the_card_first_line() -> None:
    workspace_id, user_id = _account(prior_topup=False)
    user = STORE.get_user(user_id)
    assert user is not None
    user.suspended = True
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)


def test_first_topup_is_also_checked_at_capture_before_any_paypal_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An order created before this rule shipped is still not captured."""
    workspace_id, user_id = _account(prior_topup=False)
    calls: list[str] = []
    monkeypatch.setattr(paypal_billing, "_paypal_get", lambda _s, path, **_k: calls.append(path) or {})
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {})
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER-FIRST", workspace_id=workspace_id, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_FIRST_TOPUP)
    assert calls == []


# --- rule 4: verified phone ---------------------------------------------------------


def test_unverified_phone_is_refused_without_naming_the_rule() -> None:
    workspace_id, user_id = _account(phone=False)
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert "phone" not in _message(raised).lower()


def test_unverified_phone_switch_off_logs_without_refusing(caplog: pytest.LogCaptureFixture) -> None:
    workspace_id, user_id = _account(phone=False)
    with caplog.at_level(logging.INFO, logger=paypal_topup_guard.__name__):
        assert _checkout(
            workspace_id, user_id, _settings(paypal_require_phone_verified=False),
        ) is not None
    assert "phone_unverified" in _observed_rules(caplog)


def test_unverified_phone_log_only_demotion() -> None:
    workspace_id, user_id = _account(phone=False)
    assert _checkout(
        workspace_id, user_id, _settings(paypal_topup_guard_log_only_rules="phone_unverified"),
    ) is not None


def test_established_workspace_without_phone_is_unaffected() -> None:
    workspace_id, user_id = _account(phone=False)
    _establish(user_id)
    assert _checkout(workspace_id, user_id) is not None
    assert _capture_check(workspace_id) is not None


def test_unverified_phone_is_refused_at_capture_before_paypal(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, _ = _account(phone=False)
    calls: list[str] = []
    monkeypatch.setattr(paypal_billing, "_paypal_get", lambda _s, path, **_k: calls.append(path) or {})
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {})
    with pytest.raises(HTTPException):
        capture_paypal_order_for_workspace(
            order_id="ORDER-PHONE", workspace_id=workspace_id, settings=_settings(),
        )
    assert calls == []


# --- rule 5: impossible account emails at checkout ----------------------------------


def test_owner_with_impossible_email_is_refused_at_checkout() -> None:
    workspace_id, user_id = _account("someone@gmail.con")
    with pytest.raises(HTTPException) as raised:
        _checkout(workspace_id, user_id)
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert ".con" not in _message(raised)


def test_impossible_account_email_switch_off_and_established() -> None:
    workspace_id, user_id = _account("someone@gamil.com")
    assert _checkout(workspace_id, user_id, _settings(paypal_reject_impossible_email=False)) is not None
    _establish(user_id)
    assert _checkout(workspace_id, user_id) is not None


# --- payer rules at capture (1, 3, 5) -----------------------------------------------


def test_verified_matching_payer_is_allowed() -> None:
    workspace_id, _ = _account()
    assert _capture_check(workspace_id, _facts()) is not None


def test_unverified_paypal_payer_is_refused() -> None:
    workspace_id, _ = _account()
    with pytest.raises(HTTPException) as raised:
        _capture_check(workspace_id, _facts(status="UNVERIFIED"))
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert "verif" not in _message(raised).lower()


def test_unknown_payer_status_is_logged_not_refused(caplog: pytest.LogCaptureFixture) -> None:
    workspace_id, _ = _account()
    with caplog.at_level(logging.INFO, logger=paypal_topup_guard.__name__):
        assert _capture_check(workspace_id, _facts(status=None)) is not None
    assert "paypal_payer_status_unknown" in _observed_rules(caplog)


def test_verified_payer_switch_off_and_log_only() -> None:
    workspace_id, _ = _account()
    unverified = _facts(status="UNVERIFIED")
    assert _capture_check(
        workspace_id, unverified, _settings(paypal_require_verified_payer=False),
    ) is not None
    assert _capture_check(
        workspace_id, unverified,
        _settings(paypal_topup_guard_log_only_rules="paypal_payer_unverified"),
    ) is not None


def test_payer_email_mismatch_is_refused() -> None:
    workspace_id, _ = _account()
    with pytest.raises(HTTPException) as raised:
        _capture_check(workspace_id, _facts(email="someone-else@example.net"))
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert "email" not in _message(raised).lower()


def test_missing_payer_email_cannot_match() -> None:
    workspace_id, _ = _account()
    with pytest.raises(HTTPException):
        _capture_check(workspace_id, _facts(email=None))


def test_payer_email_match_is_case_insensitive_and_accepts_the_paying_member() -> None:
    workspace_id, _ = _account()
    assert _capture_check(workspace_id, _facts(email="  OWNER@Example.com ")) is not None
    member = STORE.ensure_user("member-payer@example.com")
    assert _capture_check(
        workspace_id, _facts(email="member-payer@example.com", user_id=member.id),
    ) is not None


def test_payer_email_match_switch_off_and_log_only() -> None:
    workspace_id, _ = _account()
    other = _facts(email="someone-else@example.net")
    assert _capture_check(
        workspace_id, other, _settings(paypal_require_payer_email_match=False),
    ) is not None
    assert _capture_check(
        workspace_id, other, _settings(paypal_topup_guard_log_only_rules="payer_email_mismatch"),
    ) is not None


def test_impossible_payer_email_is_refused_on_its_own() -> None:
    workspace_id, _ = _account()
    typo = _facts(email="owner@gmail.con")
    # The match rule is demoted so only the impossible-email rule can refuse.
    match_logged = "payer_email_mismatch"
    with pytest.raises(HTTPException) as raised:
        _capture_check(workspace_id, typo, _settings(paypal_topup_guard_log_only_rules=match_logged))
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert _capture_check(
        workspace_id, typo,
        _settings(paypal_topup_guard_log_only_rules=match_logged, paypal_reject_impossible_email=False),
    ) is not None


def test_established_workspace_never_reads_the_order() -> None:
    workspace_id, user_id = _account(phone=False, prior_topup=False)
    _establish(user_id)

    def must_not_load() -> PayPalPayerFacts:
        raise AssertionError("established workspaces skip the payer read")

    subject = check_paypal_capture(
        STORE, workspace_id=workspace_id, settings=_settings(), load_payer=must_not_load,
    )
    assert subject is not None and subject.established is True


def test_payer_rules_all_off_skip_the_order_read() -> None:
    workspace_id, _ = _account()

    def must_not_load() -> PayPalPayerFacts:
        raise AssertionError("no payer rule is on")

    settings = _settings(
        paypal_require_verified_payer=False,
        paypal_require_payer_email_match=False,
        paypal_reject_impossible_email=False,
    )
    assert check_paypal_capture(
        STORE, workspace_id=workspace_id, settings=settings, load_payer=must_not_load,
    ) is not None


def test_order_read_outage_fails_closed() -> None:
    workspace_id, _ = _account()

    def outage() -> PayPalPayerFacts:
        raise HTTPException(status_code=502, detail="PayPal request failed")

    with pytest.raises(HTTPException) as raised:
        check_paypal_capture(STORE, workspace_id=workspace_id, settings=_settings(), load_payer=outage)
    assert raised.value.status_code == 503


def test_order_read_outage_in_log_mode_lets_the_capture_proceed() -> None:
    workspace_id, _ = _account()

    def outage() -> PayPalPayerFacts:
        raise RuntimeError("timeout")

    assert check_paypal_capture(
        STORE, workspace_id=workspace_id, settings=_settings(paypal_topup_guard_mode="log"),
        load_payer=outage,
    ) is not None


def test_unknown_order_stays_a_client_error() -> None:
    workspace_id, _ = _account()

    def unknown() -> PayPalPayerFacts:
        raise HTTPException(status_code=400, detail="PayPal order could not be captured")

    with pytest.raises(HTTPException) as raised:
        check_paypal_capture(STORE, workspace_id=workspace_id, settings=_settings(), load_payer=unknown)
    assert raised.value.status_code == 400


def test_refused_payer_does_not_use_up_the_daily_capture_limit() -> None:
    workspace_id, _ = _account()
    settings = _settings()
    for _ in range(3):
        with pytest.raises(HTTPException):
            _capture_check(workspace_id, _facts(status="UNVERIFIED"), settings)
    # The workspace still has both of today's captures.
    _capture_check(workspace_id, _facts(), settings)
    _capture_check(workspace_id, _facts(), settings)


# --- payer facts from PayPal's order shape -----------------------------------------


def _approved_order(workspace_id: str, user_id: str, *, email: str = OWNER_EMAIL,
                    status: str | None = "VERIFIED", payer_block: bool = False) -> dict[str, Any]:
    wallet: dict[str, Any] = {"email_address": email, "account_id": "PAYERACCT"}
    if status is not None:
        wallet["account_status"] = status
    order: dict[str, Any] = {
        "id": "ORDER-APPROVED",
        "status": "APPROVED",
        "payment_source": {"paypal": wallet},
        "purchase_units": [{
            "reference_id": workspace_id,
            "custom_id": f"tr1|{workspace_id}|2000|2085|{user_id}",
        }],
    }
    if payer_block:
        order["payer"] = {"email_address": email, "payer_id": "PAYERACCT"}
    return order


def test_payer_facts_read_payment_source_paypal() -> None:
    facts = paypal_payer_facts(_approved_order("ws_1", "user_1", email="A@Example.com", status="unverified"))
    assert facts == PayPalPayerFacts(
        email="a@example.com", account_status="UNVERIFIED", initiating_user_id="user_1",
    )


def test_payer_facts_tolerate_missing_fields() -> None:
    assert paypal_payer_facts({"id": "X"}) == PayPalPayerFacts(
        email=None, account_status=None, initiating_user_id=None,
    )
    facts = paypal_payer_facts(_approved_order("ws_1", "user_1", status=None, payer_block=True))
    assert facts.account_status is None and facts.email == OWNER_EMAIL


# --- capture path: refused payers never reach PayPal's capture ------------------------


def _captured_order(workspace_id: str, user_id: str, capture_id: str) -> dict[str, Any]:
    return {
        "id": f"ORDER-{capture_id}",
        "status": "COMPLETED",
        "payer": {"email_address": OWNER_EMAIL, "payer_id": "PAYERACCT"},
        "purchase_units": [{
            "reference_id": workspace_id,
            "custom_id": f"tr1|{workspace_id}|2000|2085|{user_id}",
            "payments": {"captures": [{
                "id": capture_id,
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "20.85"},
            }]},
        }],
    }


def _paypal_calls(monkeypatch: pytest.MonkeyPatch, workspace_id: str, user_id: str,
                  **order_kwargs: Any) -> list[str]:
    calls: list[str] = []

    def paypal_get(_settings: Settings, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(f"GET {path}")
        return _approved_order(workspace_id, user_id, **order_kwargs)

    def paypal_post(_settings: Settings, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(f"POST {path}")
        return _captured_order(workspace_id, user_id, f"CAPTURE-{len(calls)}")

    monkeypatch.setattr(paypal_billing, "_paypal_get", paypal_get)
    monkeypatch.setattr(paypal_billing, "_paypal_post", paypal_post)
    return calls


@pytest.mark.parametrize("order_kwargs", [
    {"status": "UNVERIFIED"},
    {"email": "someone-else@example.net"},
    {"email": "owner@gmail.con"},
])
def test_refused_payer_capture_reads_the_order_but_never_captures(
    monkeypatch: pytest.MonkeyPatch, order_kwargs: dict[str, Any],
) -> None:
    workspace_id, user_id = _account()
    calls = _paypal_calls(monkeypatch, workspace_id, user_id, **order_kwargs)
    before = STORE.get_credit_account(workspace_id)
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER-APPROVED", workspace_id=workspace_id, settings=_settings(),
        )
    assert _refusal(raised) == (403, GUARD_CODE_UNAVAILABLE)
    assert calls == ["GET /v2/checkout/orders/ORDER-APPROVED"]
    assert STORE.get_credit_account(workspace_id) == before


def test_verified_matching_payer_capture_is_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account()
    calls = _paypal_calls(monkeypatch, workspace_id, user_id)
    result = capture_paypal_order_for_workspace(
        order_id="ORDER-APPROVED", workspace_id=workspace_id, settings=_settings(),
    )
    assert result.credited is True
    assert calls == [
        "GET /v2/checkout/orders/ORDER-APPROVED",
        "POST /v2/checkout/orders/ORDER-APPROVED/capture",
    ]


def test_established_capture_skips_the_order_read_even_for_an_unverified_payer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, user_id = _account(phone=False, prior_topup=False)
    _establish(user_id)
    calls = _paypal_calls(monkeypatch, workspace_id, user_id, status="UNVERIFIED")
    result = capture_paypal_order_for_workspace(
        order_id="ORDER-APPROVED", workspace_id=workspace_id, settings=_settings(),
    )
    assert result.credited is True
    assert calls == ["POST /v2/checkout/orders/ORDER-APPROVED/capture"]


def test_order_id_with_a_path_separator_is_refused_before_paypal(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, user_id = _account()
    calls = _paypal_calls(monkeypatch, workspace_id, user_id)
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER/../x", workspace_id=workspace_id, settings=_settings(),
        )
    assert raised.value.status_code == 400
    assert calls == []



@pytest.mark.parametrize(("paypal_status", "expected_status"), [
    (404, 400), (422, 400), (401, 503), (500, 503),
])
def test_order_read_failure_is_classified_and_never_captures(
    monkeypatch: pytest.MonkeyPatch, paypal_status: int, expected_status: int,
) -> None:
    """An unknown order stays the caller's 400; a PayPal or credential failure
    fails closed (503). Either way the capture is never sent."""
    workspace_id, _ = _account()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(paypal_status, request=request, json={"name": "ERROR"})

    real_client = httpx.Client
    monkeypatch.setattr(paypal_billing, "_access_token", lambda _settings: "token")
    monkeypatch.setattr(
        paypal_billing.httpx, "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(HTTPException) as raised:
        capture_paypal_order_for_workspace(
            order_id="ORDER-READ", workspace_id=workspace_id, settings=_settings(),
        )
    assert raised.value.status_code == expected_status
    assert seen == ["GET /v2/checkout/orders/ORDER-READ"]

# --- routes ---------------------------------------------------------------------------


def test_api_first_topup_with_paypal_is_refused_before_an_order_exists(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(paypal_billing, "_paypal_post", lambda _s, path, **_k: calls.append(path) or {})
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id = str(client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"])
        workspace = STORE.get_workspace(workspace_id)
        assert workspace is not None
        owner = STORE.get_user(workspace.owner_user_id)
        assert owner is not None
        owner.phone_verified = True
        response = client.post(
            "/v1/billing/checkout", headers=user_headers,
            json={"amount": 20, "payment_method": "paypal"},
        )
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["checkout_guard"] == GUARD_CODE_FIRST_TOPUP
    assert error["message"] == PAYPAL_FIRST_TOPUP_MESSAGE
    assert calls == []


def _console_client(*, prior_topup: bool) -> tuple[TestClient, Any, Any]:
    app = create_app(Settings(environment="local"), init_observability=False)  # type: ignore[call-arg]
    client = TestClient(app)
    user = STORE.ensure_user("strict-console@example.com")
    user.phone_verified = True
    if prior_topup:
        assert STORE.add_lifetime_topup(user.id, 10_000_000, f"card-{user.id}")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="email", label="strict-console@example.com",
        ttl_seconds=3600, workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, user, workspace


def test_console_first_topup_with_paypal_points_to_card_checkout() -> None:
    client, _, _ = _console_client(prior_topup=False)
    response = client.post(
        "/console/credits/checkout",
        data={"amount": "20", "payment_method": "paypal"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={GUARD_CODE_FIRST_TOPUP}"
    page = client.get(response.headers["location"]).text
    assert "Your first top-up needs to be made with a card." in page


def test_console_first_topup_with_a_card_is_unaffected() -> None:
    client, _, _ = _console_client(prior_topup=False)
    response = client.post(
        "/console/credits/checkout",
        data={"amount": "20", "payment_method": "auto"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]


def test_console_capture_with_unverified_payer_redirects_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, user, workspace = _console_client(prior_topup=True)
    calls = _paypal_calls(monkeypatch, workspace.id, user.id, status="UNVERIFIED",
                          email="strict-console@example.com")
    monkeypatch.setattr(Settings, "paypal_enabled", property(lambda _self: True))
    response = client.get("/console/credits/paypal/capture?token=ORDER-APPROVED", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/console/credits?error={GUARD_CODE_UNAVAILABLE}"
    assert calls == ["GET /v2/checkout/orders/ORDER-APPROVED"]


def test_console_capture_with_verified_payer_is_credited(monkeypatch: pytest.MonkeyPatch) -> None:
    client, user, workspace = _console_client(prior_topup=True)
    calls = _paypal_calls(monkeypatch, workspace.id, user.id, email="strict-console@example.com")
    monkeypatch.setattr(Settings, "paypal_enabled", property(lambda _self: True))
    response = client.get("/console/credits/paypal/capture?token=ORDER-APPROVED", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/console/credits?checkout=success")
    assert calls[-1] == "POST /v2/checkout/orders/ORDER-APPROVED/capture"


def test_api_capture_with_unverified_payer_never_captures(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str],
) -> None:
    monkeypatch.setattr(Settings, "paypal_enabled", property(lambda _self: True))
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        workspace_id = str(client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"])
        workspace = STORE.get_workspace(workspace_id)
        assert workspace is not None
        owner = STORE.get_user(workspace.owner_user_id)
        assert owner is not None
        owner.phone_verified = True
        assert STORE.add_lifetime_topup(owner.id, 10_000_000, f"card-{owner.id}")
        calls = _paypal_calls(monkeypatch, workspace_id, owner.id, status="UNVERIFIED",
                              email=owner.email)
        response = client.post(
            "/v1/billing/paypal/orders/ORDER-APPROVED/capture", headers=user_headers,
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["checkout_guard"] == GUARD_CODE_UNAVAILABLE
    assert calls == ["GET /v2/checkout/orders/ORDER-APPROVED"]

